from uuid import UUID

import pytest
from sqlalchemy import func, select

from management.identity.bootstrap_admin import bootstrap_admin
from management.notifications.service import notify_project_members, notify_user
from management.projects.models import Notification, NotificationPreference
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project


async def notices(harness, user_id, kind):
    async with harness.factory() as db:
        return await db.scalar(
            select(func.count(Notification.id)).where(
                Notification.recipient_user_id == UUID(user_id), Notification.kind == kind
            )
        )


@pytest.mark.asyncio
async def test_members_read_and_change_only_their_own_choices(harness: Harness) -> None:
    async with (
        harness.client() as manager_client,
        harness.client() as researcher_client,
        harness.client() as admin_client,
    ):
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        await login(harness, admin_client, uid="admin", email="admin@example.com")
        await bootstrap_admin(
            "admin@example.com", settings=harness.settings, session_factory=harness.factory
        )
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        url = f"{PROJECTS}/{project['id']}/notification-preferences/me"
        headers = mutation_headers(manager["csrf_token"])

        first = await manager_client.get(url)
        assert first.status_code == 200, first.text
        data = first.json()["data"]
        assert data["version"] == 0 and data["updated_at"] is None
        assert [(e["key"], e["in_app"], e["email"]) for e in data["events"]] == [
            ("review", True, False),
            ("finished", True, False),
            ("comments", True, False),
            ("members", True, False),
            ("budget", True, False),
            ("integrations", True, False),
        ]
        assert data["events"][0]["supported"] == {"in_app": True, "email": False}
        assert data["events"][0]["reason_codes"]["email"] == "EMAIL_SET_BY_PROJECT"
        assert "project_email" not in data["capabilities"]
        assert data["digest"]["timezone"] == "Asia/Ho_Chi_Minh"
        assert data["capabilities"]["digest"]["supported"] is False
        async with harness.factory() as db:
            assert await db.scalar(select(func.count(NotificationPreference.id))) == 0
        # Admins who are not members have no preferences here.
        assert code(await admin_client.get(url)) == (404, "NOT_FOUND")

        saved = await manager_client.patch(
            url,
            json={"version": 0, "events": {"comments": {"in_app": False}}},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["version"] == 1
        reloaded = (await manager_client.get(url)).json()["data"]
        comments = next(e for e in reloaded["events"] if e["key"] == "comments")
        assert (comments["in_app"], comments["email"]) == (False, False)
        assert (await researcher_client.get(url)).json()["data"]["version"] == 0

        stale = {"version": 0, "events": {"review": {"in_app": False}}}
        assert code(await manager_client.patch(url, json=stale, headers=headers)) == (
            409,
            "NOTIFICATION_PREFERENCES_VERSION_CONFLICT",
        )
        for body, expected in (
            ({"version": 1}, (422, "EMPTY_UPDATE")),
            ({"version": 1, "events": {}}, (422, "EMPTY_UPDATE")),
            ({"version": 1, "events": {"review": {}}, "digest": {}}, (422, "EMPTY_UPDATE")),
            (
                {"version": 1, "events": {"review": {"email": True}}},
                (422, "NOTIFICATION_PREFERENCE_UNSUPPORTED"),
            ),
            (
                {"version": 1, "digest": {"frequency": "daily"}},
                (422, "NOTIFICATION_PREFERENCE_UNSUPPORTED"),
            ),
            (
                {"version": 1, "quiet_hours": {"enabled": True}},
                (422, "NOTIFICATION_PREFERENCE_UNSUPPORTED"),
            ),
            ({"version": 1, "events": {"review": {"in_app": None}}}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "events": {"mentions": {"in_app": False}}}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "user_id": researcher["user"]["id"]}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "digest": {"timezone": "Mars/Base"}}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "digest": {"weekday": 8}}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "quiet_hours": {"start": "25:00"}}, (422, "VALIDATION_ERROR")),
            ({"version": 1, "quiet_hours": {"start": "08:00"}}, (422, "VALIDATION_ERROR")),
        ):
            assert code(await manager_client.patch(url, json=body, headers=headers)) == expected, (
                body
            )
        bad_zone = await manager_client.patch(
            url, json={"version": 1, "digest": {"timezone": "Mars/Base"}}, headers=headers
        )
        assert bad_zone.json()["error"]["details"][0]["field"] == "body.digest.timezone"
        # Stored defaults that stay off are fine to send back.
        quiet = await manager_client.patch(
            url,
            json={"version": 1, "quiet_hours": {"start": "23:00", "end": "06:30"}},
            headers=headers,
        )
        assert quiet.status_code == 200, quiet.text
        assert quiet.json()["data"]["quiet_hours"]["start"] == "23:00"


@pytest.mark.asyncio
async def test_turning_a_group_off_stops_new_notices_and_keeps_old_ones(harness: Harness) -> None:
    async with harness.client() as manager_client, harness.client() as researcher_client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        project = await create_project(manager_client, manager)
        member = await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        project_id, user_id = UUID(project["id"]), researcher["user"]["id"]

        async def comment_notice():
            async with harness.factory() as db:
                await notify_project_members(db, project_id, "run_commented")
                await db.commit()

        await comment_notice()
        assert await notices(harness, user_id, "run_commented") == 1
        url = f"{PROJECTS}/{project['id']}/notification-preferences/me"
        muted = await researcher_client.patch(
            url,
            json={"version": 0, "events": {"comments": {"in_app": False}}},
            headers=mutation_headers(researcher["csrf_token"]),
        )
        assert muted.status_code == 200, muted.text
        await comment_notice()
        assert await notices(harness, user_id, "run_commented") == 1
        assert await notices(harness, manager["user"]["id"], "run_commented") == 2

        async with harness.factory() as db:
            with pytest.raises(ValueError):
                notify_user(db, UUID(user_id), "run_commented", project_id=project_id)

        removed = await manager_client.delete(
            f"{PROJECTS}/{project['id']}/members/{member['id']}",
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert removed.status_code == 200, removed.text
        assert await notices(harness, user_id, "removed_from_project") == 1
        async with harness.factory() as db:
            assert await db.scalar(select(func.count(NotificationPreference.id))) == 0


@pytest.mark.asyncio
async def test_members_see_the_email_switches_only_managers_change(harness: Harness) -> None:
    async with harness.client() as manager_client, harness.client() as researcher_client:
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        settings_url = f"{PROJECTS}/{project['id']}/settings/notifications"
        preferences_url = f"{PROJECTS}/{project['id']}/notification-preferences/me"

        denied = await researcher_client.patch(
            settings_url,
            json={"version": 0, "values": {"review": True}},
            headers=mutation_headers(researcher["csrf_token"]),
        )
        assert code(denied) == (403, "ROLE_REQUIRED")

        def emails(data):
            return {e["key"]: e["email"] for e in data["events"]}

        on = await manager_client.patch(
            settings_url,
            json={"version": 0, "values": {"review": True, "comments": True}},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert on.status_code == 200, on.text
        seen = (await researcher_client.get(preferences_url)).json()["data"]
        assert emails(seen) == {
            "review": True,
            "finished": False,
            "comments": True,
            "members": False,
            "budget": False,
            "integrations": False,
        }
        assert seen["events"][0]["supported"]["email"] is False

        off = await manager_client.patch(
            settings_url,
            json={"version": 1, "values": {"review": False}},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert off.status_code == 200, off.text
        seen = (await researcher_client.get(preferences_url)).json()["data"]
        assert emails(seen)["review"] is False and emails(seen)["comments"] is True

        # A member's own choice cannot turn email on.
        own = await researcher_client.patch(
            preferences_url,
            json={"version": 0, "events": {"review": {"email": True}}},
            headers=mutation_headers(researcher["csrf_token"]),
        )
        assert code(own) == (422, "NOTIFICATION_PREFERENCE_UNSUPPORTED")
