"""Chat channels a project posts its events to: Slack, Microsoft Teams and Discord webhooks."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.infra.secret_box import SecretBox, SecretBoxError, get_secret_box
from management.integrations.catalog import error_message
from management.integrations.formatters import payload_for
from management.integrations.messages import sample_message
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.integrations.schemas import (
    CreateIntegration,
    DeliveryItem,
    IntegrationCatalog,
    IntegrationItem,
    UpdateIntegration,
    WebhookTest,
    WebhookTestResult,
)
from management.integrations.sender import WebhookSender, get_webhook_sender
from management.integrations.service import (
    catalog,
    create_integration,
    delete_integration,
    find_integration,
    item,
    require_box,
    reserve_test,
    update_integration,
    webhook_test_result,
)
from management.integrations.url_rules import validate_webhook_url

router = APIRouter(tags=["integrations"])

PROJECT_PATH = "/projects/{project_id}/integrations"

READ_ERRORS = {
    404: {
        "model": ErrorResponse,
        "description": "NOT_FOUND (project) or INTEGRATION_NOT_FOUND",
    },
}
WRITE_ERRORS = {
    **READ_ERRORS,
    403: {"model": ErrorResponse, "description": "Project Manager role is required"},
    409: {
        "model": ErrorResponse,
        "description": "INTEGRATION_NAME_TAKEN, INTEGRATION_LIMIT_REACHED or PROJECT_ARCHIVED",
    },
    422: {
        "model": ErrorResponse,
        "description": (
            "INTEGRATION_URL_INVALID, INTEGRATION_EVENTS_REQUIRED, EMPTY_UPDATE or VALIDATION_ERROR"
        ),
    },
    503: {"model": ErrorResponse, "description": "INTEGRATIONS_NOT_CONFIGURED"},
}
TEST_ERRORS = {
    **WRITE_ERRORS,
    429: {"model": ErrorResponse, "description": "INTEGRATION_TEST_RATE_LIMITED"},
}
TEST_DESCRIPTION = (
    "Project Manager only. Posts a fixed test message at once and answers 200 with the "
    "outcome: `ok: false` is a result, with `error.code` and a message to show. It never "
    "changes the integration's status, also when the test succeeds after an error. At most 5 "
    "tests a minute per user, and 5 a minute and 30 a day per project."
)


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


@router.get(
    "/integrations/catalog",
    response_model=ApiResponse[IntegrationCatalog],
    summary="What a project can connect to, and which events it can post",
    description=(
        "Drives the add-integration form: providers with setup steps for finding the webhook "
        "URL, the events with their default, and the per-project limit."
    ),
)
async def read_catalog(
    _principal: Principal = Depends(require_active_principal),
) -> ApiResponse[IntegrationCatalog]:
    return ok(catalog())


@router.get(
    PROJECT_PATH,
    response_model=ApiResponse[list[IntegrationItem]],
    summary="List the project's integrations, newest first",
    description="Any project member can read. Webhook URLs are never returned, only `url_hint`.",
    responses=READ_ERRORS,
)
async def list_integrations(
    project_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[IntegrationItem]]:
    await require_project_access(db, principal, project_id)
    rows = (
        await db.scalars(
            select(ProjectIntegration)
            .where(ProjectIntegration.project_id == project_id)
            .order_by(ProjectIntegration.created_at.desc(), ProjectIntegration.id)
        )
    ).all()
    return ok([item(row) for row in rows])


@router.post(
    PROJECT_PATH,
    status_code=201,
    response_model=ApiResponse[IntegrationItem],
    summary="Connect a chat channel",
    description=(
        "Project Manager only. The URL must be a webhook issued by the chosen provider; "
        "it is stored encrypted and never returned."
    ),
    responses=WRITE_ERRORS,
)
async def add_integration(
    project_id: UUID,
    body: CreateIntegration,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
) -> ApiResponse[IntegrationItem]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    row = await create_integration(
        db,
        require_box(box),
        project_id,
        body,
        actor_user_id=principal.user.id,
        request_id=_request_id(request),
    )
    return ok(item(row), "Integration added")


@router.post(
    PROJECT_PATH + "/test",
    response_model=ApiResponse[WebhookTestResult],
    summary="Send a test message to a webhook before saving it",
    description=TEST_DESCRIPTION + " The message does not name the project.",
    responses=TEST_ERRORS,
)
async def try_new_webhook(
    project_id: UUID,
    body: WebhookTest,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    sender: WebhookSender = Depends(get_webhook_sender),
) -> ApiResponse[WebhookTestResult]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    url = validate_webhook_url(body.provider, body.webhook_url)
    await reserve_test(
        db,
        project_id,
        provider=body.provider,
        url=url,
        resource_type="project",
        resource_id=project_id,
        actor_user_id=principal.user.id,
        request_id=_request_id(request),
    )
    # Count the test and release the project lock before waiting on the provider.
    await db.commit()
    result = await sender.send(
        body.provider, url, payload_for(body.provider, sample_message(None, None))
    )
    return ok(webhook_test_result(result))


@router.post(
    PROJECT_PATH + "/{integration_id}/test",
    response_model=ApiResponse[WebhookTestResult],
    summary="Send a test message through a saved integration",
    description=TEST_DESCRIPTION,
    responses={
        **TEST_ERRORS,
        409: {
            "model": ErrorResponse,
            "description": "INTEGRATION_URL_UNREADABLE or PROJECT_ARCHIVED",
        },
    },
)
async def try_saved_webhook(
    project_id: UUID,
    integration_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    sender: WebhookSender = Depends(get_webhook_sender),
) -> ApiResponse[WebhookTestResult]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    row = await find_integration(db, project_id, integration_id)
    try:
        url = str(require_box(box).open(row.secret_ciphertext)["url"])
    except SecretBoxError as exc:
        raise APIError(
            409,
            "INTEGRATION_URL_UNREADABLE",
            "The saved webhook URL can't be read. Paste the URL again to fix it.",
        ) from exc
    provider, message = row.provider, sample_message(project.name, row.name)
    await reserve_test(
        db,
        project_id,
        provider=provider,
        url=url,
        resource_type="project_integration",
        resource_id=row.id,
        actor_user_id=principal.user.id,
        request_id=_request_id(request),
    )
    await db.commit()
    result = await sender.send(provider, url, payload_for(provider, message))
    return ok(webhook_test_result(result))


@router.get(
    PROJECT_PATH + "/{integration_id}",
    response_model=ApiResponse[IntegrationItem],
    summary="Get an integration",
    description="Any project member can read.",
    responses=READ_ERRORS,
)
async def get_integration(
    project_id: UUID,
    integration_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[IntegrationItem]:
    await require_project_access(db, principal, project_id)
    return ok(item(await find_integration(db, project_id, integration_id)))


@router.patch(
    PROJECT_PATH + "/{integration_id}",
    response_model=ApiResponse[IntegrationItem],
    summary="Change an integration",
    description=(
        "Project Manager only. Send only what changes. `enabled: false` pauses; `enabled: "
        "true` turns it on again, also after an error. A new `webhook_url` also clears an "
        "error. The provider cannot change: add a new integration instead."
    ),
    responses=WRITE_ERRORS,
)
async def change_integration(
    project_id: UUID,
    integration_id: UUID,
    body: UpdateIntegration,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
) -> ApiResponse[IntegrationItem]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    row = await find_integration(db, project_id, integration_id, lock=True)
    row = await update_integration(
        db,
        require_box(box),
        row,
        body,
        actor_user_id=principal.user.id,
        request_id=_request_id(request),
    )
    return ok(item(row), "Integration saved")


@router.delete(
    PROJECT_PATH + "/{integration_id}",
    response_model=ApiResponse[None],
    summary="Remove an integration",
    description="Project Manager only. Its delivery history goes with it.",
    responses=WRITE_ERRORS,
)
async def remove_integration(
    project_id: UUID,
    integration_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    row = await find_integration(db, project_id, integration_id, lock=True)
    await delete_integration(
        db, row, actor_user_id=principal.user.id, request_id=_request_id(request)
    )
    return ok(None, "Integration removed")


@router.get(
    PROJECT_PATH + "/{integration_id}/deliveries",
    response_model=ApiResponse[list[DeliveryItem]],
    summary="Recent messages sent through an integration, newest first",
    description="Project Manager only. History is kept for 30 days.",
    responses={
        **READ_ERRORS,
        403: {"model": ErrorResponse, "description": "Project Manager role is required"},
    },
)
async def list_deliveries(
    project_id: UUID,
    integration_id: UUID,
    limit: int = Query(20, ge=1, le=50),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[DeliveryItem]]:
    await require_project_access(db, principal, project_id, manage=True)
    await find_integration(db, project_id, integration_id)
    rows = (
        await db.scalars(
            select(IntegrationDelivery)
            .where(IntegrationDelivery.integration_id == integration_id)
            .order_by(IntegrationDelivery.created_at.desc(), IntegrationDelivery.id)
            .limit(limit)
        )
    ).all()
    return ok(
        [
            DeliveryItem(
                id=str(row.id),
                event=row.event_key,
                status=row.status,
                attempts=row.attempts,
                response_code=row.response_code,
                error_code=row.error_code,
                error_message=error_message(row.error_code) if row.error_code else None,
                created_at=row.created_at,
                sent_at=row.sent_at,
            )
            for row in rows
        ]
    )
