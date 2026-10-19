from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select

from management.integrations.models import IntegrationDelivery
from management.research.models import ResearchRun
from tests.helpers.harness import Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, create_project
from tests.helpers.research import REVIEW, ready_project, report, start_run

SLACK = "https://hooks.slack.com/services/T000/B000/secretTOKEN9876"
ALL_EVENTS = ["run.awaiting_review", "run.finished", "run.commented", "member.joined"]


async def connect(client, session, project_id: str, *, events=ALL_EVENTS, name="Team") -> str:
    response = await client.post(
        f"{PROJECTS}/{project_id}/integrations",
        json={"provider": "slack", "name": name, "webhook_url": SLACK, "events": events},
        headers=mutation_headers(session["csrf_token"]),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


async def deliveries(harness: Harness) -> list[IntegrationDelivery]:
    async with harness.factory() as db:
        return list(
            await db.scalars(select(IntegrationDelivery).order_by(IntegrationDelivery.created_at))
        )


@pytest.mark.asyncio
async def test_run_review_and_finish_queue_one_delivery_each(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project, version = await ready_project(client, session)
        integration_id = await connect(client, session, project["id"])
        # Only the chosen events are posted.
        await connect(client, session, project["id"], events=["member.joined"], name="Members")
        run = (await start_run(client, session, project["id"], version["id"])).json()["data"]
        assert await deliveries(harness) == []

        await report(client, run["id"], status="awaiting_review", review=REVIEW)
        # The same status again is no transition.
        await report(client, run["id"], status="awaiting_review", review=REVIEW)
        await report(client, run["id"], status="completed", cost_usd="1.00")

    rows = await deliveries(harness)
    assert [(row.event_key, row.ref["status"]) for row in rows] == [
        ("run.awaiting_review", "awaiting_review"),
        ("run.finished", "completed"),
    ]
    assert {row.integration_id for row in rows} == {UUID(integration_id)}
    assert all(row.status == "pending" and row.config_version == 1 for row in rows)
    assert all(set(row.ref) == {"run_id", "status"} for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "stopped", "budget_exceeded"])
async def test_every_ending_posts_run_finished_from_any_code_path(
    harness: Harness, status: str
) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        await connect(client, session, project["id"])
    async with harness.factory() as db:
        run = ResearchRun(
            project_id=UUID(project["id"]),
            created_by_user_id=UUID(session["user"]["id"]),
            topic="T",
            status="running",
            budget_usd=Decimal("5"),
        )
        db.add(run)
        await db.commit()
        run.status = "paused"
        await db.commit()
        run.status = status
        await db.commit()
    assert [(row.event_key, row.ref["status"]) for row in await deliveries(harness)] == [
        ("run.finished", status)
    ]


@pytest.mark.asyncio
async def test_rollback_queues_nothing(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        await connect(client, session, project["id"])
    async with harness.factory() as db:
        run = ResearchRun(
            project_id=UUID(project["id"]),
            created_by_user_id=UUID(session["user"]["id"]),
            status="running",
            budget_usd=Decimal("5"),
        )
        db.add(run)
        await db.commit()
        run.status = "completed"
        await db.flush()
        await db.rollback()
    assert await deliveries(harness) == []


@pytest.mark.asyncio
async def test_paused_integration_gets_nothing_and_no_catch_up(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project, version = await ready_project(client, session)
        integration_id = await connect(client, session, project["id"])
        await client.patch(
            f"{PROJECTS}/{project['id']}/integrations/{integration_id}",
            json={"enabled": False},
            headers=mutation_headers(session["csrf_token"]),
        )
        run = (await start_run(client, session, project["id"], version["id"])).json()["data"]
        await report(client, run["id"], status="completed", cost_usd="1.00")
    assert await deliveries(harness) == []


@pytest.mark.asyncio
async def test_first_comment_per_discussion_only(harness: Harness) -> None:
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project, version = await ready_project(client, session)
        await connect(client, session, project["id"], events=["run.commented"])
        run = (await start_run(client, session, project["id"], version["id"])).json()["data"]
        comments = f"{PROJECTS}/{project['id']}/runs/{run['id']}/comments"
        headers = mutation_headers(session["csrf_token"])
        first = await client.post(comments, json={"body": "First look"}, headers=headers)
        assert first.status_code == 201, first.text
        second = await client.post(comments, json={"body": "Another"}, headers=headers)
        assert second.status_code == 201, second.text
        removed = await client.delete(f"{comments}/{first.json()['data']['id']}", headers=headers)
        assert removed.status_code in (200, 204), removed.text
        again = await client.post(comments, json={"body": "Written again"}, headers=headers)
        assert again.status_code == 201, again.text

    [row] = await deliveries(harness)
    assert row.event_key == "run.commented"
    assert row.ref == {
        "run_id": run["id"],
        "comment_id": first.json()["data"]["id"],
        "artifact_id": None,
    }


@pytest.mark.asyncio
async def test_member_joining_is_posted_once(harness: Harness) -> None:
    async with harness.client() as client, harness.client() as newcomer:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        await login(harness, newcomer, uid="new", email="new@example.com")
        project = await create_project(client, session)
        await connect(client, session, project["id"])
        membership = await add_member(
            client, session, project["id"], "new@example.com", "researcher"
        )
    [row] = await deliveries(harness)
    assert row.event_key == "member.joined"
    assert row.ref["membership_id"] == membership["id"]
