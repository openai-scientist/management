"""Send queued deliveries: `python -m management.integrations.worker`.

Each delivery goes through three steps, and no transaction stays open while the provider
answers:

1. Claim: lock one due row with SKIP LOCKED, mark it `sending` with a lease, commit.
2. Send: build the message and post it, outside any transaction.
3. Record: lock the integration row, store the outcome, schedule a retry or give up.

A worker that dies mid-send leaves its lease to run out; another claims the row again, so
a message can arrive twice but is never lost. Several workers can run side by side.
"""

import asyncio
import logging
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from management.audit.service import record_audit
from management.core.config import Settings, get_settings
from management.core.logging import configure_logging
from management.infra.secret_box import SecretBox, SecretBoxError
from management.integrations.content import build_message
from management.integrations.formatters import payload_for
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.integrations.sender import SendResult, WebhookSender
from management.notifications.notification_stream import NotificationHub
from management.notifications.service import notify_user
from management.projects.models import Project, ProjectMembership

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
LEASE = timedelta(seconds=60)
MAX_CLAIMS = 10
# A failed send is tried again after each delay, then given up.
RETRY_DELAYS = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30))
MAX_ATTEMPTS = len(RETRY_DELAYS) + 1
# A provider that keeps asking us to wait this long gets the delivery marked failed.
MAX_RATE_LIMITED_FOR = timedelta(hours=1)
FAILURES_BEFORE_ERROR = 5
KEEP_HISTORY = timedelta(days=30)
CLEANUP_EVERY_SECONDS = 3600
HEARTBEAT = Path("/tmp/integration-worker.heartbeat")

Sessions = async_sessionmaker[AsyncSession]


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(moment: datetime) -> datetime:
    # SQLite hands back naive datetimes; every stored time is UTC.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _open(sessions: Sessions, hub: NotificationHub | None) -> AsyncSession:
    db = sessions()
    if hub is not None:
        hub.bind(db)
    return db


Claim = tuple[UUID, int]


async def claim(sessions: Sessions) -> Claim | None:
    """The claimed delivery and its claim number, which only this claim's outcome may carry."""
    now = _now()
    async with sessions() as db, db.begin():
        delivery = await db.scalar(
            select(IntegrationDelivery)
            .where(
                or_(
                    and_(
                        IntegrationDelivery.status == "pending",
                        IntegrationDelivery.next_attempt_at <= now,
                    ),
                    and_(
                        IntegrationDelivery.status == "sending",
                        IntegrationDelivery.locked_until < now,
                    ),
                )
            )
            .order_by(IntegrationDelivery.next_attempt_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if delivery is None:
            return None
        delivery.claims += 1
        if delivery.claims > MAX_CLAIMS:
            # Claimed again and again without an outcome: something crashes on this row.
            delivery.status, delivery.error_code, delivery.locked_until = (
                "failed",
                "CLAIM_LIMIT",
                None,
            )
            return None
        delivery.status, delivery.locked_until = "sending", now + LEASE
        return delivery.id, delivery.claims


async def _prepare(
    sessions: Sessions, delivery_id: UUID, box: SecretBox | None, app_url: str | None
) -> tuple[str, str, dict] | str | None:
    """What to post and where, or the error code that ends the delivery unsent."""
    async with sessions() as db:
        delivery = await db.get(IntegrationDelivery, delivery_id)
        if delivery is None:
            return None
        integration = await db.get(ProjectIntegration, delivery.integration_id)
        if integration is None or integration.status != "active":
            return "INTEGRATION_INACTIVE"
        if box is None:
            return "INTEGRATIONS_NOT_CONFIGURED"
        try:
            url = str(box.open(integration.secret_ciphertext)["url"])
        except (SecretBoxError, KeyError):
            return "INTEGRATION_URL_UNREADABLE"
        project = await db.get(Project, integration.project_id)
        message = await build_message(db, delivery, project, app_url) if project else None
        if message is None:
            return "EVENT_GONE"
        return integration.provider, url, payload_for(integration.provider, message)


async def _notify_managers(db: AsyncSession, integration: ProjectIntegration) -> None:
    managers = await db.scalars(
        select(ProjectMembership.user_id).where(
            ProjectMembership.project_id == integration.project_id,
            ProjectMembership.role_code == "project_manager",
            ProjectMembership.status == "active",
        )
    )
    for user_id in managers:
        notify_user(db, user_id, "integration_failed", project_id=integration.project_id)
    record_audit(
        db,
        actor_user_id=None,
        action="integration.auto_disabled",
        resource_type="project_integration",
        resource_id=integration.id,
        project_id=integration.project_id,
        details={"error_code": integration.last_error_code},
    )


async def record(
    sessions: Sessions,
    hub: NotificationHub | None,
    claimed: Claim,
    result: SendResult | None,
    *,
    skipped: str | None = None,
) -> None:
    now = _now()
    delivery_id, claim_number = claimed
    async with _open(sessions, hub) as db, db.begin():
        delivery = await db.get(IntegrationDelivery, delivery_id, with_for_update=True)
        if delivery is None or delivery.status != "sending" or delivery.claims != claim_number:
            # Deleted with its integration, or claimed again after this lease ran out: the
            # newer claim records the outcome.
            return
        delivery.locked_until = None
        if result is None:
            delivery.status, delivery.error_code = "failed", skipped
            return
        delivery.response_code = result.status
        if result.ok:
            delivery.status, delivery.sent_at, delivery.error_code = "sent", now, None
        elif (
            result.outcome == "rate_limited"
            and now - _aware(delivery.created_at) < MAX_RATE_LIMITED_FOR
        ):
            # Waiting is not a failure and does not use up an attempt.
            delivery.status, delivery.error_code = "pending", result.error_code
            delivery.next_attempt_at = now + timedelta(seconds=result.retry_after or 60)
            return
        elif result.outcome == "retryable" and delivery.attempts + 1 < MAX_ATTEMPTS:
            delivery.attempts += 1
            delivery.status, delivery.error_code = "pending", result.error_code
            delivery.next_attempt_at = now + RETRY_DELAYS[delivery.attempts - 1]
            return
        else:
            if result.outcome == "retryable":
                delivery.attempts += 1
            delivery.status, delivery.error_code = "failed", result.error_code

        integration = await db.get(
            ProjectIntegration, delivery.integration_id, with_for_update=True
        )
        # A result for an older URL says nothing about the one saved since.
        if integration is None or integration.config_version != delivery.config_version:
            return
        if delivery.status == "sent":
            integration.consecutive_failures, integration.last_delivered_at = 0, now
            return
        integration.consecutive_failures += 1
        integration.last_error_code, integration.last_error_at = delivery.error_code, now
        if integration.status == "active" and (
            result.disables or integration.consecutive_failures >= FAILURES_BEFORE_ERROR
        ):
            # Under the row lock only one worker sees `active` here, so managers hear once.
            integration.status = "error"
            await _notify_managers(db, integration)


async def run_once(
    sessions: Sessions,
    sender: WebhookSender,
    *,
    box: SecretBox | None,
    app_url: str | None,
    hub: NotificationHub | None = None,
) -> bool:
    """Handle one due delivery; False when none was due."""
    claimed = await claim(sessions)
    if claimed is None:
        return False
    prepared = await _prepare(sessions, claimed[0], box, app_url)
    if prepared is None:
        return True
    if isinstance(prepared, str):
        await record(sessions, hub, claimed, None, skipped=prepared)
        return True
    provider, url, payload = prepared
    result = await sender.send(provider, url, payload)
    await record(sessions, hub, claimed, result)
    return True


async def clean_up(sessions: Sessions) -> int:
    """Drop finished deliveries older than the history the web app shows."""
    async with sessions() as db, db.begin():
        removed = await db.execute(
            delete(IntegrationDelivery).where(
                IntegrationDelivery.status.in_(("sent", "failed")),
                IntegrationDelivery.created_at < _now() - KEEP_HISTORY,
            )
        )
        return removed.rowcount or 0


async def _schema_ready(sessions: Sessions) -> bool:
    try:
        async with sessions() as db:
            await db.execute(text("SELECT 1 FROM integration_deliveries LIMIT 1"))
        return True
    except DBAPIError:
        return False


def secret_box(settings: Settings) -> SecretBox | None:
    key = settings.connection_secret_key
    return SecretBox(key.get_secret_value()) if key else None


async def serve(stop: asyncio.Event) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    hub = NotificationHub(engine)
    box = secret_box(settings)
    sender = WebhookSender()
    try:
        wait = 5.0
        while not stop.is_set() and not await _schema_ready(sessions):
            if wait == 5.0:
                logger.error(
                    "Integration tables are missing; run the database migrations. Retrying."
                )
            HEARTBEAT.write_text(str(time.time()))
            await _sleep(stop, wait)
            wait = min(wait * 2, 60.0)
        logger.info("Integration worker started")
        last_cleanup = 0.0
        while not stop.is_set():
            HEARTBEAT.write_text(str(time.time()))
            try:
                if time.monotonic() - last_cleanup >= CLEANUP_EVERY_SECONDS:
                    last_cleanup = time.monotonic()
                    await clean_up(sessions)
                busy = await run_once(sessions, sender, box=box, app_url=settings.app_url, hub=hub)
            except Exception:
                # The exception may quote a URL; its type is enough to find the bug.
                logger.exception("Integration worker step failed")
                busy = False
            if not busy:
                await _sleep(stop, POLL_SECONDS)
    finally:
        await engine.dispose()
        logger.info("Integration worker stopped")


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass


def main() -> None:
    configure_logging("DEBUG" if get_settings().debug else "INFO")

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        # Stop claiming; the delivery in hand finishes first.
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stop.set)
        await serve(stop)

    asyncio.run(run())


if __name__ == "__main__":
    main()
