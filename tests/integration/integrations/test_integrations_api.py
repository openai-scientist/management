import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from management.infra.secret_box import SecretBox
from management.integrations.catalog import MAX_INTEGRATIONS_PER_PROJECT
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from tests.helpers.audit import audit_events, every_audit_detail
from tests.helpers.harness import CONNECTION_KEY, ORIGIN, Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import MISSING, PROJECTS, add_member, code, create_project

SLACK = "https://hooks.slack.com/services/T000/B000/secretTOKEN9876"
SLACK_2 = "https://hooks.slack.com/services/T000/B111/otherTOKEN5555"
DISCORD = "https://discord.com/api/webhooks/123456789/discordSECRETabcd"
ALL_EVENTS = ["run.awaiting_review", "run.finished", "run.commented", "member.joined"]


def body(**fields: object) -> dict:
    return {
        "provider": "slack",
        "name": "Team channel",
        "webhook_url": SLACK,
        "events": ["run.finished"],
        **fields,
    }


async def stored(harness: Harness, integration_id: str) -> ProjectIntegration:
    async with harness.factory() as db:
        row = await db.get(ProjectIntegration, UUID(integration_id))
        assert row is not None
        return row


@pytest.mark.asyncio
async def test_catalog_lists_providers_events_and_limit(harness: Harness) -> None:
    async with harness.client() as client:
        assert (await client.get("/api/v1/integrations/catalog")).status_code == 401
        await login(harness, client, uid="owner", email="owner@example.com")
        data = (await client.get("/api/v1/integrations/catalog")).json()["data"]
    assert [provider["key"] for provider in data["providers"]] == ["slack", "teams", "discord"]
    assert all(provider["setup_steps"] for provider in data["providers"])
    assert [event["key"] for event in data["events"]] == ALL_EVENTS
    assert all(event["default"] for event in data["events"])
    assert data["limits"] == {"max_per_project": MAX_INTEGRATIONS_PER_PROJECT}


@pytest.mark.asyncio
async def test_manager_adds_changes_and_removes_without_ever_seeing_the_url(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        base = f"{PROJECTS}/{project['id']}/integrations"
        headers = mutation_headers(session["csrf_token"])

        assert (await client.post(base, json=body(), headers={"Origin": ORIGIN})).status_code == 403
        created = await client.post(
            base,
            json=body(name="  Team channel ", events=["run.finished", "run.awaiting_review"] * 2),
            headers=headers,
        )
        assert created.status_code == 201, created.text
        item = created.json()["data"]
        assert item["name"] == "Team channel"
        assert item["status"] == "active"
        assert item["url_hint"] == "hooks.slack.com/…9876"
        # Catalog order, no repeats.
        assert item["events"] == ["run.awaiting_review", "run.finished"]
        assert item["last_error"] is None
        assert "secretTOKEN" not in created.text and "webhook_url" not in item
        row = await stored(harness, item["id"])
        assert SecretBox(CONNECTION_KEY).open(row.secret_ciphertext) == {"url": SLACK}
        assert row.config_version == 1
        url = f"{base}/{item['id']}"

        listed = await client.get(base)
        assert [entry["id"] for entry in listed.json()["data"]] == [item["id"]]
        assert (await client.get(url)).json()["data"]["id"] == item["id"]

        assert code(await client.post(base, json=body(name="TEAM CHANNEL"), headers=headers)) == (
            409,
            "INTEGRATION_NAME_TAKEN",
        )
        assert code(await client.post(base, json=body(webhook_url=DISCORD), headers=headers)) == (
            422,
            "INTEGRATION_URL_INVALID",
        )
        for events in ([], ["run.started"]):
            assert code(await client.post(base, json=body(events=events), headers=headers)) == (
                422,
                "INTEGRATION_EVENTS_REQUIRED",
            )
        assert code(await client.patch(url, json={}, headers=headers)) == (422, "EMPTY_UPDATE")
        assert (
            await client.patch(url, json={"provider": "discord"}, headers=headers)
        ).status_code == 422

        paused = await client.patch(url, json={"enabled": False, "name": "Lab"}, headers=headers)
        assert paused.status_code == 200, paused.text
        assert paused.json()["data"]["status"] == "paused"
        assert paused.json()["data"]["name"] == "Lab"
        assert (await stored(harness, item["id"])).config_version == 1

        moved = await client.patch(
            url, json={"webhook_url": SLACK_2, "enabled": True}, headers=headers
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["data"]["status"] == "active"
        assert moved.json()["data"]["url_hint"] == "hooks.slack.com/…5555"
        row = await stored(harness, item["id"])
        assert SecretBox(CONNECTION_KEY).open(row.secret_ciphertext) == {"url": SLACK_2}
        assert row.config_version == 2

        removed = await client.delete(url, headers=headers)
        assert removed.status_code == 200, removed.text
        assert code(await client.get(url)) == (404, "INTEGRATION_NOT_FOUND")
        assert code(await client.get(f"{base}/{MISSING}")) == (404, "INTEGRATION_NOT_FOUND")

    for action in ("integration.created", "integration.updated", "integration.deleted"):
        assert await audit_events(harness, action), action
    details = await every_audit_detail(harness)
    assert "secretTOKEN" not in details and "otherTOKEN" not in details


@pytest.mark.asyncio
async def test_new_url_or_turning_on_clears_an_error(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        base = f"{PROJECTS}/{project['id']}/integrations"
        headers = mutation_headers(session["csrf_token"])
        item = (await client.post(base, json=body(), headers=headers)).json()["data"]
        url = f"{base}/{item['id']}"

        async def break_it() -> None:
            async with harness.factory() as db:
                row = await db.get(ProjectIntegration, UUID(item["id"]))
                row.status, row.consecutive_failures = "error", 5
                row.last_error_code, row.last_error_at = "WEBHOOK_NOT_FOUND", datetime.now(UTC)
                await db.commit()

        await break_it()
        shown = (await client.get(url)).json()["data"]
        assert shown["status"] == "error"
        assert shown["last_error"]["code"] == "WEBHOOK_NOT_FOUND"
        assert "no longer exists" in shown["last_error"]["message"]

        # Renaming alone leaves the error in place.
        renamed = (await client.patch(url, json={"name": "Other"}, headers=headers)).json()
        assert renamed["data"]["status"] == "error"

        fixed = (await client.patch(url, json={"webhook_url": SLACK_2}, headers=headers)).json()
        assert fixed["data"]["status"] == "active"
        assert fixed["data"]["last_error"] is None
        row = await stored(harness, item["id"])
        assert (row.consecutive_failures, row.config_version) == (0, 2)

        await break_it()
        on = (await client.patch(url, json={"enabled": True}, headers=headers)).json()
        assert on["data"]["status"] == "active"
        assert (await stored(harness, item["id"])).config_version == 3

        # A new URL while paused keeps it paused.
        await client.patch(url, json={"enabled": False}, headers=headers)
        still = (await client.patch(url, json={"webhook_url": SLACK}, headers=headers)).json()
        assert still["data"]["status"] == "paused"


@pytest.mark.asyncio
async def test_only_managers_change_and_members_read(harness: Harness) -> None:
    async with (
        harness.client() as manager_client,
        harness.client() as researcher_client,
        harness.client() as reviewer_client,
        harness.client() as outsider_client,
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
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        await add_member(manager_client, manager, project["id"], "reviewer@example.com", "reviewer")
        base = f"{PROJECTS}/{project['id']}/integrations"
        created = await manager_client.post(
            base, json=body(), headers=mutation_headers(manager["csrf_token"])
        )
        url = f"{base}/{created.json()['data']['id']}"

        for client, session in ((researcher_client, researcher), (reviewer_client, reviewer)):
            headers = mutation_headers(session["csrf_token"])
            listed = await client.get(base)
            assert listed.status_code == 200
            assert listed.json()["data"][0]["url_hint"] == "hooks.slack.com/…9876"
            assert (await client.get(url)).status_code == 200
            for denied in (
                await client.post(base, json=body(name="Mine"), headers=headers),
                await client.patch(url, json={"name": "Mine"}, headers=headers),
                await client.delete(url, headers=headers),
            ):
                assert code(denied) == (403, "ROLE_REQUIRED")

        headers = mutation_headers(outsider["csrf_token"])
        for response in (
            await outsider_client.get(base),
            await outsider_client.get(url),
            await outsider_client.post(base, json=body(name="Mine"), headers=headers),
            await outsider_client.patch(url, json={"name": "Mine"}, headers=headers),
            await outsider_client.delete(url, headers=headers),
        ):
            assert code(response) == (404, "NOT_FOUND")


@pytest.mark.asyncio
async def test_another_projects_integration_is_not_found(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        headers = mutation_headers(session["csrf_token"])
        first = await create_project(client, session)
        second = await create_project(client, session, name="Study B")
        created = await client.post(
            f"{PROJECTS}/{first['id']}/integrations", json=body(), headers=headers
        )
        other = f"{PROJECTS}/{second['id']}/integrations/{created.json()['data']['id']}"
        assert code(await client.get(other)) == (404, "INTEGRATION_NOT_FOUND")
        assert code(await client.patch(other, json={"name": "X"}, headers=headers)) == (
            404,
            "INTEGRATION_NOT_FOUND",
        )
        assert code(await client.delete(other, headers=headers)) == (404, "INTEGRATION_NOT_FOUND")


@pytest.mark.asyncio
async def test_limit_per_project(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        base = f"{PROJECTS}/{project['id']}/integrations"
        headers = mutation_headers(session["csrf_token"])
        for number in range(MAX_INTEGRATIONS_PER_PROJECT):
            response = await client.post(base, json=body(name=f"Channel {number}"), headers=headers)
            assert response.status_code == 201, response.text
        assert code(await client.post(base, json=body(name="One more"), headers=headers)) == (
            409,
            "INTEGRATION_LIMIT_REACHED",
        )


@pytest.mark.asyncio
async def test_archived_project_is_read_only(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        base = f"{PROJECTS}/{project['id']}/integrations"
        headers = mutation_headers(session["csrf_token"])
        created = (await client.post(base, json=body(), headers=headers)).json()["data"]
        archived = await client.post(f"{PROJECTS}/{project['id']}/archive", headers=headers)
        assert archived.status_code == 200, archived.text
        assert (await client.get(base)).status_code == 200
        for response in (
            await client.post(base, json=body(name="New"), headers=headers),
            await client.patch(f"{base}/{created['id']}", json={"name": "X"}, headers=headers),
            await client.delete(f"{base}/{created['id']}", headers=headers),
        ):
            assert code(response) == (409, "PROJECT_ARCHIVED")


@pytest.mark.asyncio
async def test_removing_an_integration_or_project_removes_its_deliveries(
    harness: Harness,
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        headers = mutation_headers(session["csrf_token"])
        project = await create_project(client, session)
        base = f"{PROJECTS}/{project['id']}/integrations"
        ids = [
            (await client.post(base, json=body(name=name), headers=headers)).json()["data"]["id"]
            for name in ("One", "Two")
        ]
        async with harness.factory() as db:
            for integration_id in ids:
                db.add(
                    IntegrationDelivery(
                        integration_id=UUID(integration_id),
                        event_id=uuid4(),
                        event_key="run.finished",
                        ref={"run_id": str(uuid4())},
                        config_version=1,
                    )
                )
            await db.commit()

        assert (await client.delete(f"{base}/{ids[0]}", headers=headers)).status_code == 200
        async with harness.factory() as db:
            left = list(await db.scalars(select(IntegrationDelivery.integration_id)))
        assert left == [UUID(ids[1])]

        deleted = await client.delete(f"{PROJECTS}/{project['id']}", headers=headers)
        assert deleted.status_code == 200, deleted.text
        async with harness.factory() as db:
            assert list(await db.scalars(select(IntegrationDelivery))) == []
            assert list(await db.scalars(select(ProjectIntegration))) == []


def test_body_helper_is_json() -> None:
    json.dumps(body())
