import pytest
from sqlalchemy import select

from management.audit.models import AuditEvent
from management.identity.bootstrap_admin import bootstrap_admin
from management.projects.settings_service import effective_invite_ttl_hours
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project

# The web app's form defaults (`useDraft` initial values), in wire keys.
FRONTEND_DEFAULTS = {
    "general": {"visibility": "members", "timezone": "Asia/Ho_Chi_Minh", "language": "en"},
    "access": {
        "inviters": "managers",
        "default_role": "researcher",
        "expiry_days": "7",
        "domains": [],
        "join_link": False,
        "join_approval": True,
        "reviewers_see_data": True,
    },
    "runs": {
        "mode": "copilot",
        "budget": "5",
        "fields": [],
        "starters": "researchers",
        "concurrent": "2",
        "rounds": "3",
        "stage_limit": "60",
        "retry": True,
        "pause_on_failure": True,
    },
    "models": {
        "stage": "default",
        "review": "default",
        "fallback": "none",
        "effort": "medium",
        "style": "balanced",
    },
    "literature": {
        "arxiv": True,
        "semantic": True,
        "openalex": True,
        "pubmed": False,
        "crossref": False,
        "years": "10",
        "preprints": True,
        "per_round": "40",
        "languages": ["English"],
        "exclude": [],
    },
    "review": {
        "approvals": "1",
        "approvers": "reviewers",
        "self_approval": False,
        "format": "latex",
        "citation": "apa",
        "language": "en",
        "appendix": True,
        "watermark": True,
    },
    "data": {
        "pinning": "latest",
        "same_name": "version",
        "types": ["CSV", "Excel"],
        "pii": True,
        "mask": True,
        "artifacts": "forever",
        "trash": "30",
        "region": "auto",
    },
    "spending": {
        "capped": False,
        "limit": "100",
        "warn_at": "80",
        "at_limit": "stop",
        "billing_email": "",
    },
    "notifications": {
        "review": False,
        "finished": False,
        "comments": False,
        "members": False,
        "budget": False,
        "integrations": False,
    },
}


@pytest.mark.asyncio
async def test_settings_start_at_the_web_app_defaults_and_save_by_section(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="manager", email="manager@example.com")
        headers = mutation_headers(session["csrf_token"])
        project = await create_project(client, session)
        url = f"{PROJECTS}/{project['id']}/settings"

        fresh = (await client.get(url)).json()["data"]
        assert fresh["version"] == 0
        assert fresh["updated_at"] is None
        assert fresh["sections"] == FRONTEND_DEFAULTS
        assert fresh["capabilities"]["access.expiry_days"] == {
            "supported": True,
            "reason_code": None,
        }
        supported = {
            "access.inviters",
            "access.default_role",
            "access.expiry_days",
            "access.domains",
            "access.reviewers_see_data",
            "data.types",
            "data.same_name",
            "data.pii",
            "data.mask",
            "spending.capped",
            "spending.limit",
            "spending.warn_at",
            "spending.at_limit",
            "notifications.review",
            "notifications.finished",
            "notifications.comments",
            "notifications.members",
            "notifications.budget",
            "notifications.integrations",
        }
        assert {key for key, c in fresh["capabilities"].items() if c["supported"]} == supported
        assert fresh["capabilities"]["access.join_link"] == {
            "supported": False,
            "reason_code": "NOT_ENFORCED_YET",
        }

        saved = await client.patch(
            f"{url}/access",
            json={
                "version": 0,
                "values": {"expiry_days": "14", "domains": ["Example.com", "example.com"]},
            },
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["message"] == "Settings saved"
        data = saved.json()["data"]
        assert data["version"] == 1
        assert data["updated_by_user_id"] == session["user"]["id"]
        assert data["sections"]["access"] == {
            **FRONTEND_DEFAULTS["access"],
            "expiry_days": "14",
            "domains": ["example.com"],
        }

        # A second partial save keeps the first one; a list replaces the stored list.
        again = await client.patch(
            f"{url}/access",
            json={"version": 1, "values": {"join_link": True, "domains": []}},
            headers=headers,
        )
        assert again.status_code == 200, again.text
        reread = (await client.get(url)).json()["data"]
        assert reread["version"] == 2
        assert reread["sections"]["access"]["expiry_days"] == "14"
        assert reread["sections"]["access"]["join_link"] is True
        assert reread["sections"]["access"]["domains"] == []
        assert reread["sections"]["runs"] == FRONTEND_DEFAULTS["runs"]

        async with harness.factory() as db:
            from uuid import UUID

            assert await effective_invite_ttl_hours(
                db, UUID(project["id"]), harness.settings
            ) == min(14 * 24, harness.settings.project_invite_ttl_hours)
            events = (
                await db.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.action == "project.settings_updated")
                    .order_by(AuditEvent.created_at)
                )
            ).all()
        # Free-text fields are named in the log but their values are not recorded.
        assert events[0].details == {
            "section": "access",
            "changed_fields": ["domains", "expiry_days"],
            "values": {"expiry_days": {"from": "7", "to": "14"}},
        }
        assert events[1].details["changed_fields"] == ["domains", "join_link"]


@pytest.mark.asyncio
async def test_settings_reject_stale_versions_and_values_the_web_app_cannot_send(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="manager", email="manager@example.com")
        headers = mutation_headers(session["csrf_token"])
        project = await create_project(client, session)
        url = f"{PROJECTS}/{project['id']}/settings"

        async def patch(section: str, values: dict, version: int = 0):
            return await client.patch(
                f"{url}/{section}", json={"version": version, "values": values}, headers=headers
            )

        assert (await patch("runs", {"budget": "12.5"})).status_code == 200
        assert code(await patch("runs", {"budget": "8"})) == (409, "SETTINGS_VERSION_CONFLICT")
        unchanged = await patch("runs", {"budget": "12.5"}, version=1)
        assert unchanged.json()["data"]["version"] == 1

        invalid = await patch("runs", {"budget": "25"}, version=1)
        assert code(invalid) == (422, "VALIDATION_ERROR")
        assert invalid.json()["error"]["details"][0]["field"] == "body.values.budget"
        for section, values in (
            ("runs", {"budget": "1e1"}),
            ("spending", {"limit": " 5"}),
            ("access", {"expiry_days": "5"}),
            ("access", {"domains": ["@example.com"]}),
            ("general", {"timezone": "Mars/Base"}),
            ("models", {"fallback": "default"}),
            ("literature", {"arxiv": False, "semantic": False, "openalex": False}),
            ("literature", {"per_round": "500"}),
            ("data", {"types": []}),
            ("spending", {"billing_email": "not-an-email"}),
            ("review", {"unknown": True}),
        ):
            assert code(await patch(section, values, version=1)) == (422, "VALIDATION_ERROR"), (
                section,
                values,
            )
        assert code(await patch("general", {"visibility": "organization"}, version=1)) == (
            422,
            "SETTING_UNSUPPORTED",
        )
        assert code(await patch("runs", {}, version=1)) == (422, "EMPTY_UPDATE")
        assert code(await patch("billing", {"x": 1}, version=1)) == (
            404,
            "SETTINGS_SECTION_NOT_FOUND",
        )
        # Every value the web app offers is accepted.
        accepted = await patch(
            "models",
            {"stage": "gemini-2-5-pro", "fallback": "claude-haiku-5-5", "style": "exploratory"},
            version=1,
        )
        assert accepted.status_code == 200, accepted.text

        archived = await client.post(f"{PROJECTS}/{project['id']}/archive", headers=headers)
        assert archived.status_code == 200
        assert code(await patch("runs", {"retry": False}, version=2)) == (409, "PROJECT_ARCHIVED")
        assert (await client.get(url)).status_code == 200


@pytest.mark.asyncio
async def test_settings_follow_the_project_manager_permissions(harness: Harness) -> None:
    async with (
        harness.client() as manager_client,
        harness.client() as researcher_client,
        harness.client() as reviewer_client,
        harness.client() as outsider_client,
        harness.client() as admin_client,
    ):
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        reviewer = await login(
            harness, reviewer_client, uid="reviewer", email="reviewer@example.com"
        )
        outsider = await login(
            harness, outsider_client, uid="outsider", email="outsider@example.com"
        )
        admin = await login(harness, admin_client, uid="admin", email="admin@example.com")
        await bootstrap_admin(
            "admin@example.com", settings=harness.settings, session_factory=harness.factory
        )
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        await add_member(manager_client, manager, project["id"], "reviewer@example.com", "reviewer")
        url = f"{PROJECTS}/{project['id']}/settings"
        body = {"version": 0, "values": {"retry": False}}

        for client, session in ((researcher_client, researcher), (reviewer_client, reviewer)):
            assert code(await client.get(url)) == (403, "ROLE_REQUIRED")
            denied = await client.patch(
                f"{url}/runs", json=body, headers=mutation_headers(session["csrf_token"])
            )
            assert code(denied) == (403, "ROLE_REQUIRED")
        assert code(await outsider_client.get(url)) == (404, "NOT_FOUND")
        hidden = await outsider_client.patch(
            f"{url}/runs", json=body, headers=mutation_headers(outsider["csrf_token"])
        )
        assert code(hidden) == (404, "NOT_FOUND")

        assert (await admin_client.get(url)).status_code == 200
        by_admin = await admin_client.patch(
            f"{url}/runs", json=body, headers=mutation_headers(admin["csrf_token"])
        )
        assert by_admin.status_code == 200, by_admin.text

        # The project's audit log names the person who acted.
        audit = await manager_client.get("/api/v1/audit", params={"project_id": project["id"]})
        names = {item["action"]: item["actor_display_name"] for item in audit.json()["data"]}
        assert names["project.settings_updated"] == "Admin"
        assert names["project.created"] == "Manager"
