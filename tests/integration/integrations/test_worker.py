import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import select, update

from management.infra.secret_box import SecretBox
from management.integrations import worker
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.integrations.sender import SendResult
from management.projects.models import Notification
from management.research.models import ResearchRun
from tests.helpers.audit import audit_events
from tests.helpers.harness import APP_URL, CONNECTION_KEY, Harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, add_member, code, create_project

SLACK = "https://hooks.slack.com/services/T000/B000/secretTOKEN9876"
SLACK_2 = "https://hooks.slack.com/services/T000/B111/otherTOKEN5555"


class World:
    def __init__(self, harness: Harness, client, session, project, integration_id: str) -> None:
        self.harness, self.client, self.session = harness, client, session
        self.project, self.integration_id = project, UUID(integration_id)

    async def once(self) -> bool:
        return await worker.run_once(
            self.harness.factory,
            self.harness.webhooks,
            box=SecretBox(CONNECTION_KEY),
            app_url=APP_URL,
            hub=self.harness.app.state.notification_hub,
        )

    async def finish_run(self, status: str = "completed", topic: str | None = "Study time") -> UUID:
        async with self.harness.factory() as db:
            started = datetime.now(UTC) - timedelta(minutes=75)
            run = ResearchRun(
                project_id=UUID(self.project["id"]),
                created_by_user_id=UUID(self.session["user"]["id"]),
                topic=topic,
                status="running",
                budget_usd=Decimal("5"),
                started_at=started,
            )
            db.add(run)
            await db.commit()
            run.status, run.finished_at = status, started + timedelta(minutes=75)
            await db.commit()
            return run.id

    async def delivery(self) -> IntegrationDelivery:
        async with self.harness.factory() as db:
            return await db.scalar(
                select(IntegrationDelivery).order_by(IntegrationDelivery.created_at.desc())
            )

    async def integration(self) -> ProjectIntegration:
        async with self.harness.factory() as db:
            return await db.get(ProjectIntegration, self.integration_id)

    async def make_due(self) -> None:
        async with self.harness.factory() as db:
            await db.execute(
                update(IntegrationDelivery)
                .where(IntegrationDelivery.status == "pending")
                .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await db.commit()

    async def notices(self) -> int:
        async with self.harness.factory() as db:
            rows = await db.scalars(
                select(Notification).where(Notification.kind == "integration_failed")
            )
            return len(list(rows))


@pytest.fixture
async def world(harness: Harness):
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session, name="Lab")
        response = await client.post(
            f"{PROJECTS}/{project['id']}/integrations",
            json={
                "provider": "slack",
                "name": "Team",
                "webhook_url": SLACK,
                "events": ["run.finished", "run.awaiting_review"],
            },
            headers=mutation_headers(session["csrf_token"]),
        )
        yield World(harness, client, session, project, response.json()["data"]["id"])


def near(moment: datetime, delta: timedelta) -> bool:
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return abs(moment - (datetime.now(UTC) + delta)) < timedelta(seconds=10)


@pytest.mark.asyncio
async def test_sends_the_message_built_at_send_time(world: World) -> None:
    run_id = await world.finish_run()
    assert await world.once() is True
    assert await world.once() is False

    [(provider, url, payload)] = world.harness.webhooks.sent
    assert (provider, url) == ("slack", SLACK)
    [attachment] = payload["attachments"]
    blocks = attachment["blocks"]
    assert attachment["color"] == "#2EB67D"
    assert blocks[0]["text"]["text"] == "Run completed"
    assert blocks[1]["text"]["text"] == "*Study time*"
    assert [field["text"] for field in blocks[2]["fields"]] == [
        "*Project*\nLab",
        "*Duration*\n1 h 15 min",
    ]
    button = blocks[3]["elements"][0]
    assert button["url"] == f"{APP_URL}/projects/{world.project['id']}/experiments/runs/{run_id}"

    delivery = await world.delivery()
    assert (delivery.status, delivery.attempts, delivery.response_code) == ("sent", 0, 200)
    assert delivery.sent_at is not None and delivery.locked_until is None
    integration = await world.integration()
    assert integration.last_delivered_at is not None
    assert integration.consecutive_failures == 0


@pytest.mark.asyncio
async def test_run_without_topic_is_named_by_its_id(world: World) -> None:
    run_id = await world.finish_run("budget_exceeded", topic="  ")
    await world.once()
    payload = world.harness.webhooks.sent[0][2]
    assert payload["text"] == f"Run out of budget: Run {str(run_id)[:8]}"


@pytest.mark.asyncio
async def test_retries_after_1_5_and_30_minutes_then_fails(world: World) -> None:
    world.harness.webhooks.result = SendResult("retryable", 503, "PROVIDER_ERROR")
    await world.finish_run()
    for attempts, delay in ((1, 1), (2, 5), (3, 30)):
        await world.make_due()
        await world.once()
        delivery = await world.delivery()
        assert (delivery.status, delivery.attempts) == ("pending", attempts)
        assert near(delivery.next_attempt_at, timedelta(minutes=delay))
        # Not due yet: nothing to do.
        assert await world.once() is False
        assert (await world.integration()).consecutive_failures == 0
    await world.make_due()
    await world.once()
    delivery = await world.delivery()
    assert (delivery.status, delivery.attempts, delivery.error_code) == (
        "failed",
        4,
        "PROVIDER_ERROR",
    )
    integration = await world.integration()
    assert (integration.status, integration.consecutive_failures) == ("active", 1)
    assert integration.last_error_code == "PROVIDER_ERROR"
    assert len(world.harness.webhooks.sent) == 4


@pytest.mark.asyncio
async def test_rate_limit_waits_without_using_an_attempt(world: World) -> None:
    world.harness.webhooks.result = SendResult("rate_limited", 429, "PROVIDER_RATE_LIMITED", 120)
    await world.finish_run()
    await world.once()
    delivery = await world.delivery()
    assert (delivery.status, delivery.attempts) == ("pending", 0)
    assert near(delivery.next_attempt_at, timedelta(seconds=120))
    assert (await world.integration()).consecutive_failures == 0

    # After an hour of being told to wait, the delivery gives up.
    async with world.harness.factory() as db:
        await db.execute(
            update(IntegrationDelivery).values(
                created_at=datetime.now(UTC) - timedelta(hours=2),
                next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await db.commit()
    await world.once()
    assert (await world.delivery()).status == "failed"


@pytest.mark.asyncio
async def test_gone_webhook_stops_the_integration_and_tells_managers_once(
    world: World,
) -> None:
    harness = world.harness
    async with harness.client() as reviewer_client:
        await login(harness, reviewer_client, uid="rev", email="rev@example.com")
        await add_member(
            world.client, world.session, world.project["id"], "rev@example.com", "reviewer"
        )
    harness.webhooks.result = SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
    await world.finish_run()
    await world.finish_run("failed")

    owner_id = UUID(world.session["user"]["id"])
    async with harness.app.state.notification_hub.subscribe(owner_id) as queue:
        await world.once()
        # The notification reaches the open stream as soon as the outcome is committed.
        assert await asyncio.wait_for(queue.get(), timeout=1) is True

    integration = await world.integration()
    assert (integration.status, integration.last_error_code) == ("error", "WEBHOOK_NOT_FOUND")
    # Only the Project Manager hears, not the reviewer.
    assert await world.notices() == 1
    assert len(await audit_events(harness, "integration.auto_disabled")) == 1

    # The second delivery finds the integration stopped and is not sent.
    await world.once()
    delivery = await world.delivery()
    assert (delivery.status, delivery.error_code) == ("failed", "INTEGRATION_INACTIVE")
    assert len(harness.webhooks.sent) == 1
    assert await world.notices() == 1

    listed = await world.client.get(
        f"{PROJECTS}/{world.project['id']}/integrations/{world.integration_id}"
    )
    assert listed.json()["data"]["status"] == "error"
    assert listed.json()["data"]["last_error"]["code"] == "WEBHOOK_NOT_FOUND"


@pytest.mark.asyncio
async def test_five_failures_in_a_row_stop_the_integration(world: World) -> None:
    world.harness.webhooks.result = SendResult("permanent", 400, "PROVIDER_REJECTED")
    for number in range(1, 6):
        await world.finish_run()
        await world.once()
        integration = await world.integration()
        assert integration.consecutive_failures == number
        assert integration.status == ("error" if number == 5 else "active")
    assert await world.notices() == 1


@pytest.mark.asyncio
async def test_result_for_an_older_url_leaves_the_integration_alone(world: World) -> None:
    world.harness.webhooks.result = SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
    await world.finish_run()
    claimed = await worker.claim(world.harness.factory)
    changed = await world.client.patch(
        f"{PROJECTS}/{world.project['id']}/integrations/{world.integration_id}",
        json={"webhook_url": SLACK_2},
        headers=mutation_headers(world.session["csrf_token"]),
    )
    assert changed.status_code == 200
    await worker.record(
        world.harness.factory, None, claimed, SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
    )
    assert (await world.delivery()).status == "failed"
    integration = await world.integration()
    assert (integration.status, integration.consecutive_failures) == ("active", 0)
    assert integration.last_error_code is None


@pytest.mark.asyncio
async def test_lease_runs_out_and_the_delivery_is_claimed_again(world: World) -> None:
    await world.finish_run()
    first = await worker.claim(world.harness.factory)
    assert first is not None
    # Held under a live lease: no one else takes it.
    assert await worker.claim(world.harness.factory) is None
    async with world.harness.factory() as db:
        await db.execute(
            update(IntegrationDelivery).values(
                locked_until=datetime.now(UTC) - timedelta(seconds=1)
            )
        )
        await db.commit()
    second = await worker.claim(world.harness.factory)
    assert second == (first[0], 2)
    # The first claim's late outcome is ignored; the newer claim records its own.
    await worker.record(world.harness.factory, None, first, SendResult("sent", 200))
    assert (await world.delivery()).status == "sending"
    await worker.record(world.harness.factory, None, second, SendResult("sent", 200))
    assert (await world.delivery()).status == "sent"


@pytest.mark.asyncio
async def test_a_row_claimed_too_often_is_given_up(world: World) -> None:
    await world.finish_run()
    async with world.harness.factory() as db:
        await db.execute(
            update(IntegrationDelivery).values(
                status="sending",
                claims=worker.MAX_CLAIMS,
                locked_until=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
        await db.commit()
    assert await world.once() is False
    delivery = await world.delivery()
    assert (delivery.status, delivery.error_code) == ("failed", "CLAIM_LIMIT")
    assert world.harness.webhooks.sent == []


@pytest.mark.asyncio
async def test_clean_up_keeps_30_days_of_finished_deliveries(world: World) -> None:
    for _ in range(3):
        await world.finish_run()
    async with world.harness.factory() as db:
        rows = list(await db.scalars(select(IntegrationDelivery)))
        old = datetime.now(UTC) - timedelta(days=31)
        rows[0].status, rows[0].created_at = "sent", old
        rows[1].status, rows[1].created_at = "pending", old
        await db.commit()
        kept = {rows[1].id, rows[2].id}
    assert await worker.clean_up(world.harness.factory) == 1
    async with world.harness.factory() as db:
        assert set(await db.scalars(select(IntegrationDelivery.id))) == kept


@pytest.mark.asyncio
async def test_deliveries_history_for_managers(world: World) -> None:
    harness = world.harness
    world.harness.webhooks.result = SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
    await world.finish_run()
    await world.once()
    url = f"{PROJECTS}/{world.project['id']}/integrations/{world.integration_id}/deliveries"
    listed = await world.client.get(url)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["data"]
    assert item["event"] == "run.finished"
    assert (item["status"], item["response_code"], item["error_code"]) == (
        "failed",
        404,
        "WEBHOOK_NOT_FOUND",
    )
    assert item["error_message"]
    assert (await world.client.get(url, params={"limit": 51})).status_code == 422

    async with harness.client() as researcher_client:
        await login(harness, researcher_client, uid="res", email="res@example.com")
        await add_member(
            world.client, world.session, world.project["id"], "res@example.com", "researcher"
        )
        assert code(await researcher_client.get(url)) == (403, "ROLE_REQUIRED")


@pytest.mark.asyncio
async def test_worker_logs_never_hold_the_webhook_url(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    world.harness.webhooks.result = SendResult("retryable", None, "PROVIDER_TIMEOUT")
    await world.finish_run()
    with caplog.at_level(logging.DEBUG):
        await world.once()
    assert "secretTOKEN" not in caplog.text


@pytest.mark.asyncio
async def test_waits_for_migrations_on_an_empty_database() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert await worker._schema_ready(async_sessionmaker(engine)) is False
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_workers_failing_at_once_tell_managers_once(world: World) -> None:
    gone = SendResult("permanent", 404, "WEBHOOK_NOT_FOUND")
    await world.finish_run()
    await world.finish_run("failed")
    # Both deliveries are in flight before either outcome is recorded.
    first = await worker.claim(world.harness.factory)
    second = await worker.claim(world.harness.factory)
    assert first and second and first != second
    await worker.record(world.harness.factory, None, first, gone)
    await worker.record(world.harness.factory, None, second, gone)
    assert (await world.integration()).status == "error"
    assert await world.notices() == 1
    assert len(await audit_events(world.harness, "integration.auto_disabled")) == 1
