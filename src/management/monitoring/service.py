import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.errors import APIError
from management.monitoring.models import MonitoringAlert, MonitoringEvent, MonitoringWorkerState
from management.monitoring.queries import (
    SERVICE_METRICS_WINDOW_SECONDS,
    aggregate_requests,
    check_capture_coverage,
    event_to_public,
    monitoring_service_id,
    parse_service_id,
    request_events,
)
from management.monitoring.schemas.admin_log_monitoring import (
    LogEventOut,
    MonitoringAlertOut,
    MonitoringServiceOut,
    ServiceMetrics,
)

RETENTION_MAX_DAYS = 14


WindowInput = tuple[datetime, datetime]


def _alert_out(alert: MonitoringAlert) -> MonitoringAlertOut:
    return MonitoringAlertOut(
        id=str(alert.id),
        service_id=alert.service_id,
        severity=alert.severity,
        code=alert.code,
        message=alert.message,
        started_at=_as_utc(alert.started_at),
        resolved_at=_as_utc(alert.resolved_at),
        acknowledged_at=_as_utc(alert.acknowledged_at),
        acknowledged_by_user_id=(
            str(alert.acknowledged_by_user_id) if alert.acknowledged_by_user_id else None
        ),
        details=alert.details,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)


def _event_out(event: MonitoringEvent) -> LogEventOut:
    public = event_to_public(event, environment=event.environment)
    return LogEventOut.model_validate(public)


def _resolve_service(service_id: str, environment: str | None = None) -> str:
    parsed = parse_service_id(service_id)
    if parsed is None:
        raise APIError(404, "NOT_FOUND", "Monitoring service was not found")
    _, service_environment = parsed
    if environment is not None and environment != service_environment:
        raise APIError(
            422, "SERVICE_ENVIRONMENT_MISMATCH", "Service and environment filters must match"
        )
    return service_environment


def _validate_window(
    from_time: datetime | None,
    to_time: datetime | None,
    *,
    now: datetime,
    default_seconds: int,
    max_days: int = RETENTION_MAX_DAYS,
    stale_allowance_seconds: int = 0,
) -> WindowInput:
    if (from_time is None) != (to_time is None):
        raise APIError(422, "INVALID_TIME_RANGE", "Provide both 'from' and 'to' timestamps")
    end = to_time or now - timedelta(seconds=stale_allowance_seconds)
    start = from_time or end - timedelta(seconds=default_seconds)
    if start.utcoffset() is None or end.utcoffset() is None:
        raise APIError(422, "TIMEZONE_REQUIRED", "Monitoring timestamps must include a timezone")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if start >= end:
        raise APIError(422, "INVALID_TIME_RANGE", "The 'from' timestamp must be before 'to'")
    if end - start > timedelta(days=max_days):
        raise APIError(
            422, "TIME_RANGE_TOO_LARGE", f"Monitoring windows cannot exceed {max_days} days"
        )
    if end > now:
        raise APIError(422, "INVALID_TIME_RANGE", "The 'to' timestamp cannot be in the future")
    return start, end


def _sse_event(name: str, payload: Any) -> str:
    if name in {"session-ended", "access-ended"}:
        return f"event: {name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
    if hasattr(payload, "model_dump_json"):
        data = payload.model_dump_json()
    else:
        data = json.dumps(
            payload,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
    return f"event: {name}\ndata: {data}\n\n"


async def _pipeline_status(db: AsyncSession, *, environment: str, now: datetime) -> dict[str, Any]:
    states = list(
        (
            await db.scalars(
                select(MonitoringWorkerState).where(
                    MonitoringWorkerState.service == "platform-api",
                    MonitoringWorkerState.environment == environment,
                )
            )
        ).all()
    )
    active = [state for state in states if state.status in ("starting", "ready", "degraded")]
    if not active:
        telemetry_status = "unknown"
        observed_at = None
    elif any(_as_utc(state.heartbeat_at) < now - timedelta(seconds=45) for state in active):
        telemetry_status = "unknown"
        observed_at = min(_as_utc(state.heartbeat_at) for state in active)
    else:
        telemetry_status = (
            "degraded"
            if any(state.status != "ready" or state.dropped_count > 0 for state in active)
            else "healthy"
        )
        observed_at = min(_as_utc(state.heartbeat_at) for state in active)
    return {
        "telemetry_connected": telemetry_status,
        "audit_pipeline_status": "unknown",
        "observed_at": observed_at,
        "worker_count": len(active),
    }


async def _service_item(
    db: AsyncSession,
    *,
    environment: str,
    now: datetime,
    health_rules_enabled: bool,
    health_minimum_samples: int | None,
    health_threshold_percent: float | None,
) -> MonitoringServiceOut:
    service_id = monitoring_service_id(environment)
    end = now - timedelta(seconds=45)
    start = end - timedelta(seconds=SERVICE_METRICS_WINDOW_SECONDS)
    coverage = await check_capture_coverage(
        db, environment=environment, start=start, end=end, now=now
    )
    events, exceeded = await request_events(db, environment=environment, start=start, end=end)
    available = coverage.available and not exceeded
    aggregate = aggregate_requests(events, SERVICE_METRICS_WINDOW_SECONDS)
    active_alerts = list(
        (
            await db.scalars(
                select(MonitoringAlert)
                .where(MonitoringAlert.service_id == service_id, MonitoringAlert.status == "active")
                .order_by(MonitoringAlert.started_at.desc(), MonitoringAlert.id.desc())
                .limit(5)
            )
        ).all()
    )
    recent_events = list(
        (
            await db.scalars(
                select(MonitoringEvent)
                .where(
                    MonitoringEvent.service == "platform-api",
                    MonitoringEvent.environment == environment,
                    MonitoringEvent.created_at >= start,
                    MonitoringEvent.created_at < now,
                )
                .order_by(MonitoringEvent.created_at.desc(), MonitoringEvent.id.desc())
                .limit(5)
            )
        ).all()
    )
    status: Literal["healthy", "warning", "critical", "unknown"] = "unknown"
    if (
        available
        and health_rules_enabled
        and health_minimum_samples is not None
        and health_threshold_percent is not None
        and aggregate.request_count >= health_minimum_samples
        and aggregate.error_rate_percent is not None
    ):
        status = (
            "warning" if aggregate.error_rate_percent >= health_threshold_percent else "healthy"
        )
        if active_alerts:
            status = (
                "critical"
                if any(alert.severity == "critical" for alert in active_alerts)
                else "warning"
            )
    metric_reason = "SAMPLE_LIMIT_EXCEEDED" if exceeded else coverage.reason_code
    return MonitoringServiceOut(
        id=service_id,
        code="platform-api",
        name="Platform API",
        category="api",
        environment=environment,
        status=status,
        observed_at=coverage.observed_at,
        stale_after_seconds=45,
        metrics_window_seconds=SERVICE_METRICS_WINDOW_SECONDS,
        latency_statistic="p95",
        metrics=ServiceMetrics(
            request_rate_per_second=aggregate.request_rate if available else None,
            error_rate_percent=aggregate.error_rate_percent if available else None,
            latency_ms=aggregate.p95_latency_ms if available else None,
            available=available,
            reason_code=metric_reason,
        ),
        alerts=[_alert_out(alert) for alert in active_alerts],
        recent_events=[_event_out(event) for event in recent_events],
    )
