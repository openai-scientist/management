from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from management.audit.models import AuditEvent
from management.identity.bootstrap_admin import bootstrap_admin
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project


@pytest.mark.asyncio
async def test_manager_and_admin_read_a_safe_feed_and_others_cannot(harness: Harness) -> None:
    async with (
        harness.client() as manager_client,
        harness.client() as researcher_client,
        harness.client() as outsider_client,
        harness.client() as admin_client,
    ):
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        await login(harness, researcher_client, uid="researcher", email="researcher@example.com")
        await login(harness, outsider_client, uid="outsider", email="outsider@example.com")
        await login(harness, admin_client, uid="admin", email="admin@example.com")
        await bootstrap_admin(
            "admin@example.com", settings=harness.settings, session_factory=harness.factory
        )
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        headers = mutation_headers(manager["csrf_token"])
        saved = await manager_client.patch(
            f"{PROJECTS}/{project['id']}/settings/spending",
            json={"version": 0, "values": {"limit": "250", "billing_email": "fin@example.com"}},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        url = f"{PROJECTS}/{project['id']}/activity"

        response = await manager_client.get(url)
        assert response.status_code == 200, response.text
        body = response.json()
        items = body["data"]
        assert body["meta"]["pagination"] == {"total": len(items), "limit": 50, "offset": 0}
        assert [item["action"] for item in items] == [
            "settings.updated",
            "member.joined",
            "member.invited",
            # The creator joins with the project.
            "member.joined",
        ]
        settings_item = items[0]
        assert settings_item["category"] == "settings"
        assert settings_item["actor"]["display_name"] == "Manager"
        assert settings_item["resource"] == {
            "type": "project",
            "id": project["id"],
            "display_name": project["name"],
            "available": True,
        }
        # The private contact is named as changed; its value never leaves the audit log.
        assert sorted(settings_item["changes"], key=lambda c: c["field"]) == [
            {"field": "spending.billing_email", "before": None, "after": None},
            {"field": "spending.limit", "before": "100", "after": "250"},
        ]
        assert "fin@example.com" not in response.text
        assert "details" not in settings_item and "request_id" not in settings_item
        joined = items[1]
        assert joined["category"] == "members"
        assert joined["resource"]["type"] == "user"
        assert joined["resource"]["display_name"] == "Researcher"

        members = (await manager_client.get(url, params={"category": "members"})).json()
        assert members["meta"]["pagination"]["total"] == 3
        page = (await manager_client.get(url, params={"limit": 1, "offset": 1})).json()
        assert [item["id"] for item in page["data"]] == [items[1]["id"]]
        assert page["meta"]["pagination"]["total"] == 4

        assert (await admin_client.get(url)).status_code == 200
        assert code(await researcher_client.get(url)) == (403, "ROLE_REQUIRED")
        assert code(await outsider_client.get(url)) == (404, "NOT_FOUND")

        archived = await manager_client.post(f"{PROJECTS}/{project['id']}/archive", headers=headers)
        assert archived.status_code == 200, archived.text
        after = (await manager_client.get(url)).json()["data"]
        assert after[0]["action"] == "project.archived"


@pytest.mark.asyncio
async def test_time_filters_need_a_timezone_and_an_ordered_range(harness: Harness) -> None:
    async with harness.client() as client:
        manager = await login(harness, client, uid="manager", email="manager@example.com")
        project = await create_project(client, manager)
        url = f"{PROJECTS}/{project['id']}/activity"
        assert code(await client.get(url, params={"from": "2026-10-01T00:00:00"})) == (
            422,
            "TIMEZONE_REQUIRED",
        )
        same = "2026-10-01T00:00:00Z"
        assert code(await client.get(url, params={"from": same, "to": same})) == (
            422,
            "INVALID_TIME_RANGE",
        )
        assert code(await client.get(url, params={"category": "billing"}))[0] == 422


@pytest.mark.asyncio
async def test_gone_resources_system_events_and_other_projects(harness: Harness) -> None:
    async with harness.client() as client:
        manager = await login(harness, client, uid="manager", email="manager@example.com")
        project = await create_project(client, manager)
        other = await create_project(client, manager, name="Study B")
        project_id, other_id = UUID(project["id"]), UUID(other["id"])
        now = datetime.now(UTC)
        run_id, dataset_id = uuid4(), uuid4()
        async with harness.factory() as db:
            db.add_all(
                [
                    AuditEvent(
                        action="dataset.deleted",
                        resource_type="dataset",
                        resource_id=str(dataset_id),
                        project_id=project_id,
                        details={"name": "Churn 2025", "versions": 2},
                        created_at=now + timedelta(seconds=1),
                    ),
                    AuditEvent(
                        action="run.status_changed",
                        resource_type="run",
                        resource_id=str(run_id),
                        project_id=project_id,
                        details={"before": "running", "after": "completed"},
                        created_at=now + timedelta(seconds=2),
                    ),
                    AuditEvent(
                        action="project.member_revoked",
                        resource_type="project_membership",
                        resource_id=str(uuid4()),
                        project_id=project_id,
                        details={"user_id": str(uuid4())},
                        created_at=now + timedelta(seconds=3),
                    ),
                    # Not part of the feed: a preview of live data.
                    AuditEvent(
                        action="connection.previewed",
                        resource_type="data_connection",
                        resource_id=str(uuid4()),
                        project_id=project_id,
                        details={"host": "db.internal"},
                        created_at=now + timedelta(seconds=4),
                    ),
                    # Another project's event, with an id from this project.
                    AuditEvent(
                        action="dataset.deleted",
                        resource_type="dataset",
                        resource_id=str(dataset_id),
                        project_id=other_id,
                        details={"name": "Secret set"},
                        created_at=now + timedelta(seconds=5),
                    ),
                ]
            )
            await db.commit()

        response = await client.get(
            f"{PROJECTS}/{project['id']}/activity", params={"from": now.isoformat()}
        )
        items = response.json()["data"]
        assert [item["action"] for item in items] == [
            "member.removed",
            "run.completed",
            "dataset.deleted",
        ]
        removed, completed, deleted = items
        assert removed["resource"]["display_name"] == "Deleted user"
        assert removed["resource"]["available"] is False
        assert completed["actor"] == {
            "user_id": None,
            "display_name": "System",
            "avatar_url": None,
            "type": "system",
        }
        assert completed["resource"]["display_name"] == "Deleted research run"
        assert deleted["resource"] == {
            "type": "dataset",
            "id": str(dataset_id),
            "display_name": "Churn 2025",
            "available": False,
        }
        assert "Secret set" not in response.text and "db.internal" not in response.text
