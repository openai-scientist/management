import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.service import record_audit
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.sessions import (
    Principal,
    get_principal,
    get_principal_without_touch,
    require_active_csrf,
    require_origin,
    require_platform_admin,
)
from management.db.session import get_db
from management.identity.models import UserPlatformRole
from management.monitoring.models import MonitoringAlert, MonitoringEvent
from management.monitoring.queries import (
    P95_MIN_SAMPLES,
    SERVICE_CODE,
    check_capture_coverage,
    event_filters,
    event_to_public,
    request_bucket_points,
    request_events,
)
from management.monitoring.schemas.admin_log_monitoring import (
    LogEventDetail,
    LogEventOut,
    MetricsPoint,
    MonitoringAlertOut,
    MonitoringServiceOut,
    ServiceMetricsData,
)
from management.monitoring.service import (
    _alert_out,
    _as_utc,
    _event_out,
    _pipeline_status,
    _resolve_service,
    _service_item,
    _sse_event,
    _validate_window,
)
from management.monitoring.stream import MonitoringHub, monitoring_changed

router = APIRouter(prefix="/admin/log-monitoring", tags=["admin-log-monitoring"])


MAX_POINTS = 500


VALID_INTERVALS = {60, 300, 900}


REQUEST_LATENCY_MIN_SAMPLES = P95_MIN_SAMPLES


ADMIN_AUTH_ERRORS = {
    401: {
        "model": ErrorResponse,
        "description": "Session missing, expired, revoked, or suspended.",
    },
    403: {
        "model": ErrorResponse,
        "description": "Platform Admin role or required origin/CSRF check failed.",
    },
}


READ_ERROR_RESPONSES = {
    **ADMIN_AUTH_ERRORS,
    422: {
        "model": ErrorResponse,
        "description": "Query parameters or time range are invalid or over the documented limit.",
    },
    429: {
        "model": ErrorResponse,
        "description": "Request rate or stream connection limit exceeded; see Retry-After.",
    },
    503: {
        "model": ErrorResponse,
        "description": "Monitoring data or stream is temporarily unavailable.",
    },
}


@router.get(
    "/services",
    response_model=ApiResponse[list[MonitoringServiceOut]],
    summary="List measured monitoring services",
    description=(
        "Returns the measured Platform API service for the current APP_ENV, with total-before-page "
        "pagination. Other environments return an empty page. Missing metrics remain null and "
        "include availability metadata. Requires a Platform Admin session."
    ),
    responses=READ_ERROR_RESPONSES,
)
async def list_monitoring_services(
    request: Request,
    environment: str | None = Query(
        default=None, max_length=20, description="Environment name; defaults to APP_ENV."
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum services returned per page."),
    offset: int = Query(default=0, ge=0, description="Number of services skipped before the page."),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[MonitoringServiceOut]]:
    settings = request.app.state.settings
    selected_environment = environment or settings.app_env
    if selected_environment != settings.app_env:
        return paginated([], total=0, limit=limit, offset=offset)
    item = await _service_item(
        db,
        environment=selected_environment,
        now=datetime.now(UTC),
        health_rules_enabled=(settings.monitoring_error_rate_alert_threshold_percent is not None),
        health_minimum_samples=settings.monitoring_error_rate_alert_min_samples,
        health_threshold_percent=settings.monitoring_error_rate_alert_threshold_percent,
    )
    rows = [item][offset : offset + limit]
    return paginated(rows, total=1, limit=limit, offset=offset)


@router.get(
    "/events",
    response_model=ApiResponse[list[LogEventOut]],
    summary="Search sanitized monitoring events",
    description=(
        "Filters technical events by environment, severity, safe text, or exact correlation IDs. "
        "The default window is the previous 90 minutes; maximum is 14 days. Results sort by "
        "created_at descending then stable event ID descending. Pagination total is counted after "
        "filters and before the page. Requires a Platform Admin session."
    ),
    responses={
        **READ_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Service ID is unknown."},
    },
)
async def list_monitoring_events(
    request: Request,
    service_id: str | None = Query(
        default=None, max_length=160, description="Optional stable service ID."
    ),
    environment: str | None = Query(
        default=None, max_length=20, description="Environment name; defaults to APP_ENV."
    ),
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"] | None = Query(
        default=None, description="Exact event severity."
    ),
    q: str | None = Query(
        default=None,
        max_length=200,
        description="Case-insensitive search over sanitized message and safe correlation fields.",
    ),
    request_id: str | None = Query(
        default=None, max_length=64, description="Exact request ID filter."
    ),
    trace_id: str | None = Query(default=None, max_length=64, description="Exact trace ID filter."),
    project_id: str | None = Query(
        default=None, max_length=64, description="Exact project ID filter."
    ),
    run_id: str | None = Query(default=None, max_length=64, description="Exact run ID filter."),
    from_time: datetime | None = Query(
        default=None, alias="from", description="Inclusive ISO-8601 start; provide with to."
    ),
    to_time: datetime | None = Query(
        default=None, alias="to", description="Exclusive ISO-8601 end; provide with from."
    ),
    limit: int = Query(default=20, ge=1, le=100, description="Maximum events returned per page."),
    offset: int = Query(
        default=0, ge=0, description="Number of filtered events skipped before the page."
    ),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[LogEventOut]]:
    default_environment = request.app.state.settings.app_env
    selected_environment = environment or default_environment
    if service_id:
        selected_environment = _resolve_service(service_id, environment)
    start, end = _validate_window(
        from_time,
        to_time,
        now=datetime.now(UTC),
        default_seconds=90 * 60,
    )
    term = q.strip() if q and q.strip() else None
    filters = event_filters(
        environment=selected_environment,
        service_id=service_id,
        level=level,
        q=term,
        request_id=request_id,
        trace_id=trace_id,
        project_id=project_id,
        run_id=run_id,
        start=start,
        end=end,
    )
    query = select(MonitoringEvent).where(*filters)
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = list(
        (
            await db.scalars(
                query.order_by(MonitoringEvent.created_at.desc(), MonitoringEvent.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return paginated(
        [_event_out(event) for event in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/events/{event_id}",
    response_model=ApiResponse[LogEventDetail],
    summary="Get sanitized monitoring event detail",
    description=(
        "Returns allowlisted event fields and a sanitized payload object; request bodies, "
        "headers, query values, prompts, credentials, and raw exception traces are not exposed. "
        "Requires a Platform Admin session."
    ),
    responses={
        **ADMIN_AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "Event ID was not found or has expired."},
        422: {"model": ErrorResponse, "description": "Event ID is not a valid UUID."},
    },
)
async def get_monitoring_event(
    event_id: UUID = Path(description="Stable UUID of a retained monitoring event."),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[LogEventDetail]:
    event = await db.get(MonitoringEvent, event_id)
    if event is None:
        raise APIError(404, "NOT_FOUND", "Monitoring event was not found")
    public = event_to_public(event, environment=event.environment)
    return ok(LogEventDetail.model_validate(public))


@router.get(
    "/services/{service_id}/metrics",
    response_model=ApiResponse[ServiceMetricsData],
    summary="Get measured service request trends",
    description=(
        "Aggregates persisted request events into interval buckets. The default window is 90 "
        "minutes ending 45 seconds before now; intervals are 60, 300, or 900 seconds and at most "
        "500 points are returned. Request rate uses event count/window duration, errors are "
        "statuses >= 500, and p95 uses nearest-rank with at least 20 latency samples. At most "
        "100,000 request observations are read; exceeding that cap makes values unavailable. "
        "Requires a Platform Admin session."
    ),
    responses={
        **READ_ERROR_RESPONSES,
        404: {"model": ErrorResponse, "description": "Service ID is unknown."},
    },
)
async def get_service_metrics(
    request: Request,
    service_id: str = Path(description="Stable service ID, e.g. platform-api:local."),
    from_time: datetime | None = Query(
        default=None, alias="from", description="Inclusive ISO-8601 start; provide with to."
    ),
    to_time: datetime | None = Query(
        default=None, alias="to", description="Exclusive ISO-8601 end; provide with from."
    ),
    interval_seconds: int = Query(
        default=60, ge=1, le=3600, description="Allowed values: 60, 300, or 900 seconds."
    ),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ServiceMetricsData]:
    environment = _resolve_service(service_id)
    if interval_seconds not in VALID_INTERVALS:
        raise APIError(422, "INVALID_INTERVAL", "Interval must be 60, 300, or 900 seconds")
    now = datetime.now(UTC)
    start, end = _validate_window(
        from_time,
        to_time,
        now=now,
        default_seconds=90 * 60,
        stale_allowance_seconds=45,
    )
    if (end - start).total_seconds() / interval_seconds > MAX_POINTS:
        raise APIError(422, "POINT_LIMIT_EXCEEDED", f"Metrics are limited to {MAX_POINTS} points")
    coverage = await check_capture_coverage(
        db, environment=environment, start=start, end=end, now=now
    )
    events, exceeded = await request_events(db, environment=environment, start=start, end=end)
    available = coverage.available and not exceeded
    points = request_bucket_points(events, start=start, end=end, interval_seconds=interval_seconds)
    if not available:
        for point in points:
            point["request_rate_per_second"] = None
            point["error_rate_percent"] = None
            point["latency_ms"] = None
    return ok(
        ServiceMetricsData(
            service_id=service_id,
            generated_at=now,
            window={"from": start, "to": end},
            interval_seconds=interval_seconds,
            latency_statistic="p95",
            points=[MetricsPoint(**point) for point in points],
            available=available,
            reason_code=("SAMPLE_LIMIT_EXCEEDED" if exceeded else coverage.reason_code),
        )
    )


@router.get(
    "/stream",
    response_class=StreamingResponse,
    summary="Stream Platform Admin monitoring updates",
    description=(
        "Credentialed server-sent events. Reconnect to receive a fresh service snapshot and "
        "then fetch current events from the REST endpoints. The service snapshot is paginated; "
        "service_id filters event/alert updates. Session and role are revalidated every 15 seconds "
        "without renewing idle expiry after initial authentication."
    ),
    responses={
        200: {
            "description": "SSE stream with initial snapshot and committed change events.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **ADMIN_AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "Service ID is unknown."},
        422: {"model": ErrorResponse, "description": "Service/environment filters do not match."},
        429: {
            "model": ErrorResponse,
            "description": "Per-process stream limit exceeded; see Retry-After.",
        },
        503: {
            "model": ErrorResponse,
            "description": "PostgreSQL notification stream is unavailable; see Retry-After.",
        },
    },
)
async def stream_monitoring(
    request: Request,
    service_id: str | None = Query(
        default=None,
        max_length=160,
        description=("Optional stable service ID for event/alert updates."),
    ),
    environment: str | None = Query(
        default=None, max_length=20, description="Environment name; defaults to APP_ENV."
    ),
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description=("Maximum services in the initial and reconnect snapshot."),
    ),
    offset: int = Query(default=0, ge=0, description="Number of services skipped in the snapshot."),
) -> StreamingResponse:
    if request.headers.get("Origin") is not None:
        require_origin(request)
    settings = request.app.state.settings
    selected_environment = environment or settings.app_env
    if service_id:
        selected_environment = _resolve_service(service_id, environment)
    hub: MonitoringHub = request.app.state.monitoring_hub
    factory = request.app.state.session_factory
    async with factory() as db:
        principal = await get_principal(request, db)
        role = await db.get(UserPlatformRole, principal.user.id)
        if role is None:
            raise APIError(403, "ROLE_REQUIRED", "Platform Admin role is required")
        user_id = principal.user.id
        await db.commit()
    try:
        queue = await hub.reserve(user_id)
    except APIError:
        raise
    except Exception as exc:
        raise APIError(
            503,
            "MONITORING_STREAM_UNAVAILABLE",
            "Monitoring updates are temporarily unavailable",
            retry_after=5,
        ) from exc

    async def events() -> AsyncIterator[str]:
        last_seen: tuple[datetime, UUID] | None = None
        yield "retry: 3000\n\n"
        try:
            async with hub.subscribe(user_id, queue):
                first = True
                refresh = True
                while True:
                    messages: list[str] = []
                    terminal = False
                    try:
                        async with factory() as db:
                            principal = await get_principal_without_touch(request, db)
                            role = await db.get(UserPlatformRole, principal.user.id)
                            if role is None:
                                messages.append(
                                    _sse_event("access-ended", {"code": "ROLE_REQUIRED"})
                                )
                                terminal = True
                            if terminal:
                                await db.commit()
                            else:
                                now = datetime.now(UTC)
                                if first or refresh:
                                    service = await _service_item(
                                        db,
                                        environment=selected_environment,
                                        now=now,
                                        health_rules_enabled=(
                                            settings.monitoring_error_rate_alert_threshold_percent
                                            is not None
                                        ),
                                        health_minimum_samples=(
                                            settings.monitoring_error_rate_alert_min_samples
                                        ),
                                        health_threshold_percent=(
                                            settings.monitoring_error_rate_alert_threshold_percent
                                        ),
                                    )
                                    rows = (
                                        [service][offset : offset + limit]
                                        if selected_environment == settings.app_env
                                        else []
                                    )
                                    if first:
                                        messages.append(
                                            _sse_event(
                                                "service-snapshot",
                                                {
                                                    "services": [
                                                        item.model_dump(mode="json")
                                                        for item in rows
                                                    ],
                                                    "total": (
                                                        1
                                                        if selected_environment == settings.app_env
                                                        else 0
                                                    ),
                                                    "limit": limit,
                                                    "offset": offset,
                                                    "generated_at": now,
                                                },
                                            )
                                        )
                                        latest_query = select(MonitoringEvent).where(
                                            MonitoringEvent.service == SERVICE_CODE,
                                            MonitoringEvent.environment == selected_environment,
                                        )
                                        latest = await db.scalar(
                                            latest_query.order_by(
                                                MonitoringEvent.created_at.desc(),
                                                MonitoringEvent.id.desc(),
                                            )
                                        )
                                        if latest is not None:
                                            last_seen = (_as_utc(latest.created_at), latest.id)
                                        first = False
                                    else:
                                        filters = [
                                            MonitoringEvent.service == SERVICE_CODE,
                                            MonitoringEvent.environment == selected_environment,
                                        ]
                                        if last_seen is not None:
                                            last_at, last_id = last_seen
                                            filters.append(
                                                or_(
                                                    MonitoringEvent.created_at > last_at,
                                                    and_(
                                                        MonitoringEvent.created_at == last_at,
                                                        MonitoringEvent.id > last_id,
                                                    ),
                                                )
                                            )
                                        new_events = list(
                                            (
                                                await db.scalars(
                                                    select(MonitoringEvent)
                                                    .where(*filters)
                                                    .order_by(
                                                        MonitoringEvent.created_at,
                                                        MonitoringEvent.id,
                                                    )
                                                    .limit(101)
                                                )
                                            ).all()
                                        )
                                        if len(new_events) > 100:
                                            messages.append(
                                                _sse_event(
                                                    "refresh-required",
                                                    {
                                                        "reason": "STREAM_BACKLOG",
                                                        "service_id": service_id,
                                                    },
                                                )
                                            )
                                            new_events = new_events[:100]
                                        for item in new_events:
                                            messages.append(
                                                _sse_event(
                                                    "log-event",
                                                    event_to_public(
                                                        item, environment=item.environment
                                                    ),
                                                )
                                            )
                                            last_seen = (_as_utc(item.created_at), item.id)
                                        active_query = select(MonitoringAlert).where(
                                            MonitoringAlert.environment == selected_environment,
                                            MonitoringAlert.status == "active",
                                        )
                                        if service_id:
                                            active_query = active_query.where(
                                                MonitoringAlert.service_id == service_id
                                            )
                                        alerts = list(
                                            (
                                                await db.scalars(
                                                    active_query.order_by(
                                                        MonitoringAlert.started_at.desc(),
                                                        MonitoringAlert.id.desc(),
                                                    ).limit(20)
                                                )
                                            ).all()
                                        )
                                        messages.append(
                                            _sse_event(
                                                "alert-updated",
                                                {
                                                    "service_id": service_id,
                                                    "alerts": [
                                                        _alert_out(alert).model_dump(mode="json")
                                                        for alert in alerts
                                                    ],
                                                    "refresh_required": True,
                                                },
                                            )
                                        )
                                        messages.append(
                                            _sse_event(
                                                "service-snapshot",
                                                {
                                                    "services": [
                                                        item.model_dump(mode="json")
                                                        for item in rows
                                                    ],
                                                    "total": (
                                                        1
                                                        if selected_environment == settings.app_env
                                                        else 0
                                                    ),
                                                    "limit": limit,
                                                    "offset": offset,
                                                    "generated_at": now,
                                                },
                                            )
                                        )
                            now = datetime.now(UTC)
                            if not terminal:
                                pipeline = await _pipeline_status(
                                    db, environment=selected_environment, now=now
                                )
                                messages.append(_sse_event("pipeline-status", pipeline))
                                await db.commit()
                    except APIError as exc:
                        messages = [_sse_event("session-ended", {"code": exc.code})]
                        terminal = True
                    for message in messages:
                        yield message
                    if terminal:
                        return
                    try:
                        connected = await asyncio.wait_for(queue.get(), timeout=15)
                        if not connected:
                            return
                        refresh = True
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        refresh = False
        finally:
            hub.release(user_id, queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/alerts/{alert_id}/acknowledge",
    response_model=ApiResponse[MonitoringAlertOut],
    summary="Acknowledge an active monitoring alert",
    description=(
        "Idempotently records the first acknowledging Platform Admin and timestamp, writes the "
        "audit event in the same transaction, and leaves the alert active. Requires session, "
        "allowed Origin, and CSRF token."
    ),
    responses={
        **ADMIN_AUTH_ERRORS,
        404: {"model": ErrorResponse, "description": "Alert ID was not found."},
        409: {"model": ErrorResponse, "description": "Resolved alerts cannot be acknowledged."},
        422: {"model": ErrorResponse, "description": "Alert ID is not a valid UUID."},
        429: {
            "model": ErrorResponse,
            "description": "Request rate limit exceeded; see Retry-After.",
        },
    },
)
async def acknowledge_monitoring_alert(
    request: Request,
    alert_id: UUID = Path(description="Stable UUID of an active monitoring alert."),
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[MonitoringAlertOut]:
    alert = await db.scalar(
        select(MonitoringAlert).where(MonitoringAlert.id == alert_id).with_for_update()
    )
    if alert is None:
        raise APIError(404, "NOT_FOUND", "Monitoring alert was not found")
    if alert.status == "resolved":
        raise APIError(409, "ALERT_RESOLVED", "Resolved alerts cannot be acknowledged")
    if alert.acknowledged_at is None:
        alert.acknowledged_at = datetime.now(UTC)
        alert.acknowledged_by_user_id = principal.user.id
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="monitoring.alert.acknowledged",
            resource_type="monitoring_alert",
            resource_id=alert.id,
            request_id=getattr(request.state, "request_id", None),
            details={"service_id": alert.service_id, "code": alert.code},
        )
        monitoring_changed(db)
    await db.flush()
    return ok(_alert_out(alert))
