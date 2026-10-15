from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.core.errors import APIError
from management.core.responses import ApiResponse, ok
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.research.contracts import Charter
from management.research.models import PI_OPEN_STATUSES, PiConsultation, PiNote
from management.research.pi.apply import endorse, put_charter, retry
from management.research.pi.authority import KINDS, authority_for
from management.research.pi.service import add_note, flush_consultations
from management.research.schemas.runs import RUN_ERRORS
from management.research.service import get_run

router = APIRouter(prefix="/projects/{project_id}/runs/{run_id}/pi", tags=["run pi"])


class ConsultationItem(BaseModel):
    id: str
    kind: str
    stage_run_id: str | None = None
    stage_key: str | None = None
    gate_id: str | None = None
    authority: str
    status: str
    result: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)
    failure: dict[str, Any] | None = None
    applied_decision_id: str | None = None
    cost_usd: Decimal
    created_at: datetime
    sent_at: datetime | None = None
    decided_at: datetime | None = None
    resolved_at: datetime | None = None


class NoteItem(BaseModel):
    id: str
    kind: str
    text: str
    stage_key: str | None = None
    actor: str
    consultation_id: str | None = None
    created_at: datetime


class PiOverview(BaseModel):
    enabled: bool
    charter: dict[str, Any] | None = None
    charter_status: str
    obligations: list[dict[str, Any]] = Field(default_factory=list)
    authority: dict[str, str]
    stage_outcome_authority: dict[str, str]
    open_consultations: list[ConsultationItem]


class EndorseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class CharterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    charter: Charter
    reason: str | None = Field(default=None, max_length=2000)


class DirectiveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


def consultation_item(row: PiConsultation) -> ConsultationItem:
    result = dict(row.result) if row.result else None
    trace = result.pop("trace", []) if result else []
    return ConsultationItem(
        id=str(row.id),
        kind=row.kind,
        stage_run_id=str(row.stage_run_id) if row.stage_run_id else None,
        stage_key=(row.request.get("situation") or {}).get("stage_key"),
        gate_id=row.gate_id,
        authority=row.authority,
        status=row.status,
        result=result,
        trace=trace,
        failure=row.failure,
        applied_decision_id=str(row.applied_decision_id) if row.applied_decision_id else None,
        cost_usd=row.cost_usd,
        created_at=row.created_at,
        sent_at=row.sent_at,
        decided_at=row.decided_at,
        resolved_at=row.resolved_at,
    )


def require_enabled(request: Request) -> None:
    if request.app.state.settings.pi_service is None:
        raise APIError(409, "PI_NOT_ENABLED", "The AI PI is not configured on this server")


async def _writable_run(
    db: AsyncSession, request: Request, principal: Principal, project_id, run_id
):
    require_enabled(request)
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    return await get_run(db, project_id, run_id, lock=True)


@router.get("", response_model=ApiResponse[PiOverview], summary="The PI state of a run")
async def overview(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
):
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    settings = request.app.state.settings
    enabled = settings.pi_service is not None and run.charter_status != "none"
    pipeline = settings.pipelines.get(run.pipeline_key) or []
    open_rows = (
        await db.scalars(
            select(PiConsultation)
            .where(
                PiConsultation.research_run_id == run.id,
                PiConsultation.status.in_(PI_OPEN_STATUSES),
            )
            .order_by(PiConsultation.created_at)
        )
        if enabled
        else []
    )
    return ok(
        PiOverview(
            enabled=enabled,
            charter=run.charter,
            charter_status=run.charter_status,
            obligations=run.obligations or [],
            authority={kind: authority_for(run, kind) for kind in KINDS if kind != "stage_outcome"},
            stage_outcome_authority={
                config.stage_key: authority_for(run, "stage_outcome", config) for config in pipeline
            },
            open_consultations=[consultation_item(row) for row in open_rows],
        )
    )


@router.get("/notebook", response_model=ApiResponse[list[NoteItem]], summary="The lab notebook")
async def notebook(
    project_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
):
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    notes = await db.scalars(
        select(PiNote)
        .where(PiNote.research_run_id == run.id)
        .order_by(PiNote.created_at, PiNote.id)
    )
    return ok(
        [
            NoteItem(
                id=str(note.id),
                kind=note.kind,
                text=note.text,
                stage_key=note.stage_key,
                actor=note.actor,
                consultation_id=str(note.consultation_id) if note.consultation_id else None,
                created_at=note.created_at,
            )
            for note in notes
        ]
    )


@router.get(
    "/consultations",
    response_model=ApiResponse[list[ConsultationItem]],
    summary="Every consultation with the PI, with its decision and trace",
)
async def consultations(
    project_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
):
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    rows = await db.scalars(
        select(PiConsultation)
        .where(PiConsultation.research_run_id == run.id)
        .order_by(PiConsultation.created_at, PiConsultation.id)
    )
    return ok([consultation_item(row) for row in rows])


@router.post(
    "/consultations/{consultation_id}/endorse",
    summary="Use the PI recommendation as the researcher decision",
    responses=RUN_ERRORS,
)
async def endorse_consultation(
    project_id: UUID,
    run_id: UUID,
    consultation_id: UUID,
    request: Request,
    body: EndorseIn | None = None,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    run = await _writable_run(db, request, principal, project_id, run_id)
    consultation = await db.get(PiConsultation, consultation_id)
    if consultation is None or consultation.research_run_id != run.id:
        raise APIError(404, "NOT_FOUND", "Consultation was not found")
    row = await endorse(
        db,
        request.app.state,
        consultation_id,
        user_id=principal.user.id,
        project_id=project_id,
        reason=body.reason if body else None,
    )
    status = row.status
    await flush_consultations(db, request.app.state, run.id)
    return ok({"consultation_id": str(consultation_id), "status": status}, "Recommendation used")


@router.post(
    "/consultations/{consultation_id}/retry",
    summary="Ask the PI again after a failed or stale consultation",
    responses=RUN_ERRORS,
)
async def retry_consultation(
    project_id: UUID,
    run_id: UUID,
    consultation_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    run = await _writable_run(db, request, principal, project_id, run_id)
    consultation = await db.get(PiConsultation, consultation_id)
    if consultation is None or consultation.research_run_id != run.id:
        raise APIError(404, "NOT_FOUND", "Consultation was not found")
    new = await retry(db, request.app.state, consultation_id, project_id=project_id)
    new_id = str(new.id)
    await flush_consultations(db, request.app.state, run.id)
    return ok({"consultation_id": new_id, "status": "queued"}, "Consultation queued again")


@router.put(
    "/charter",
    summary="Write or approve the charter yourself",
    responses=RUN_ERRORS,
)
async def write_charter(
    project_id: UUID,
    run_id: UUID,
    body: CharterIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    run = await _writable_run(db, request, principal, project_id, run_id)
    await put_charter(db, request.app.state, run, body.charter, reason=body.reason)
    await flush_consultations(db, request.app.state, run.id)
    return ok({"charter_status": run.charter_status, "charter": run.charter}, "Charter saved")


@router.post(
    "/directives",
    status_code=201,
    summary="Give the PI a directive for the rest of the run",
    responses=RUN_ERRORS,
)
async def add_directive(
    project_id: UUID,
    run_id: UUID,
    body: DirectiveIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    run = await _writable_run(db, request, principal, project_id, run_id)
    if run.charter_status == "none":
        raise APIError(409, "PI_NOT_ENABLED", "This run has no PI")
    note = add_note(
        db, run, "directive", body.text.strip(), "user", stage_key=run.current_stage_key
    )
    await db.flush()
    return ok({"id": str(note.id), "kind": "directive"}, "Directive recorded")
