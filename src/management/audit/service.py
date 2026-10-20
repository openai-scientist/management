from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.models import AuditEvent


def record_audit(
    db: AsyncSession,
    *,
    actor_user_id: UUID | None,
    action: str,
    resource_type: str,
    resource_id: UUID | str,
    request_id: str | None = None,
    project_id: UUID | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    created_at = datetime.now(UTC)
    previous = db.info.get("last_audit_timestamp")
    if previous is not None and created_at <= previous:
        created_at = previous + timedelta(microseconds=1)
    db.info["last_audit_timestamp"] = created_at
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            request_id=request_id,
            project_id=project_id,
            details=details or {},
            created_at=created_at,
        )
    )
