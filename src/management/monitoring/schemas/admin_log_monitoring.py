from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MonitoringAlertOut(BaseModel):
    id: str
    service_id: str
    severity: Literal["warning", "critical"]
    code: str
    message: str
    started_at: datetime
    resolved_at: datetime | None
    acknowledged_at: datetime | None
    acknowledged_by_user_id: str | None
    details: dict[str, Any]


class ServiceMetrics(BaseModel):
    request_rate_per_second: float | None
    error_rate_percent: float | None
    latency_ms: float | None
    cpu_percent: float | None = None
    memory_percent: float | None = None
    queue_depth: float | None = None
    available: bool = Field(
        description="True only when capture coverage and sample limits are complete."
    )
    reason_code: str | None = Field(
        default=None,
        description=(
            "CAPTURE_NOT_STARTED, CAPTURE_INCOMPLETE, or SAMPLE_LIMIT_EXCEEDED when unavailable."
        ),
    )


class LogEventOut(BaseModel):
    id: str
    service_id: str
    service: str
    environment: str
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    message: str
    created_at: datetime
    duration_ms: float | None
    status: str | None
    request_id: str | None
    trace_id: str | None
    project_id: str | None
    run_id: str | None
    actor_user_id: str | None
    provider_id: str | None
    method: str | None
    route: str | None


class LogEventDetail(LogEventOut):
    payload: dict[str, Any]


class MonitoringServiceOut(BaseModel):
    id: str
    code: str
    name: str
    category: str
    environment: str
    status: Literal["healthy", "warning", "critical", "unknown"]
    observed_at: datetime | None
    stale_after_seconds: int
    metrics_window_seconds: int
    latency_statistic: Literal["p95"]
    metrics: ServiceMetrics
    alerts: list[MonitoringAlertOut]
    recent_events: list[LogEventOut]


class MetricsPoint(BaseModel):
    timestamp: datetime
    request_rate_per_second: float | None
    error_rate_percent: float | None
    latency_ms: float | None
    cpu_percent: float | None
    memory_percent: float | None
    queue_depth: float | None


class ServiceMetricsData(BaseModel):
    service_id: str
    generated_at: datetime
    window: dict[str, datetime]
    interval_seconds: int
    latency_statistic: Literal["p95"]
    points: list[MetricsPoint]
    available: bool = Field(
        description="False when capture coverage or the bounded sample limit is incomplete."
    )
    reason_code: str | None = Field(
        default=None,
        description=(
            "CAPTURE_NOT_STARTED, CAPTURE_INCOMPLETE, or SAMPLE_LIMIT_EXCEEDED when unavailable."
        ),
    )
