"""Consultations: ask the PI, send the question after commit, record who overrode it."""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from management.core.errors import APIError
from management.core.roles import ProjectRole
from management.notifications.service import notify_project_members
from management.research.contracts import ConsultationRequest, PIModels
from management.research.model_profiles import load_models
from management.research.models import (
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


def pi_active(app_state, run) -> bool:
    """PI is configured and the run has a charter flow. Older runs keep the old behavior."""
    return app_state.settings.pi_service is not None and run.charter_status != "none"


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


async def open_consultation(db, run, kind, stage_run, gate_id):
    return await db.scalar(
        select(PiConsultation).where(
            PiConsultation.research_run_id == run.id,
            PiConsultation.kind == kind,
            PiConsultation.stage_run_id.is_(None)
            if stage_run is None
            else PiConsultation.stage_run_id == stage_run.id,
            PiConsultation.gate_id.is_(None)
            if gate_id is None
            else PiConsultation.gate_id == gate_id,
            PiConsultation.status.in_(PI_OPEN_STATUSES),
        )
    )


async def request_consultation(
    db, app_state, run, kind, *, stage_run=None, gate=None
) -> PiConsultation:
    """Queue a question for the PI in the caller's transaction; `flush_consultations` sends it.

    One consultation stays open per (stage run, kind, gate). The researcher is told at once
    when the point is theirs; the PI's recommendation follows.
    """
    gate_id = gate.get("gate_id") if gate else None
    existing = await open_consultation(db, run, kind, stage_run, gate_id)
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
        models=models,
        stage_run=stage_run,
        gate=gate,
    )
    consultation = PiConsultation(
        id=consultation_id,
        research_run_id=run.id,
        stage_run_id=stage_run.id if stage_run else None,
        kind=kind,
        gate_id=gate_id,
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
        {
            "consultation_id": str(consultation_id),
            "kind": kind,
            "authority": authority,
            "gate_id": gate_id,
        },
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


async def flush_consultations(db, app_state, run_id) -> None:
    """Commit, then send what is queued. The PI may answer at once, so nothing stays locked."""
    await db.commit()
    await dispatch_queued(app_state, run_id)


async def dispatch_queued(app_state, run_id) -> None:
    client = getattr(app_state, "pi_client", None)
    if client is None:
        return
    async with app_state.session_factory() as db:
        app_state.notification_hub.bind(db)
        app_state.run_event_hub.bind(db)
        rows = list(
            await db.scalars(
                select(PiConsultation)
                .where(PiConsultation.research_run_id == run_id, PiConsultation.status == "queued")
                .order_by(PiConsultation.created_at)
            )
        )
        if not rows:
            return
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
            if models is None:
                failure = ("models_not_configured", "The PI models are not configured")
            else:
                request = ConsultationRequest.model_validate(row.request).model_copy(
                    update={"models": PIModels(lead=models.review, critic=models.stage)}
                )
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
                    await fail_consultation(db, row, *failure)
            await db.commit()


async def fail_consultation(db, consultation, code, message) -> None:
    """The PI cannot decide: the researcher does, and may retry."""
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


async def result_cost(db, result) -> tuple[Decimal, bool]:
    """Cost of a PI result: lead uses the review model, critic the stage model. (usd, known)."""
    profiles = {
        row.role: row
        for row in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }
    total, known = Decimal("0"), True
    for role, usage in result.usage.items():
        profile = profiles.get("review" if role == "lead" else "stage")
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


async def mark_overridden(
    db, app_state, run, *, kinds, stage_run_id=None, gate_id=None, what, reason=None
) -> None:
    """A researcher decided on a point where the PI was asked: record it in the notebook."""
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
    for consultation in list(await db.scalars(query)):
        recommended = ((consultation.result or {}).get("decision") or {}).get("action")
        consultation.status = "overridden"
        consultation.resolved_at = datetime.now(UTC)
        text = f"The researcher overrode the PI at {consultation.kind}: {what}."
        if recommended:
            text += f" The PI had recommended {recommended}."
        if reason:
            text += f" Reason: {reason}"
        add_note(
            db,
            run,
            "override",
            text,
            "user",
            consultation_id=consultation.id,
            stage_key=(consultation.request.get("situation") or {}).get("stage_key"),
        )
