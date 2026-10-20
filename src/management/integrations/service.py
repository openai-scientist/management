import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.models import AuditEvent
from management.audit.service import record_audit
from management.core.errors import APIError
from management.infra.secret_box import SecretBox
from management.integrations.catalog import (
    EVENTS,
    MAX_INTEGRATIONS_PER_PROJECT,
    PROVIDERS,
    error_message,
)
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.integrations.schemas import (
    CatalogEvent,
    CatalogLimits,
    CatalogProvider,
    CreateIntegration,
    IntegrationCatalog,
    IntegrationError,
    IntegrationItem,
    UpdateIntegration,
    WebhookTestFailure,
    WebhookTestResult,
)
from management.integrations.sender import SendResult
from management.integrations.url_rules import url_hint, validate_webhook_url


def catalog() -> IntegrationCatalog:
    return IntegrationCatalog(
        providers=[
            CatalogProvider(
                key=provider.key,
                label=provider.label,
                url_placeholder=provider.url_placeholder,
                setup_steps=list(provider.setup_steps),
                docs_url=provider.docs_url,
            )
            for provider in PROVIDERS.values()
        ],
        events=[
            CatalogEvent(
                key=event.key, label=event.label, description=event.description, default=True
            )
            for event in EVENTS.values()
        ],
        limits=CatalogLimits(max_per_project=MAX_INTEGRATIONS_PER_PROJECT),
    )


def require_box(box: SecretBox | None) -> SecretBox:
    if box is None:
        raise APIError(
            503, "INTEGRATIONS_NOT_CONFIGURED", "Integrations are not configured on this server"
        )
    return box


def item(row: ProjectIntegration) -> IntegrationItem:
    last_error = None
    if row.last_error_code is not None:
        last_error = IntegrationError(
            code=row.last_error_code,
            message=error_message(row.last_error_code),
            at=row.last_error_at,
        )
    return IntegrationItem(
        id=str(row.id),
        project_id=str(row.project_id),
        provider=row.provider,
        name=row.name,
        url_hint=row.url_hint,
        events=list(row.events),
        status=row.status,
        last_delivered_at=row.last_delivered_at,
        last_error=last_error,
        created_by_user_id=str(row.created_by_user_id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def checked_events(keys: list[str]) -> list[str]:
    """The keys in catalog order without repeats, or 422 when empty or unknown."""
    wanted = set(keys)
    if not wanted or not wanted <= EVENTS.keys():
        raise APIError(
            422,
            "INTEGRATION_EVENTS_REQUIRED",
            "Choose at least one event from the list to post about.",
        )
    return [key for key in EVENTS if key in wanted]


async def find_integration(
    db: AsyncSession, project_id: UUID, integration_id: UUID, *, lock: bool = False
) -> ProjectIntegration:
    query = select(ProjectIntegration).where(
        ProjectIntegration.id == integration_id, ProjectIntegration.project_id == project_id
    )
    if lock:
        query = query.with_for_update()
    row = await db.scalar(query)
    if row is None:
        raise APIError(404, "INTEGRATION_NOT_FOUND", "The integration was not found")
    return row


async def _name_taken(
    db: AsyncSession, project_id: UUID, name: str, *, except_id: UUID | None = None
) -> bool:
    query = select(ProjectIntegration.id).where(
        ProjectIntegration.project_id == project_id,
        func.lower(ProjectIntegration.name) == name.lower(),
    )
    if except_id is not None:
        query = query.where(ProjectIntegration.id != except_id)
    return await db.scalar(query) is not None


def _name_exists() -> APIError:
    return APIError(
        409, "INTEGRATION_NAME_TAKEN", "The project already has an integration by that name"
    )


def _audit(
    db: AsyncSession,
    row: ProjectIntegration,
    action: str,
    *,
    actor_user_id: UUID,
    request_id: str | None,
    details: dict[str, Any],
) -> None:
    record_audit(
        db,
        actor_user_id=actor_user_id,
        action=action,
        resource_type="project_integration",
        resource_id=row.id,
        project_id=row.project_id,
        request_id=request_id,
        details=details,
    )


async def create_integration(
    db: AsyncSession,
    box: SecretBox,
    project_id: UUID,
    body: CreateIntegration,
    *,
    actor_user_id: UUID,
    request_id: str | None,
) -> ProjectIntegration:
    """Save a new integration; the caller holds the project scope lock."""
    events = checked_events(body.events)
    url = validate_webhook_url(body.provider, body.webhook_url)
    count = await db.scalar(
        select(func.count())
        .select_from(ProjectIntegration)
        .where(ProjectIntegration.project_id == project_id)
    )
    if int(count or 0) >= MAX_INTEGRATIONS_PER_PROJECT:
        raise APIError(
            409,
            "INTEGRATION_LIMIT_REACHED",
            f"A project can have up to {MAX_INTEGRATIONS_PER_PROJECT} integrations. "
            "Remove one to add another.",
        )
    if await _name_taken(db, project_id, body.name):
        raise _name_exists()
    row = ProjectIntegration(
        project_id=project_id,
        provider=body.provider,
        name=body.name,
        events=events,
        secret_ciphertext=box.seal({"url": url}),
        url_hint=url_hint(url),
        status="active" if body.enabled else "paused",
        created_by_user_id=actor_user_id,
    )
    db.add(row)
    await db.flush()
    _audit(
        db,
        row,
        "integration.created",
        actor_user_id=actor_user_id,
        request_id=request_id,
        details={"provider": row.provider, "name": row.name, "events": events},
    )
    return row


async def update_integration(
    db: AsyncSession,
    box: SecretBox,
    row: ProjectIntegration,
    body: UpdateIntegration,
    *,
    actor_user_id: UUID,
    request_id: str | None,
) -> ProjectIntegration:
    """Apply the fields sent; the caller holds the project scope lock and the row lock."""
    if body.model_dump(exclude_none=True) == {}:
        raise APIError(422, "EMPTY_UPDATE", "Provide at least one field to change")
    changed: dict[str, Any] = {}
    if body.name is not None and body.name != row.name:
        if await _name_taken(db, row.project_id, body.name, except_id=row.id):
            raise _name_exists()
        changed["name"] = {"from": row.name, "to": body.name}
        row.name = body.name
    if body.events is not None:
        events = checked_events(body.events)
        if events != row.events:
            changed["events"] = events
            row.events = events
    restart = False
    if body.webhook_url is not None:
        url = validate_webhook_url(row.provider, body.webhook_url)
        row.secret_ciphertext = box.seal({"url": url})
        row.url_hint = url_hint(url)
        changed["webhook_url"] = True
        restart = True
    if body.enabled is False:
        if row.status != "paused":
            changed["status"] = {"from": row.status, "to": "paused"}
        row.status = "paused"
    elif body.enabled is True and row.status != "active":
        changed["status"] = {"from": row.status, "to": "active"}
        row.status = "active"
        restart = True
    elif restart and row.status == "error":
        # A new URL is the fix the error asked for.
        changed["status"] = {"from": "error", "to": "active"}
        row.status = "active"
    if restart:
        row.config_version += 1
        row.consecutive_failures = 0
        row.last_error_code = None
        row.last_error_at = None
    if changed:
        _audit(
            db,
            row,
            "integration.updated",
            actor_user_id=actor_user_id,
            request_id=request_id,
            details=changed,
        )
    await db.flush()
    return row


async def delete_integration(
    db: AsyncSession, row: ProjectIntegration, *, actor_user_id: UUID, request_id: str | None
) -> None:
    await db.execute(
        delete(IntegrationDelivery).where(IntegrationDelivery.integration_id == row.id)
    )
    _audit(
        db,
        row,
        "integration.deleted",
        actor_user_id=actor_user_id,
        request_id=request_id,
        details={"provider": row.provider, "name": row.name},
    )
    await db.delete(row)
    await db.flush()


# Test sends go out at once, so they are capped where every API process sees the count.
TESTS_PER_MINUTE_PER_USER = 5
TESTS_PER_MINUTE_PER_PROJECT = 5
TESTS_PER_DAY_PER_PROJECT = 30


async def reserve_test(
    db: AsyncSession,
    project_id: UUID,
    *,
    provider: str,
    url: str,
    resource_type: str,
    resource_id: UUID,
    actor_user_id: UUID,
    request_id: str | None,
) -> None:
    """Count this test send against the limits, or 429; the caller holds the project lock."""
    if db.bind and db.bind.dialect.name == "postgresql":
        # The project lock does not cover one user testing in several projects at once.
        # Always taken after the project lock, so the two cannot deadlock.
        await db.execute(
            text("SELECT pg_advisory_xact_lock(1806, hashtext(:user_id))"),
            {"user_id": str(actor_user_id)},
        )
    now = datetime.now(UTC)
    tested = (
        select(func.count())
        .select_from(AuditEvent)
        .where(AuditEvent.action == "integration.tested")
    )
    minute_ago = now - timedelta(minutes=1)
    counts = [
        await db.scalar(
            tested.where(
                AuditEvent.actor_user_id == actor_user_id, AuditEvent.created_at >= minute_ago
            )
        ),
        await db.scalar(
            tested.where(AuditEvent.project_id == project_id, AuditEvent.created_at >= minute_ago)
        ),
        await db.scalar(
            tested.where(
                AuditEvent.project_id == project_id,
                AuditEvent.created_at >= now - timedelta(days=1),
            )
        ),
    ]
    limits = [
        TESTS_PER_MINUTE_PER_USER,
        TESTS_PER_MINUTE_PER_PROJECT,
        TESTS_PER_DAY_PER_PROJECT,
    ]
    if any((count or 0) >= limit for count, limit in zip(counts, limits, strict=True)):
        raise APIError(
            429,
            "INTEGRATION_TEST_RATE_LIMITED",
            "Too many test messages. Wait a minute, then try again.",
        )
    record_audit(
        db,
        actor_user_id=actor_user_id,
        action="integration.tested",
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        request_id=request_id,
        # A digest tells tests of one URL apart without keeping the secret.
        details={"provider": provider, "url_sha256": hashlib.sha256(url.encode()).hexdigest()},
    )


def webhook_test_result(result: SendResult) -> WebhookTestResult:
    if result.ok:
        return WebhookTestResult(ok=True)
    code = result.error_code or "PROVIDER_ERROR"
    return WebhookTestResult(
        ok=False, error=WebhookTestFailure(code=code, message=error_message(code))
    )
