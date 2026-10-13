from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from management.audit.models import AuditEvent
from management.audit.service import record_audit


async def test_events_recorded_at_same_clock_tick_retain_call_order(harness, monkeypatch):
    instant = datetime(2026, 10, 14, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz):
            return instant

    monkeypatch.setattr("management.audit.models.datetime", Clock)
    monkeypatch.setattr("management.audit.service.datetime", Clock, raising=False)
    async with harness.factory() as db:
        for action in ("first", "second"):
            record_audit(
                db, actor_user_id=None, action=action, resource_type="test", resource_id="1"
            )
        await db.flush()
        rows = list(await db.scalars(select(AuditEvent).order_by(AuditEvent.created_at)))
        assert rows[0].created_at.replace(tzinfo=UTC) == instant
        assert rows[1].created_at.replace(tzinfo=UTC) == instant + timedelta(microseconds=1)
