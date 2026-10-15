from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.core.errors import APIError
from management.core.responses import ApiResponse, ok
from management.core.sessions import (
    Principal,
    require_active_csrf,
    require_active_principal,
)
from management.db.session import get_db
from management.research.contracts import Finding, GateAnswer
from management.research.models import StageDecision, StageRun
from management.research.orchestrator import (
    accept_stage,
    rerun_stage,
    send_back,
    validate_selection,
)
from management.research.orchestrator import answer_stage_gate as answer_gate_service
from management.research.pi.service import flush_consultations, mark_overridden, pi_active
from management.research.reviews import normalize_comment, review_history
from management.research.schemas.reviews import ReviewRoundItem
from management.research.schemas.runs import RUN_ERRORS, CurrentStage
from management.research.service import current_stage_item, get_run

router = APIRouter(prefix="/projects/{project_id}/runs/{run_id}/stages", tags=["run stages"])


class StageItem(CurrentStage):
    iteration: int
    prior_stage_run_id: str | None = None
    failure: dict[str, Any] | None = None
    created_at: datetime
    finished_at: datetime | None = None


class GateAnswerIn(BaseModel):
    """What a client may send; the server sets actor and decided_by."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["answer", "approve", "comment"]
    option_id: str | None = Field(default=None, max_length=200)
    text: str | None = Field(default=None, max_length=4000)
    findings: list[Finding] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = Field(default=None, max_length=2000)


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: dict[str, list[str]] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=2000)
    reason: str | None = Field(default=None, max_length=2000)


class SendBackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_stage_key: str = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=2000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str | None = Field(default=None, max_length=2000)


class RerunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


def stage_item(stage: StageRun) -> StageItem:
    return StageItem(
        **current_stage_item(stage).model_dump(),
        iteration=stage.iteration,
        prior_stage_run_id=str(stage.prior_stage_run_id) if stage.prior_stage_run_id else None,
        failure=stage.failure,
        created_at=stage.created_at,
        finished_at=stage.finished_at,
    )


async def _stage(db: AsyncSession, run, stage_run_id: UUID, *, lock: bool = False) -> StageRun:
    query = select(StageRun).where(StageRun.id == stage_run_id, StageRun.research_run_id == run.id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    stage = await db.scalar(query)
    if stage is None:
        raise APIError(404, "NOT_FOUND", "Stage run was not found")
    return stage


@router.get(
    "",
    response_model=ApiResponse[list[StageItem]],
    summary="List the stage runs of a research run",
    responses={404: RUN_ERRORS[404]},
)
async def list_stages(
    project_id: UUID,
    run_id: UUID,
    iteration: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[StageItem]]:
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    query = select(StageRun).where(StageRun.research_run_id == run.id)
    if iteration is not None:
        query = query.where(StageRun.iteration == iteration)
    stages = await db.scalars(query.order_by(StageRun.iteration, StageRun.created_at))
    return ok([stage_item(stage) for stage in stages])


@router.post(
    "/{stage_run_id}/gates/{gate_id}",
    summary="Answer the open gate of a stage",
    responses=RUN_ERRORS,
)
async def answer_stage_gate(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID,
    gate_id: str,
    body: GateAnswerIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    stage = await _stage(db, run, stage_run_id, lock=True)
    payload = await normalize_comment(db, stage, body.model_dump(mode="json", exclude={"reason"}))
    try:
        answer = GateAnswer(**payload, actor="user", decided_by=str(principal.user.id))
    except ValidationError:
        raise APIError(422, "VALIDATION_ERROR", "Invalid gate answer") from None
    await answer_gate_service(
        db,
        request.app.state,
        stage,
        answer,
        actor="user",
        user_id=principal.user.id,
        reason=body.reason,
        gate_id=gate_id,
    )
    await mark_overridden(
        db,
        request.app.state,
        run,
        kinds=("gate_question", "gate_review"),
        stage_run_id=stage.id,
        gate_id=gate_id,
        what=f"{body.action} at the gate",
        reason=body.reason,
    )
    return ok({"gate_id": gate_id, "status": "applied"}, "Answer recorded")


@router.get("/{stage_run_id}/reviews", response_model=ApiResponse[list[ReviewRoundItem]])
async def list_reviews(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[ReviewRoundItem]]:
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    stage = await _stage(db, run, stage_run_id)
    return ok(await review_history(db, stage))


@router.post(
    "/{stage_run_id}/accept",
    summary="Accept a completed stage and start the next one",
    responses=RUN_ERRORS,
)
async def accept(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID,
    body: AcceptIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    stage = await _stage(db, run, stage_run_id, lock=True)
    if stage.status != "COMPLETED":
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage is not completed")
    await validate_selection(db, request.app.state, stage, body.selection)
    next_stage = await accept_stage(
        db,
        request.app.state,
        stage,
        selection=body.selection,
        note=body.note,
        actor="user",
        user_id=principal.user.id,
        reason=body.reason,
    )
    await mark_overridden(
        db,
        request.app.state,
        run,
        kinds=("stage_outcome", "stage_review"),
        stage_run_id=stage.id,
        what="accepted the stage",
        reason=body.reason,
        selection=body.selection,
        note=body.note,
    )
    if pi_active(request.app.state, run):
        await flush_consultations(db, request.app.state, run.id)
    return ok(
        {
            "stage_run_id": str(stage_run_id),
            "status": "ACCEPTED",
            "next_stage_run_id": str(next_stage.id) if next_stage else None,
        },
        "Stage accepted",
    )


async def _started_by(db: AsyncSession, decision: StageDecision) -> StageRun | None:
    return await db.scalar(select(StageRun).where(StageRun.origin_decision_id == decision.id))


@router.post(
    "/{stage_run_id}/send-back",
    summary="Send a completed stage back to an earlier stage or to itself",
    responses=RUN_ERRORS,
)
async def send_back_stage(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID,
    body: SendBackIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    stage = await _stage(db, run, stage_run_id, lock=True)
    decision = await send_back(
        db,
        request.app.state,
        stage,
        target_stage_key=body.target_stage_key,
        note=body.note,
        artifact_ids=body.artifact_ids,
        actor="user",
        user_id=principal.user.id,
        reason=body.reason,
    )
    await mark_overridden(
        db,
        request.app.state,
        run,
        kinds=("stage_outcome", "stage_review"),
        stage_run_id=stage.id,
        what=f"sent the stage back to {body.target_stage_key}",
        reason=body.reason,
        note=body.note,
    )
    if pi_active(request.app.state, run):
        await flush_consultations(db, request.app.state, run.id)
    new = await _started_by(db, decision)
    return ok(
        {
            "stage_run_id": str(stage_run_id),
            "status": stage.status,
            "new_stage_run_id": str(new.id) if new else None,
        },
        "Stage sent back",
    )


@router.post(
    "/{stage_run_id}/rerun",
    summary="Start a failed stage again with the same inputs",
    responses=RUN_ERRORS,
)
async def rerun(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID,
    body: RerunIn,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
):
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    stage = await _stage(db, run, stage_run_id, lock=True)
    decision = await rerun_stage(
        db, request.app.state, stage, actor="user", user_id=principal.user.id, reason=body.reason
    )
    await mark_overridden(
        db,
        request.app.state,
        run,
        kinds=("failure",),
        stage_run_id=stage.id,
        what="started the stage again",
        reason=body.reason,
    )
    if pi_active(request.app.state, run):
        await flush_consultations(db, request.app.state, run.id)
    new = await _started_by(db, decision)
    return ok(
        {
            "stage_run_id": str(stage_run_id),
            "status": stage.status,
            "new_stage_run_id": str(new.id) if new else None,
        },
        "Stage started again",
    )
