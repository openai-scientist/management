from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, update

from management.research.models import PiConsultation, RunEvent, RunGate, StageRun
from management.research.run_event_stream import run_events_changed

PLATFORM_EVENT_TYPES = frozenset(
    {
        "stage.status_changed",
        "decision.recorded",
        "iteration.started",
        "budget.updated",
        "engine.health_changed",
        "attention.required",
        "pi.consultation_requested",
        "pi.decision",
        "pi.recommendation",
    }
)


def append_run_event(
    db,
    run,
    stage,
    event_type,
    payload,
    *,
    actor=None,
    source_seq=None,
    envelope=None,
    event_class=None,
    summary=None,
):
    """Add an event to the run's stream. `stage` is None for events before the first stage."""
    run.last_seq = (run.last_seq or 0) + 1
    db.add(
        RunEvent(
            run_id=run.id,
            stage_run_id=stage.id if stage else None,
            seq=run.last_seq,
            source_seq=source_seq,
            type=event_type,
            stage_key=stage.stage_key if stage else None,
            step_key=envelope.step_key if envelope else (stage.step_key if stage else None),
            iteration=stage.iteration if stage else run.iteration,
            phase=envelope.phase if envelope else (stage.phase if stage else None),
            event_class=envelope.event_class if envelope else (event_class or "platform"),
            summary=envelope.summary if envelope else summary,
            refs=list(envelope.refs) if envelope else [],
            created_at=envelope.occurred_at if envelope else datetime.now(UTC),
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
        if stage.status == "FAILED" and stage.failure:
            run.failure_message = stage.failure.get("message")
    if run.status in {"failed", "stopped", "completed"}:
        run.finished_at = datetime.now(UTC)


async def recompute_run_cost(db, run) -> None:
    """The run's cost is what its stage runs and its PI consultations cost."""
    await db.flush()
    stages = await db.scalar(
        select(func.coalesce(func.sum(StageRun.cost_usd), 0)).where(
            StageRun.research_run_id == run.id
        )
    )
    consultations = await db.scalar(
        select(func.coalesce(func.sum(PiConsultation.cost_usd), 0)).where(
            PiConsultation.research_run_id == run.id
        )
    )
    run.cost_usd = Decimal(str(stages)) + Decimal(str(consultations))
