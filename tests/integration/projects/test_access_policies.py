from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, update

from management.projects.models import ProjectMembership
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import (
    PROJECTS,
    accept_invitation,
    add_member,
    code,
    create_project,
    invite_member,
)


async def save(client: AsyncClient, session: dict, project_id: str, section: str, **values):
    url = f"{PROJECTS}/{project_id}/settings"
    version = (await client.get(url)).json()["data"]["version"]
    response = await client.patch(
        f"{url}/{section}",
        json={"version": version, "values": values},
        headers=mutation_headers(session["csrf_token"]),
    )
    assert response.status_code == 200, response.text


async def invite(client: AsyncClient, session: dict, project_id: str, email: str, role: str):
    return await client.post(
        f"{PROJECTS}/{project_id}/members",
        json={"email": email, "role": role},
        headers=mutation_headers(session["csrf_token"]),
    )


async def context(client: AsyncClient, project_id: str):
    return await client.get(f"{PROJECTS}/{project_id}/access-context")


@pytest.mark.asyncio
async def test_inviters_setting_decides_who_may_invite(harness: Harness) -> None:
    async with (
        harness.client() as pm,
        harness.client() as researcher,
        harness.client() as reviewer,
        harness.client() as outsider,
    ):
        manager = await login(harness, pm, uid="pm", email="pm@example.com")
        res = await login(harness, researcher, uid="res", email="res@example.com")
        rev = await login(harness, reviewer, uid="rev", email="rev@example.com")
        out = await login(harness, outsider, uid="out", email="out@example.com")
        for uid in ("a", "b", "c", "d"):
            async with harness.client() as other:
                await login(harness, other, uid=uid, email=f"{uid}@example.com")
        project = await create_project(pm, manager)
        pid = project["id"]
        await add_member(pm, manager, pid, "res@example.com", "researcher")
        await add_member(pm, manager, pid, "rev@example.com", "reviewer")
        candidates = f"{PROJECTS}/{pid}/invite-candidates"

        # Managers only (the default).
        assert code(await invite(researcher, res, pid, "a@example.com", "reviewer")) == (
            403,
            "INVITE_NOT_ALLOWED",
        )
        assert code(await researcher.get(candidates)) == (403, "INVITE_NOT_ALLOWED")
        assert (await context(researcher, pid)).json()["data"] == {
            "settings_version": 0,
            "permissions": {
                "can_invite": False,
                "can_view_invite_candidates": False,
                "can_manage_members": False,
                "can_manage_settings": False,
                "can_read_raw_datasets": True,
            },
            "invitation": None,
        }

        await save(pm, manager, pid, "access", inviters="researchers", default_role="reviewer")

        assert (await researcher.get(candidates)).status_code == 200
        sent = await invite(researcher, res, pid, "a@example.com", "reviewer")
        assert sent.status_code == 201, sent.text
        # The default role is only preselected: another allowed role may be sent.
        assert (
            await invite(researcher, res, pid, "b@example.com", "researcher")
        ).status_code == 201
        assert code(await invite(researcher, res, pid, "c@example.com", "project_manager")) == (
            403,
            "INVITE_NOT_ALLOWED",
        )
        researcher_context = (await context(researcher, pid)).json()["data"]
        assert researcher_context["permissions"]["can_invite"] is True
        assert researcher_context["permissions"]["can_manage_members"] is False
        assert researcher_context["invitation"]["default_role"] == "reviewer"
        assert researcher_context["invitation"]["allowed_roles"] == ["researcher", "reviewer"]
        manager_context = (await context(pm, pid)).json()["data"]
        assert manager_context["invitation"]["allowed_roles"] == [
            "project_manager",
            "researcher",
            "reviewer",
        ]

        # Inviting is all a Researcher gains: settings, roles and removals stay with the PM.
        headers = mutation_headers(res["csrf_token"])
        assert code(await researcher.get(f"{PROJECTS}/{pid}/settings")) == (403, "ROLE_REQUIRED")
        assert code(
            await researcher.put(
                f"{PROJECTS}/{pid}/members/{sent.json()['data']['id']}",
                json={"role": "researcher"},
                headers=headers,
            )
        ) == (403, "ROLE_REQUIRED")
        assert code(
            await researcher.delete(
                f"{PROJECTS}/{pid}/members/{sent.json()['data']['id']}", headers=headers
            )
        ) == (403, "ROLE_REQUIRED")

        # Reviewers and outsiders never invite.
        assert code(await invite(reviewer, rev, pid, "d@example.com", "reviewer")) == (
            403,
            "INVITE_NOT_ALLOWED",
        )
        assert code(await reviewer.get(candidates)) == (403, "INVITE_NOT_ALLOWED")
        assert code(await invite(outsider, out, pid, "d@example.com", "reviewer")) == (
            404,
            "NOT_FOUND",
        )
        assert code(await context(outsider, pid)) == (404, "NOT_FOUND")

        # The setting saved back to managers applies to the very next request.
        await save(pm, manager, pid, "access", inviters="managers")
        assert code(await invite(researcher, res, pid, "d@example.com", "reviewer")) == (
            403,
            "INVITE_NOT_ALLOWED",
        )

        # Archived: nobody may invite, and the context says so.
        archived = await pm.post(
            f"{PROJECTS}/{pid}/archive", headers=mutation_headers(manager["csrf_token"])
        )
        assert archived.status_code == 200
        permissions = (await context(pm, pid)).json()["data"]["permissions"]
        assert permissions["can_invite"] is False
        assert permissions["can_manage_settings"] is False


@pytest.mark.asyncio
async def test_allowed_domains_limit_candidates_invitations_and_accepting(
    harness: Harness,
) -> None:
    async with harness.client() as pm, harness.client() as inside, harness.client() as later:
        manager = await login(harness, pm, uid="pm", email="pm@allowed.example.com")
        insider = await login(harness, inside, uid="in", email="In@Allowed.Example.com")
        late = await login(harness, later, uid="late", email="late@example.com")
        for uid, email in (
            ("sub", "sub@sub.allowed.example.com"),
            ("fake", "fake@evil-allowed.example.com"),
            ("plain", "plain@example.com"),
        ):
            async with harness.client() as other:
                await login(harness, other, uid=uid, email=email)
        project = await create_project(pm, manager)
        pid = project["id"]
        # Invited before the allowlist was set.
        early = await invite_member(pm, manager, pid, "late@example.com", "researcher")

        await save(pm, manager, pid, "access", domains=["Allowed.Example.com"])
        emails = [
            item["email"]
            for item in (await pm.get(f"{PROJECTS}/{pid}/invite-candidates")).json()["data"]
        ]
        assert [email.lower() for email in emails] == ["in@allowed.example.com"]

        sent_before = len(harness.emails.sent)
        for email in (
            "plain@example.com",
            "sub@sub.allowed.example.com",
            "fake@evil-allowed.example.com",
        ):
            assert code(await invite(pm, manager, pid, email, "researcher")) == (
                403,
                "INVITE_DOMAIN_NOT_ALLOWED",
            )
        assert len(harness.emails.sent) == sent_before
        async with harness.factory() as db:
            pending = await db.scalar(
                select(func.count())
                .select_from(ProjectMembership)
                .where(ProjectMembership.project_id == UUID(pid))
            )
        assert pending == 2  # the manager and the early invitation only
        sent = await invite(pm, manager, pid, "in@allowed.example.com", "reviewer")
        assert sent.status_code == 201, sent.text
        assert (
            await accept_invitation(inside, insider, sent.json()["data"]["id"])
        ).status_code == 200

        # An invitation sent before the allowlist cannot be accepted, nor sent again.
        assert code(await accept_invitation(later, late, early["id"])) == (
            403,
            "INVITE_DOMAIN_NOT_ALLOWED",
        )
        async with harness.factory() as db:
            await db.execute(
                update(ProjectMembership)
                .where(ProjectMembership.id == UUID(early["id"]))
                .values(invite_expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
            await db.commit()
        resent = await pm.post(
            f"{PROJECTS}/{pid}/members/{early['id']}/invite",
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert code(resent) == (403, "INVITE_DOMAIN_NOT_ALLOWED")

        # Emptying the list allows any domain again.
        await save(pm, manager, pid, "access", domains=[])
        assert (await invite(pm, manager, pid, "plain@example.com", "reviewer")).status_code == 201


@pytest.mark.asyncio
async def test_invitation_context_reports_the_effective_lifetime(harness: Harness) -> None:
    async with harness.client() as pm:
        manager = await login(harness, pm, uid="pm", email="pm@example.com")
        project = await create_project(pm, manager)
        await save(pm, manager, project["id"], "access", expiry_days="3")
        invitation = (await context(pm, project["id"])).json()["data"]["invitation"]
        ceiling = harness.settings.project_invite_ttl_hours
        assert invitation["configured_expiry_days"] == "3"
        assert invitation["effective_ttl_hours"] == min(72, ceiling)
        assert invitation["allowed_domains"] == []


@pytest.mark.asyncio
async def test_unreadable_file_types_cannot_be_saved(harness: Harness) -> None:
    async with harness.client() as pm:
        manager = await login(harness, pm, uid="pm", email="pm@example.com")
        project = await create_project(pm, manager)
        response = await pm.patch(
            f"{PROJECTS}/{project['id']}/settings/data",
            json={"version": 0, "values": {"types": ["CSV", "Parquet"]}},
            headers=mutation_headers(manager["csrf_token"]),
        )
        assert code(response) == (422, "SETTING_UNSUPPORTED")
