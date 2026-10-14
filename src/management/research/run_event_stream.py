"""Commit-aware invalidations and pub-sub for run event SSE streams."""

from uuid import UUID

# pyrefly: ignore [missing-import]
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from management.notifications.notification_stream import NotificationHub, stream_changed
from management.research.models import RunEvent
from management.research.schemas.runs import RunEventItem

CHANNEL = "platform_run_events"
RUNS_KEY = "run_event_stream_runs"


def event_item(event: RunEvent) -> RunEventItem:
    return RunEventItem(
        seq=event.seq,
        run_id=str(event.run_id),
        ts=event.created_at,
        type=event.type,
        stage_key=event.stage_key,
        stage_run_id=str(event.stage_run_id) if event.stage_run_id else None,
        step_key=event.step_key,
        iteration=event.iteration,
        phase=event.phase,
        event_class=event.event_class,
        summary=event.summary,
        refs=event.refs or [],
        actor=event.actor,
        payload=event.payload or {},
    )


def run_events_changed(db: AsyncSession, run_id: UUID) -> None:
    """Invalidate or notify subscribers of new events for this run after commit."""
    stream_changed(db.sync_session, RUNS_KEY, run_id)


class RunEventHub(NotificationHub):
    """One PostgreSQL listener per API process for run events.

    Signals carry the UUID of the run that has new events committed.
    Subscribed streams receive the signal, query new events from DB, and stream them.
    SQLite falls back to direct signaling after commit for local tests.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine, channel=CHANNEL, recipients_key=RUNS_KEY)
