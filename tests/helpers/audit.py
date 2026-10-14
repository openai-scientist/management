import json

from sqlalchemy import select

from management.audit.models import AuditEvent
from tests.helpers.harness import Harness


async def audit_events(harness: Harness, action: str) -> list[AuditEvent]:
    async with harness.factory() as db:
        return list(await db.scalars(select(AuditEvent).where(AuditEvent.action == action)))


async def every_audit_detail(harness: Harness) -> str:
    async with harness.factory() as db:
        return json.dumps([event.details for event in await db.scalars(select(AuditEvent))])
