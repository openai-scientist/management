import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import update

from management.audit.models import AuditEvent
from management.integrations.models import ProjectIntegration
from management.integrations.sender import SendResult
from management.projects.models import ProjectMembership
from tests.helpers.audit import audit_events, every_audit_detail
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import MISSING, PROJECTS, add_member, code, create_project

SLACK = "https://hooks.slack.com/services/T000/B000/secretTOKEN9876"
DISCORD = "https://discord.com/api/webhooks/123456789/discordSECRETabcd"


async def setup(harness: Harness, client, *, name: str = "Study A"):
    session = await login(harness, client, uid="owner", email="owner@example.com")
    project = await create_project(client, session, name=name)
    return session, project, f"{PROJECTS}/{project['id']}/integrations"


@pytest.mark.asyncio
async def test_draft_test_posts_a_message_that_does_not_name_the_project(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session, project, base = await setup(harness, client, name="Secret Study")
        headers = mutation_headers(session["csrf_token"])
        response = await client.post(
            f"{base}/test", json={"provider": "discord", "webhook_url": DISCORD}, headers=headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"] == {"ok": True, "error": None}
        [(provider, url, payload)] = harness.webhooks.sent
        assert (provider, url) == ("discord", DISCORD)
        assert payload["allowed_mentions"] == {"parse": []}
        assert "Secret Study" not in str(payload)

        bad = await client.post(
            f"{base}/test", json={"provider": "slack", "webhook_url": DISCORD}, headers=headers
        )
        assert code(bad) == (422, "INTEGRATION_URL_INVALID")
        assert len(harness.webhooks.sent) == 1

    [event] = await audit_events(harness, "integration.tested")
    assert event.details == {
        "provider": "discord",
        "url_sha256": hashlib.sha256(DISCORD.encode()).hexdigest(),
    }
    assert "discordSECRET" not in await every_audit_detail(harness)


@pytest.mark.asyncio
async def test_saved_test_reports_failure_without_touching_the_integration(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session, project, base = await setup(harness, client)
        headers = mutation_headers(session["csrf_token"])
        created = await client.post(
            base,
            json={
                "provider": "slack",
                "name": "Team <!channel>",
                "webhook_url": SLACK,
                "events": ["run.finished"],
            },
            headers=headers,
        )
        integration_id = created.json()["data"]["id"]
        async with harness.factory() as db:
            row = await db.get(ProjectIntegration, UUID(integration_id))
            row.status, row.consecutive_failures, row.last_error_code = "error", 5, "PROVIDER_ERROR"
            await db.commit()

        harness.webhooks.result = SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
        failed = await client.post(f"{base}/{integration_id}/test", headers=headers)
        assert failed.status_code == 200, failed.text
        assert failed.json()["data"]["ok"] is False
        assert failed.json()["data"]["error"]["code"] == "WEBHOOK_NOT_FOUND"
        assert failed.json()["data"]["error"]["message"]

        harness.webhooks.result = SendResult("sent", 200)
        passed = await client.post(f"{base}/{integration_id}/test", headers=headers)
        assert passed.json()["data"] == {"ok": True, "error": None}

        [_, (provider, url, payload)] = harness.webhooks.sent
        assert (provider, url) == ("slack", SLACK)
        assert "Study A" in payload["text"]
        assert "&lt;!channel&gt;" in json.dumps(payload["attachments"])
        async with harness.factory() as db:
            row = await db.get(ProjectIntegration, UUID(integration_id))
            # A passing test does not turn an integration in error back on.
            assert (row.status, row.consecutive_failures, row.last_error_code) == (
                "error",
                5,
                "PROVIDER_ERROR",
            )
        assert code(await client.post(f"{base}/{MISSING}/test", headers=headers)) == (
            404,
            "INTEGRATION_NOT_FOUND",
        )


@pytest.mark.asyncio
async def test_five_tests_a_minute_per_user(harness: Harness) -> None:
    async with harness.client() as client:
        session, _project, base = await setup(harness, client)
        headers = mutation_headers(session["csrf_token"])
        second = await create_project(client, session, name="Study B")
        body = {"provider": "slack", "webhook_url": SLACK}
        for _ in range(5):
            assert (
                await client.post(f"{base}/test", json=body, headers=headers)
            ).status_code == 200
        # The user's limit holds in another project too.
        other = f"{PROJECTS}/{second['id']}/integrations/test"
        assert code(await client.post(other, json=body, headers=headers)) == (
            429,
            "INTEGRATION_TEST_RATE_LIMITED",
        )
        assert len(harness.webhooks.sent) == 5


@pytest.mark.asyncio
async def test_five_tests_a_minute_per_project(harness: Harness) -> None:
    async with harness.client() as owner_client, harness.client() as other_client:
        owner, project, base = await setup(harness, owner_client)
        other = await login(harness, other_client, uid="other", email="other@example.com")
        await add_member(owner_client, owner, project["id"], "other@example.com", "researcher")
        async with harness.factory() as db:
            await db.execute(
                update(ProjectMembership)
                .where(ProjectMembership.user_id == UUID(other["user"]["id"]))
                .values(role_code="project_manager")
            )
            await db.commit()
        body = {"provider": "slack", "webhook_url": SLACK}
        for _ in range(3):
            response = await owner_client.post(
                f"{base}/test", json=body, headers=mutation_headers(owner["csrf_token"])
            )
            assert response.status_code == 200
        other_headers = mutation_headers(other["csrf_token"])
        for _ in range(2):
            response = await other_client.post(f"{base}/test", json=body, headers=other_headers)
            assert response.status_code == 200, response.text
        assert code(await other_client.post(f"{base}/test", json=body, headers=other_headers)) == (
            429,
            "INTEGRATION_TEST_RATE_LIMITED",
        )


@pytest.mark.asyncio
async def test_only_managers_send_tests(harness: Harness) -> None:
    async with (
        harness.client() as owner_client,
        harness.client() as researcher_client,
        harness.client() as outsider_client,
    ):
        owner, project, base = await setup(harness, owner_client)
        researcher = await login(
            harness, researcher_client, uid="researcher", email="researcher@example.com"
        )
        outsider = await login(harness, outsider_client, uid="out", email="out@example.com")
        await add_member(owner_client, owner, project["id"], "researcher@example.com", "researcher")
        body = {"provider": "slack", "webhook_url": SLACK}
        denied = await researcher_client.post(
            f"{base}/test", json=body, headers=mutation_headers(researcher["csrf_token"])
        )
        assert code(denied) == (403, "ROLE_REQUIRED")
        hidden = await outsider_client.post(
            f"{base}/test", json=body, headers=mutation_headers(outsider["csrf_token"])
        )
        assert code(hidden) == (404, "NOT_FOUND")
        assert harness.webhooks.sent == []
        assert await audit_events(harness, "integration.tested") == []


@pytest.mark.asyncio
async def test_thirty_tests_a_day_per_project(harness: Harness) -> None:
    async with harness.client() as client:
        session, project, base = await setup(harness, client)
        earlier = datetime.now(UTC) - timedelta(hours=2)
        async with harness.factory() as db:
            for _ in range(30):
                db.add(
                    AuditEvent(
                        action="integration.tested",
                        resource_type="project_integration",
                        resource_id=project["id"],
                        project_id=UUID(project["id"]),
                        details={},
                        created_at=earlier,
                    )
                )
            await db.commit()
        response = await client.post(
            f"{base}/test",
            json={"provider": "slack", "webhook_url": SLACK},
            headers=mutation_headers(session["csrf_token"]),
        )
        assert code(response) == (429, "INTEGRATION_TEST_RATE_LIMITED")
