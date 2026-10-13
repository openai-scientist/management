from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_be.api.v1.runs import RUN_ERRORS, CurrentStage, current_stage_item
from platform_be.auth.sessions import (
    Principal,
    require_active_csrf,
    require_active_principal,
)
from platform_be.contracts import Finding, GateAnswer
from platform_be.core.errors import APIError
from platform_be.core.responses import ApiResponse, ok
from platform_be.db.session import get_db
from platform_be.models import StageDecision, StageRun
from platform_be.services.access import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from platform_be.services.engines.client import EngineRejected, EngineUnavailable
from platform_be.services.orchestrator import accept_stage, validate_selection
from platform_be.services.runs import get_run
from platform_be.services.stage_runs import append_run_event

router = APIRouter(prefix="/projects/{project_id}/runs/{run_id}/stages", tags=["run stages"])

DECISION_ACTION = {"answer": "answer_gate", "approve": "approve", "comment": "comment"}


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


class AcceptIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection: dict[str, list[str]] = Field(default_factory=dict)
    note: str | None = Field(default=None, max_length=2000)


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
    try:
        answer = GateAnswer(**body.model_dump(), actor="user", decided_by=str(principal.user.id))
    except ValidationError:
        raise APIError(422, "VALIDATION_ERROR", "Invalid gate answer") from None
    payload = body.model_dump(mode="json")
    decision = await db.scalar(
        select(StageDecision).where(
            StageDecision.stage_run_id == stage.id, StageDecision.gate_id == gate_id
        )
    )
    if decision is not None:
        # The same answer again is a retry; a different one cannot reopen the gate.
        if decision.payload != payload:
            raise APIError(409, "GATE_NOT_OPEN", "Gate was already answered")
        if decision.status == "applied":
            return ok({"gate_id": gate_id, "status": "applied"}, "Answer recorded")
    else:
        if not stage.open_gate or stage.open_gate.get("gate_id") != gate_id:
            raise APIError(409, "GATE_NOT_OPEN", "Gate is not open")
        decision = StageDecision(
            stage_run_id=stage.id,
            gate_id=gate_id,
            action=DECISION_ACTION[body.action],
            payload=payload,
            actor="user",
            status="pending",
            decided_by_user_id=principal.user.id,
        )
        db.add(decision)
        await db.flush()
    if not stage.engine_run_id:
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage has not started on the engine")
    decision_id, engine_key, engine_run_id = decision.id, stage.engine_key, stage.engine_run_id
    # The engine calls back while it answers; commit first so the callback sees the stage.
    await db.commit()
    try:
        await request.app.state.engines.get(engine_key).answer_gate(engine_run_id, gate_id, answer)
    except EngineUnavailable:
        raise APIError(503, "ENGINE_UNAVAILABLE", "Engine could not be reached") from None
    except EngineRejected as exc:
        await db.delete(await db.get(StageDecision, decision_id))
        await db.commit()
        raise APIError(
            409 if exc.status_code == 409 else 422, exc.code, "Engine rejected the answer"
        ) from None
    run = await get_run(db, project_id, run_id, lock=True)
    stage = await _stage(db, run, stage_run_id)
    decision = await db.get(StageDecision, decision_id, populate_existing=True)
    decision.status = "applied"
    append_run_event(
        db,
        run,
        stage,
        "decision.recorded",
        {"action": decision.action, "gate_id": gate_id},
        actor="user",
    )
    return ok({"gate_id": gate_id, "status": "applied"}, "Answer recorded")


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
    )
    return ok(
        {
            "stage_run_id": str(stage_run_id),
            "status": "ACCEPTED",
            "next_stage_run_id": str(next_stage.id) if next_stage else None,
        },
        "Stage accepted",
    )
