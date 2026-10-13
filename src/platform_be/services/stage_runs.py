from datetime import UTC, datetime

from sqlalchemy import select, update

from platform_be.models.research import RunEvent, RunGate, StageRun
from platform_be.services.run_event_stream import run_events_changed


def append_run_event(db, run, stage, event_type, payload, *, actor=None, source_seq=None):
    run.last_seq = (run.last_seq or 0) + 1
    db.add(
        RunEvent(
            run_id=run.id,
            stage_run_id=stage.id,
            seq=run.last_seq,
            source_seq=source_seq,
            type=event_type,
            stage_key=stage.stage_key,
            actor=actor,
            payload=payload,
        )
    )
    run_events_changed(db, run.id)


async def sync_run_status(db, run, *, stage=None) -> None:
    if stage is None:
        stage = await db.scalar(
            select(StageRun)
            .where(StageRun.research_run_id == run.id)
            .order_by(StageRun.iteration.desc(), StageRun.created_at.desc())
            .limit(1)
        )
    if stage is None:
        return
    if stage.status in {"FAILED", "STOPPED", "SUPERSEDED"}:
        await db.execute(
            update(RunGate)
            .where(RunGate.stage_run_id == stage.id, RunGate.answer.is_(None))
            .values(answer="closed", answered_at=datetime.now(UTC))
        )
        stage.open_gate = None
        stage.waiting_input = False
    if stage.status in {"PENDING", "RUNNING"}:
        if stage.phase == "paused":
            run.status = "budget_exceeded" if run.status == "budget_exceeded" else "paused"
        else:
            run.status = "awaiting_review" if stage.open_gate else "running"
    elif stage.status == "COMPLETED":
        run.status = "awaiting_review"
    elif stage.status in {"FAILED", "STOPPED", "ACCEPTED"}:
        run.status = {"FAILED": "failed", "STOPPED": "stopped", "ACCEPTED": "completed"}[
            stage.status
        ]
    if run.status in {"failed", "stopped", "completed"}:
        run.finished_at = datetime.now(UTC)
