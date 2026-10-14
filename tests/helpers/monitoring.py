from datetime import datetime
from uuid import uuid4

from management.identity.bootstrap_admin import bootstrap_admin
from management.monitoring.models import MonitoringEvent
from tests.helpers.harness import Harness
from tests.helpers.identity import login

ADMIN_EMAIL = "log-monitoring-admin@example.com"


async def login_admin(harness: Harness, client):
    session = await login(harness, client, uid="log-monitoring-admin", email=ADMIN_EMAIL)
    await bootstrap_admin(ADMIN_EMAIL, settings=harness.settings, session_factory=harness.factory)
    return session


def request_event(*, created_at: datetime, status_code: int, duration_ms: float) -> MonitoringEvent:
    return MonitoringEvent(
        id=uuid4(),
        event_type="http.request.completed",
        service="platform-api",
        environment="test",
        worker_id="api-worker-test",
        level="error" if status_code >= 500 else "warning" if status_code >= 400 else "info",
        message="HTTP request completed",
        request_id=f"request-{uuid4()}",
        method="GET",
        route="/api/v1/projects/{project_id}",
        status_code=status_code,
        duration_ms=duration_ms,
        attributes={"request_body": "must not escape", "password": "secret"},
        created_at=created_at,
    )
