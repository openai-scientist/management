from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select

from management.core.roles import ProjectRole
from management.infra.email_templates import project_notice
from management.notifications.email_dispatch import send_pending
from management.notifications.preferences import KIND_GROUP, MANDATORY_KINDS
from management.notifications.service import notify_project_members
from management.projects.models import NOTICE_KINDS, NotificationEmail, ProjectSetting
from tests.fakes import FakeEmailSender
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, create_project

MEMBERS = ["ana", "binh", "chi", "dung", "em"]


async def queued(harness: Harness, kind: str) -> list[UUID]:
    async with harness.factory() as db:
        rows = await db.scalars(select(NotificationEmail).where(NotificationEmail.kind == kind))
        return sorted(row.recipient_user_id for row in rows)


async def email_on(harness: Harness, project_id) -> None:
    """Turn email on for every group, as a Project Manager's save would."""
    async with harness.factory() as db:
        db.add(
            ProjectSetting(
                project_id=UUID(str(project_id)),
                sections={"notifications": dict.fromkeys(KIND_GROUP.values(), True)},
            )
        )
        await db.commit()


async def six_member_project(harness: Harness, manager_client):
    manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
    project = await create_project(manager_client, manager)
    members = []
    for name in MEMBERS:
        async with harness.client() as member_client:
            members.append(
                await login(harness, member_client, uid=name, email=f"{name}@example.com")
            )
        await add_member(
            manager_client, manager, project["id"], f"{name}@example.com", "researcher"
        )
    return manager, project, members


async def notify(harness: Harness, project_id: UUID, kind: str, **kwargs) -> None:
    async with harness.factory() as db:
        await notify_project_members(db, project_id, kind, **kwargs)
        await db.commit()


@pytest.mark.asyncio
async def test_every_recipient_gets_an_email_while_the_project_switch_is_on(
    harness: Harness,
) -> None:
    async with harness.client() as manager_client:
        manager, project, members = await six_member_project(harness, manager_client)
        project_id, manager_id = UUID(project["id"]), UUID(manager["user"]["id"])
        member_ids = sorted(UUID(m["user"]["id"]) for m in members)

        # Off until a Project Manager turns it on; earlier notices are never emailed.
        await notify(harness, project_id, "run_commented", exclude_user_id=manager_id)
        assert await queued(harness, "run_commented") == []
        async with harness.factory() as db:
            await db.execute(NotificationEmail.__table__.delete())
            await db.commit()
        await email_on(harness, project_id)

        # A member's in-app choice does not stop the email.
        async with harness.client() as ana_client:
            ana = await login(harness, ana_client, uid="ana", email="ana@example.com")
            muted = await ana_client.patch(
                f"{PROJECTS}/{project['id']}/notification-preferences/me",
                json={"version": 0, "events": {"comments": {"in_app": False}}},
                headers=mutation_headers(ana["csrf_token"]),
            )
            assert muted.status_code == 200, muted.text

        await notify(harness, project_id, "run_commented", exclude_user_id=manager_id)
        assert await queued(harness, "run_commented") == member_ids

        await notify(harness, project_id, "budget_warning", roles=(ProjectRole.MANAGER,))
        assert await queued(harness, "budget_warning") == [manager_id]

        # Turning one group off stops only that group's emails.
        off = await manager_client.patch(
            f"{PROJECTS}/{project['id']}/settings/notifications",
            json={"version": 0, "values": {"review": False}},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert off.status_code == 200, off.text
        await notify(harness, project_id, "run_awaiting_review")
        assert await queued(harness, "run_awaiting_review") == []
        await notify(harness, project_id, "run_finished")
        assert len(await queued(harness, "run_finished")) == 6


@pytest.mark.asyncio
async def test_a_rolled_back_notice_queues_no_email(harness: Harness) -> None:
    async with harness.client() as manager_client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        project = await create_project(manager_client, manager)
        await email_on(harness, project["id"])
        async with harness.factory() as db:
            await notify_project_members(db, UUID(project["id"]), "run_finished")
            await db.rollback()
        assert await queued(harness, "run_finished") == []


def test_every_notice_kind_may_be_queued() -> None:
    for kind in [*KIND_GROUP, *MANDATORY_KINDS]:
        assert f"'{kind}'" in NOTICE_KINDS


async def statuses(harness: Harness, kind: str) -> list[str]:
    async with harness.factory() as db:
        rows = await db.scalars(select(NotificationEmail).where(NotificationEmail.kind == kind))
        return sorted(row.status for row in rows)


@pytest.mark.asyncio
async def test_the_dispatcher_sends_each_queued_email_once(harness: Harness) -> None:
    async with harness.client() as manager_client:
        manager, project, _ = await six_member_project(harness, manager_client)
        project_id, manager_id = UUID(project["id"]), UUID(manager["user"]["id"])
        await email_on(harness, project_id)
        await notify(
            harness,
            project_id,
            "run_commented",
            exclude_user_id=manager_id,
            actor_user_id=manager_id,
        )
        sender = FakeEmailSender()

        await send_pending(harness.factory, sender, app_url="https://app.example.com", limit=100)
        comments = [m for m in sender.sent if m["subject"].startswith("New comment")]
        assert sorted(m["to"] for m in comments) == [f"{n}@example.com" for n in MEMBERS]
        assert f"https://app.example.com/projects/{project_id}" in comments[0]["text"]
        assert await statuses(harness, "run_commented") == ["sent"] * 5

        sender.sent.clear()
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        assert sender.sent == []


@pytest.mark.asyncio
async def test_a_refused_email_is_tried_three_times_then_given_up(harness: Harness) -> None:
    async with harness.client() as manager_client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        project = await create_project(manager_client, manager)
        await email_on(harness, project["id"])
        await notify(harness, UUID(project["id"]), "budget_warning", roles=(ProjectRole.MANAGER,))
        sender = FakeEmailSender()
        sender.accept = False

        for expected in (["pending"], ["pending"], ["failed"]):
            await send_pending(harness.factory, sender, app_url=None)
            assert await statuses(harness, "budget_warning") == expected
        # Given up: a later batch tries no more.
        sender.accept = True
        await send_pending(harness.factory, sender, app_url=None)
        assert sender.sent == []


@pytest.mark.asyncio
async def test_emails_no_longer_due_are_skipped_and_old_rows_purged(harness: Harness) -> None:
    async with harness.client() as manager_client:
        manager, project, _ = await six_member_project(harness, manager_client)
        project_id = UUID(project["id"])
        sender = FakeEmailSender()

        # Queued while its group was on, switched off before sending.
        await email_on(harness, project_id)
        await notify(harness, project_id, "run_awaiting_review")
        off = await manager_client.patch(
            f"{PROJECTS}/{project['id']}/settings/notifications",
            json={"version": 0, "values": {"review": False}},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert off.status_code == 200, off.text
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        assert await statuses(harness, "run_awaiting_review") == ["skipped"] * 6
        assert sender.sent == []

        # Waited too long.
        async with harness.factory() as db:
            db.add(
                NotificationEmail(
                    recipient_user_id=UUID(manager["user"]["id"]),
                    kind="run_finished",
                    project_id=project_id,
                    created_at=datetime.now(UTC) - timedelta(hours=25),
                )
            )
            await db.commit()
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        assert await statuses(harness, "run_finished") == ["skipped"]

        later = datetime.now(UTC) + timedelta(days=31)
        await send_pending(harness.factory, sender, app_url=None, limit=100, now=later)
        assert await statuses(harness, "run_finished") == []
        assert await statuses(harness, "run_awaiting_review") == []


@pytest.mark.asyncio
async def test_a_member_removed_before_sending_gets_no_email(harness: Harness) -> None:
    async with harness.client() as manager_client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        async with harness.client() as ana_client:
            await login(harness, ana_client, uid="ana", email="ana@example.com")
        project = await create_project(manager_client, manager)
        member = await add_member(
            manager_client, manager, project["id"], "ana@example.com", "researcher"
        )
        await email_on(harness, project["id"])
        await notify(harness, UUID(project["id"]), "run_finished")
        removed = await manager_client.delete(
            f"{PROJECTS}/{project['id']}/members/{member['id']}",
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert removed.status_code == 200, removed.text
        sender = FakeEmailSender()
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        assert [m["to"] for m in sender.sent if m["subject"].startswith("Run ended")] == [
            "manager@example.com"
        ]


@pytest.mark.asyncio
async def test_one_broken_email_neither_stops_nor_repeats_the_others(
    harness: Harness, monkeypatch
) -> None:
    async with harness.client() as manager_client:
        manager, project, _ = await six_member_project(harness, manager_client)
        await email_on(harness, project["id"])
        await notify(harness, UUID(project["id"]), "run_finished")
        broken = UUID(manager["user"]["id"])
        real = project_notice
        manager_name = manager["user"].get("display_name")

        def flaky(kind, **kwargs):
            if kwargs["recipient_name"] in (manager_name, "manager@example.com"):
                raise RuntimeError("template failed")
            return real(kind, **kwargs)

        monkeypatch.setattr("management.notifications.email_dispatch.project_notice", flaky)
        sender = FakeEmailSender()
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        await send_pending(harness.factory, sender, app_url=None, limit=100)
        sent = [m["to"] for m in sender.sent if m["subject"].startswith("Run ended")]
        assert sorted(sent) == [f"{n}@example.com" for n in MEMBERS]
        async with harness.factory() as db:
            row = await db.scalar(
                select(NotificationEmail).where(
                    NotificationEmail.kind == "run_finished",
                    NotificationEmail.recipient_user_id == broken,
                )
            )
            assert row.status == "skipped"
