"""Consultations: ask the PI, send the question after commit, record who overrode it."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from management.core.errors import APIError
from management.core.roles import ProjectRole
from management.notifications.service import notify_project_members
from management.research.contracts import ConsultationRequest, PIModels
from management.research.model_profiles import load_critic_model, load_models
from management.research.models import (
    ACTIVE_RUN_STATUSES,
    PI_OPEN_STATUSES,
    ModelProfile,
    PiConsultation,
    PiNote,
    ResearchRun,
    StageRun,
)
from management.research.pi import context
from management.research.pi.authority import authority_for
from management.research.pi.client import PIError, PIUnavailable
from management.research.stage_runs import append_run_event

logger = logging.getLogger(__name__)

#: A Critic that has not answered after this long is treated as unavailable.
STAGE_REVIEW_TIMEOUT = timedelta(minutes=20)


def pi_active(app_state, run) -> bool:
    """PI is configured and the run has a charter flow. Older runs keep the old behavior."""
    return app_state.settings.pi_service is not None and run.charter_status != "none"


def run_active(run) -> bool:
    return run.status in ACTIVE_RUN_STATUSES or (
        run.status == "budget_exceeded" and run.pipeline_key is not None
    )


def stage_config(app_state, run, stage_key):
    return next(
        (c for c in app_state.settings.pipelines[run.pipeline_key] if c.stage_key == stage_key),
        None,
    )


def add_note(db, run, kind, text, actor, *, consultation_id=None, stage_key=None):
    note = PiNote(
        research_run_id=run.id,
        consultation_id=consultation_id,
        kind=kind,
        text=text,
        actor=actor,
        stage_key=stage_key,
    )
    db.add(note)
    return note


async def emit_attention(db, run, stage_run, payload) -> None:
    append_run_event(
        db,
        run,
        stage_run,
        "attention.required",
        {
            "stage_run_id": str(stage_run.id) if stage_run else None,
            "gate_id": (stage_run.open_gate or {}).get("gate_id") if stage_run else None,
        }
        | payload,
    )
    await notify_project_members(
        db,
        run.project_id,
        "run_awaiting_review",
        run_id=run.id,
        roles=(ProjectRole.MANAGER, ProjectRole.RESEARCHER),
    )


def _strip_secrets(request: ConsultationRequest) -> dict:
    """`model_dump` masks SecretStr, but the stored copy must not even hold the mask."""
    data = request.model_dump(mode="json", by_alias=True)
    for role in ("lead", "critic"):
        data["models"][role]["api_key"] = None
    return data


async def open_consultation(db, run, kind, stage_run):
    return await db.scalar(
        select(PiConsultation).where(
            PiConsultation.research_run_id == run.id,
            PiConsultation.kind == kind,
            PiConsultation.stage_run_id.is_(None)
            if stage_run is None
            else PiConsultation.stage_run_id == stage_run.id,
            PiConsultation.status.in_(PI_OPEN_STATUSES),
        )
    )


async def pi_models(db, app_state, kind, models) -> PIModels:
    """The lead is the review model. The Critic of a `stage_review` has its own profile."""
    critic = (
        await load_critic_model(db, app_state.secret_box)
        if kind == "stage_review"
        else models.stage
    )
    return PIModels(lead=models.review, critic=critic)


async def request_consultation(db, app_state, run, kind, *, stage_run=None) -> PiConsultation:
    """Queue a question for the PI in the caller's transaction; `flush_consultations` sends it.

    One consultation stays open per (stage run, kind). The researcher is told at once
    when the point is theirs; the PI's recommendation follows.
    """
    existing = await open_consultation(db, run, kind, stage_run)
    if existing is not None:
        return existing
    config = stage_config(app_state, run, stage_run.stage_key) if stage_run else None
    authority = authority_for(run, kind, config)
    consultation_id = uuid4()
    models = await load_models(db, app_state.secret_box)
    request = await context.build_request(
        db,
        app_state,
        run,
        kind,
        consultation_id=consultation_id,
        authority=authority,
        models=await pi_models(db, app_state, kind, models),
        stage_run=stage_run,
    )
    consultation = PiConsultation(
        id=consultation_id,
        research_run_id=run.id,
        stage_run_id=stage_run.id if stage_run else None,
        kind=kind,
        authority=authority,
        status="queued",
        request=_strip_secrets(request),
    )
    db.add(consultation)
    await db.flush()
    append_run_event(
        db,
        run,
        stage_run,
        "pi.consultation_requested",
        {"consultation_id": str(consultation_id), "kind": kind, "authority": authority},
    )
    if authority == "researcher":
        await emit_attention(
            db,
            run,
            stage_run,
            {
                "consultation_id": str(consultation_id),
                "kind": kind,
                "reason": "researcher_decision",
            },
        )
    return consultation


async def flush_consultations(db, app_state, run_id, *, answer_gates=True) -> None:
    """Commit, then send what is queued and let the review-gate rule answer its gates.

    The PI may answer at once, so nothing stays locked. The engine event endpoint sets
    `answer_gates=False` and runs the rule after its response, so the rule's call to the engine
    does not nest inside the engine's own event delivery.
    """
    await db.commit()
    # After the caller's commit: an error while expiring must not roll the caller's work back.
    await expire_stage_reviews(db, app_state, run_id)
    await dispatch_queued(app_state, run_id)
    if answer_gates:
        from management.research.pi.review_gate import answer_review_gates_safely  # noqa: PLC0415

        # The caller's own change is committed: a rule failure goes to the researcher, not to it.
        await answer_review_gates_safely(app_state, run_id)


async def expire_stage_reviews(db, app_state, run_id) -> None:
    """A Critic that never answers must not hold the stage: go on without its report.

    Each row commits on its own. A row that fails is logged and left for the next sweep.
    """
    cutoff = datetime.now(UTC) - STAGE_REVIEW_TIMEOUT
    ids = list(
        await db.scalars(
            select(PiConsultation.id).where(
                PiConsultation.research_run_id == run_id,
                PiConsultation.kind == "stage_review",
                PiConsultation.status.in_(("queued", "sent")),
                PiConsultation.created_at < cutoff,
            )
        )
    )
    for consultation_id in ids:
        try:
            row = await db.scalar(
                select(PiConsultation)
                .where(PiConsultation.id == consultation_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row.status in {"queued", "sent"} and row.result is None:
                await fail_consultation(
                    db,
                    row,
                    "critic_timeout",
                    "The Critic did not answer in time",
                    app_state=app_state,
                )
            await db.commit()
        except Exception:  # noqa: BLE001 - one bad row must not block the others
            logger.exception("Expiring stage review %s failed", consultation_id)
            await db.rollback()


async def sweep(app_state) -> None:
    """Recover what no event triggers: a Critic that never answered, and a review gate whose
    background answer was lost (for example, a restart between the engine event and the task)."""
    cutoff = datetime.now(UTC) - STAGE_REVIEW_TIMEOUT
    async with app_state.session_factory() as db:
        late = await db.scalars(
            select(PiConsultation.research_run_id).where(
                PiConsultation.kind == "stage_review",
                PiConsultation.status.in_(("queued", "sent")),
                PiConsultation.created_at < cutoff,
            )
        )
        gated = await db.execute(
            select(StageRun.research_run_id, StageRun.open_gate).where(StageRun.status == "RUNNING")
        )
        run_ids = set(late) | {run_id for run_id, gate in gated if gate}
    for run_id in run_ids:
        try:
            async with app_state.session_factory() as db:
                app_state.notification_hub.bind(db)
                app_state.run_event_hub.bind(db)
                await flush_consultations(db, app_state, run_id)
        except Exception:  # noqa: BLE001 - one run must not stop the sweep of the others
            logger.exception("PI sweep failed for run %s", run_id)


async def sweep_forever(app_state, interval: float = 60.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep(app_state)
        except Exception:  # noqa: BLE001
            logger.exception("PI sweep failed")


async def dispatch_queued(app_state, run_id) -> None:
    client = getattr(app_state, "pi_client", None)
    if client is None:
        return
    async with app_state.session_factory() as db:
        app_state.notification_hub.bind(db)
        app_state.run_event_hub.bind(db)
        # A Critic that cannot be reached queues the stage outcome question: send that too.
        while rows := list(
            await db.scalars(
                select(PiConsultation)
                .where(PiConsultation.research_run_id == run_id, PiConsultation.status == "queued")
                .order_by(PiConsultation.created_at)
            )
        ):
            await _send(db, app_state, client, rows)


async def _send(db, app_state, client, rows) -> None:
    now = datetime.now(UTC)
    for row in rows:
        row.status, row.sent_at = "sent", now
    await db.commit()
    try:
        models = await load_models(db, app_state.secret_box)
    except APIError:
        models = None
    for row in rows:
        failure = None
        request = None
        if models is not None:
            try:
                request = ConsultationRequest.model_validate(row.request).model_copy(
                    update={"models": await pi_models(db, app_state, row.kind, models)}
                )
            except APIError:
                pass
        if request is None:
            failure = ("models_not_configured", "The PI models are not configured")
        else:
            try:
                await client.consult(request)
            except PIUnavailable:
                failure = ("pi_unavailable", "The PI could not be reached")
            except PIError:
                failure = ("pi_rejected", "The PI rejected the consultation")
        if failure is not None:
            # The PI may have answered through the callback before the error came back.
            row = await db.scalar(
                select(PiConsultation)
                .where(PiConsultation.id == row.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if row.status == "sent" and row.result is None:
                await fail_consultation(db, row, *failure, app_state=app_state)
        await db.commit()


async def fail_consultation(db, consultation, code, message, *, app_state=None) -> None:
    """The PI cannot decide: the researcher does, and may retry.

    A failed `stage_review` is no one's decision: the PI is asked about the outcome without it.
    """
    consultation.status = "failed"
    consultation.failure = {"code": code, "message": message}
    consultation.resolved_at = datetime.now(UTC)
    run = await db.scalar(
        select(ResearchRun)
        .where(ResearchRun.id == consultation.research_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stage_run = (
        await db.get(StageRun, consultation.stage_run_id) if consultation.stage_run_id else None
    )
    if consultation.kind == "stage_review":
        await critic_unavailable(db, app_state, run, stage_run, message)
        return
    await emit_attention(
        db,
        run,
        stage_run,
        {
            "consultation_id": str(consultation.id),
            "kind": consultation.kind,
            "reason": "pi_failed",
            "code": code,
        },
    )


async def critic_unavailable(db, app_state, run, stage_run, reason) -> None:
    """Write why there is no Critic report and ask the PI about the outcome without it."""
    if stage_run is None or stage_run.status != "COMPLETED" or not run_active(run):
        return
    add_note(
        db,
        run,
        "observation",
        f"Critic report unavailable: {reason}",
        "pi",
        stage_key=stage_run.stage_key,
    )
    await request_consultation(db, app_state, run, "stage_outcome", stage_run=stage_run)


async def result_cost(db, result, kind) -> tuple[Decimal, bool]:
    """Cost of a PI result: the lead ran the review model, the critic the stage model.

    The Critic of a `stage_review` ran its own profile, else the review one. (usd, known).
    """
    profiles = {
        row.role: row
        for row in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }
    total, known = Decimal("0"), True
    for role, usage in result.usage.items():
        if role == "lead":
            profile = profiles.get("review")
        elif kind == "stage_review":
            profile = profiles.get("critic") or profiles.get("review")
        else:
            profile = profiles.get("stage")
        if (
            profile is None
            or profile.input_usd_per_mtok is None
            or profile.output_usd_per_mtok is None
        ):
            known = False
            continue
        total += (
            Decimal(profile.input_usd_per_mtok) * usage.input_tokens
            + Decimal(profile.output_usd_per_mtok) * usage.output_tokens
        ) / 1_000_000
    return total, known


def describe_selection(selection: dict | None) -> str:
    """``hypothesis_ids: H1, H2`` for a selection; empty when nothing is selected."""
    parts = []
    for key, value in (selection or {}).items():
        if value:
            items = value if isinstance(value, list) else [value]
            parts.append(f"{key}: {', '.join(str(item) for item in items)}")
    return "; ".join(parts)


async def mark_overridden(
    db,
    app_state,
    run,
    *,
    kinds,
    stage_run_id=None,
    gate_id=None,
    what,
    reason=None,
    selection=None,
    note=None,
) -> None:
    """A researcher decided on a point where the PI was asked: record it in the notebook.

    The entry keeps what the researcher chose (selection, note), not only that they chose, so
    a later consultation can follow it (live: a narrowed hypothesis set was lost on send-back).
    """
    if not pi_active(app_state, run):
        return
    query = select(PiConsultation).where(
        PiConsultation.research_run_id == run.id,
        PiConsultation.kind.in_(kinds),
        PiConsultation.status.in_(PI_OPEN_STATUSES),
    )
    if stage_run_id is not None:
        query = query.where(PiConsultation.stage_run_id == stage_run_id)
    if gate_id is not None:
        query = query.where(PiConsultation.gate_id == gate_id)
    chosen = describe_selection(selection)
    noted = False
    for consultation in list(await db.scalars(query)):
        decision = (consultation.result or {}).get("decision") or {}
        recommended = decision.get("action")
        consultation.status = "overridden"
        consultation.resolved_at = datetime.now(UTC)
        if consultation.kind == "stage_review":
            continue  # the Critic had not reported: nothing the researcher overrode
        text = f"The researcher overrode the PI at {consultation.kind}: {what}"
        text += f", selecting {chosen}." if chosen else "."
        if recommended:
            text += f" The PI had recommended {recommended}"
            proposed = describe_selection(decision.get("selection"))
            text += f" with {proposed}." if proposed and proposed != chosen else "."
        if reason:
            text += f" Reason: {reason}"
        if note:
            text += f" Researcher's note: {note}"
        add_note(
            db,
            run,
            "override",
            text,
            "user",
            consultation_id=consultation.id,
            stage_key=(consultation.request.get("situation") or {}).get("stage_key"),
        )
        noted = True
    # Live v11: a send-back while only the Critic review was open lost the researcher's note.
    # What the researcher chose still binds later consultations when the PI had no say yet.
    if not noted and (note or chosen) and stage_run_id is not None:
        stage_run = await db.get(StageRun, stage_run_id)
        text = f"The researcher decided before the PI: {what}"
        text += f", selecting {chosen}." if chosen else "."
        if reason:
            text += f" Reason: {reason}"
        if note:
            text += f" Researcher's note: {note}"
        add_note(
            db, run, "override", text, "user", stage_key=stage_run.stage_key if stage_run else None
        )
