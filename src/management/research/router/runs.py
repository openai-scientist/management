import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.audit.service import record_audit
from management.core.config import Settings
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.sessions import (
    Principal,
    get_principal,
    require_active_csrf,
    require_active_principal,
    require_origin,
)
from management.db.session import get_db
from management.infra.file_store import FileStore, get_file_store
from management.projects.project_status import refresh_project_status
from management.research.contracts import InteractionMode
from management.research.models import (
    ACTIVE_RUN_STATUSES,
    FINISHED_RUN_STATUSES,
    ResearchRun,
    RunEvent,
    RunGate,
)
from management.research.orchestrator import start_research_run, stop_research_run
from management.research.pi.service import flush_consultations, mark_overridden
from management.research.popper_client import (
    PopperClient,
    PopperNotFound,
    PopperRejected,
    PopperUnavailable,
    PopperUncertain,
    get_popper_client,
)
from management.research.run_event_stream import RunEventHub, event_item, run_events_changed
from management.research.schemas.runs import (
    RUN_ERRORS,
    GateAnswer,
    RunCreate,
    RunEventItem,
    RunItem,
    RunStatus,
)
from management.research.service import (
    _ensure_not_dispatching,
    _item,
    _require_popper,
    _run_item,
    _run_query,
    apply_run_status,
    gate_answer_summary,
    get_run,
    ingest_popper_state,
    normalize_popper_status,
)

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/projects/{project_id}/runs", tags=["runs"])


KEEP_ALIVE_SECONDS = 15


class StopIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


@router.get(
    "",
    response_model=ApiResponse[list[RunItem]],
    summary="List the project's runs, newest first",
    responses={404: RUN_ERRORS[404]},
)
async def list_runs(
    project_id: UUID,
    status: RunStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[RunItem]]:
    await require_project_access(db, principal, project_id)
    filters = [ResearchRun.project_id == project_id]
    if status is not None:
        filters.append(ResearchRun.status == status)
    total = int(await db.scalar(select(func.count()).select_from(ResearchRun).where(*filters)) or 0)
    rows = (
        await db.execute(
            _run_query()
            .where(*filters)
            .order_by(ResearchRun.created_at.desc(), ResearchRun.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return paginated(
        [_run_item(run, version, context_version) for run, version, context_version in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ApiResponse[RunItem],
    status_code=201,
    summary="Start a run",
    description=(
        "Starts a topic-to-hypothesis research run. Answers 202 with a queued run "
        "when Popper did not confirm in time."
    ),
    responses={
        **RUN_ERRORS,
        202: {"model": ApiResponse[RunItem], "description": "Popper has not confirmed yet"},
    },
)
async def create_run(
    project_id: UUID,
    body: RunCreate,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    popper: PopperClient | None = Depends(get_popper_client),
) -> ApiResponse[RunItem]:
    settings: Settings = request.app.state.settings
    request_id = getattr(request.state, "request_id", None)
    pipeline_mode = bool(settings.engines)
    if not pipeline_mode:
        popper = _require_popper(popper)

    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    if project.status == "completed":
        raise APIError(409, "PROJECT_COMPLETED", "Reopen the project before starting a run")

    budget = body.budget_usd if body.budget_usd is not None else settings.run_default_budget_usd
    if not 0 < budget <= settings.run_max_budget_usd:
        raise APIError(
            422,
            "BUDGET_OUT_OF_RANGE",
            f"The budget must be above 0 and at most {settings.run_max_budget_usd} USD",
        )

    active = await db.scalar(
        select(ResearchRun.id).where(
            ResearchRun.project_id == project_id, ResearchRun.status.in_(ACTIVE_RUN_STATUSES)
        )
    )
    if active is not None and not pipeline_mode:
        raise APIError(409, "RUN_ACTIVE", "This project already has a run in progress")

    topic = body.topic.strip()
    if len(topic) < 12:
        raise APIError(422, "VALIDATION_ERROR", "Topic must be at least 12 characters")
    domains = [d.strip() for d in body.domains if d and d.strip()]
    seen_domains: set[str] = set()
    cleaned_domains: list[str] = []
    for d in domains:
        if d.lower() not in seen_domains:
            seen_domains.add(d.lower())
            cleaned_domains.append(d)
    if not cleaned_domains:
        raise APIError(422, "VALIDATION_ERROR", "At least one research domain is required")

    if pipeline_mode:
        interaction_mode = body.interaction_mode or InteractionMode(body.review_mode)
        run = await start_research_run(
            db,
            request.app.state,
            project=project,
            principal=principal,
            topic=topic,
            interaction_mode=interaction_mode,
            budget_usd=budget,
            domains=cleaned_domains,
        )
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="run.created",
            resource_type="run",
            resource_id=run.id,
            project_id=project_id,
            request_id=request_id,
            details={
                "topic": topic,
                "domains": cleaned_domains,
                "interaction_mode": str(interaction_mode),
                "budget_usd": str(budget),
            },
        )
        if run.charter_status == "pending":
            await flush_consultations(db, request.app.state, run.id)
        return ok(await _item(db, run), "Run started")

    auto_review = body.review_mode == "auto"
    run = ResearchRun(
        project_id=project_id,
        topic=topic,
        domains=cleaned_domains,
        review_mode=body.review_mode,
        created_by_user_id=principal.user.id,
        status="queued",
        auto_review=auto_review,
        budget_usd=budget,
    )
    db.add(run)
    await db.flush()
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="run.created",
        resource_type="run",
        resource_id=run.id,
        project_id=project_id,
        request_id=request_id,
        details={
            "topic": topic,
            "domains": cleaned_domains,
            "review_mode": body.review_mode,
            "budget_usd": str(budget),
        },
    )
    await refresh_project_status(db, project)
    await db.commit()

    failure: APIError | None = None
    popper_run_id: str | None = None
    callback_url = (
        f"{settings.public_base_url.rstrip('/')}{settings.api_prefix.rstrip('/')}"
        f"/internal/popper/runs/{run.id}"
    )
    try:
        popper_run_id = await popper.start_run(
            platform_run_id=run.id,
            topic=topic,
            domains=cleaned_domains,
            review_mode=body.review_mode,
            budget_usd=budget,
            callback_url=callback_url,
        )
    except PopperUncertain:
        response.status_code = 202
        return ok(await _item(db, run), "Popper has not confirmed the run yet")
    except PopperUnavailable:
        failure = APIError(502, "POPPER_UNAVAILABLE", "Popper could not be reached")
    except PopperRejected as exc:
        failure = APIError(422, "POPPER_REJECTED", str(exc))
    except Exception:
        logger.exception("could not send run %s to Popper", run.id)
        failure = APIError(500, "RUN_START_FAILED", "The run could not be sent to Popper")

    await lock_project_scope(db, project_id)
    run = await get_run(db, project_id, run.id, lock=True)
    if failure is not None:
        await apply_run_status(db, run, "failed", message=failure.message, request_id=request_id)
        await db.commit()
        raise failure
    run.popper_run_id = popper_run_id
    if run.status == "queued":
        await apply_run_status(
            db, run, "running", actor_user_id=principal.user.id, request_id=request_id
        )
    return ok(await _item(db, run), "Run started")


@router.get(
    "/{run_id}",
    response_model=ApiResponse[RunItem],
    summary="Get a run",
    responses={404: RUN_ERRORS[404]},
)
async def get_run_route(
    project_id: UUID,
    run_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RunItem]:
    await require_project_access(db, principal, project_id)
    return ok(await _item(db, await get_run(db, project_id, run_id)))


@router.get(
    "/{run_id}/events",
    response_model=ApiResponse[list[RunEventItem]],
    summary="Get paginated run events",
    responses={404: RUN_ERRORS[404]},
)
async def get_run_events(
    project_id: UUID,
    run_id: UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=1000),
    stage_run_id: UUID | None = None,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[RunEventItem]]:
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    filters = [RunEvent.run_id == run.id, RunEvent.seq > after]
    if stage_run_id is not None:
        filters.append(RunEvent.stage_run_id == stage_run_id)
    total = int(await db.scalar(select(func.count()).select_from(RunEvent).where(*filters)) or 0)
    rows = (
        (
            await db.execute(
                select(RunEvent).where(*filters).order_by(RunEvent.seq.asc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    items = [event_item(event) for event in rows]
    return paginated(items, total=total, limit=limit, offset=0)


@router.get(
    "/{run_id}/events/stream",
    summary="SSE stream for run events",
)
async def stream_run_events(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    after: int | None = Query(default=None, ge=0),
    stage_run_id: UUID | None = None,
) -> StreamingResponse:
    if request.headers.get("Origin") is not None:
        require_origin(request)

    last_event_id_header = request.headers.get("Last-Event-ID")
    start_seq = 0
    if last_event_id_header is not None:
        try:
            start_seq = max(0, int(last_event_id_header))
        except ValueError:
            pass
    elif after is not None:
        start_seq = after

    factory = request.app.state.session_factory
    async with factory() as db:
        principal = await get_principal(request, db)
        await require_project_access(db, principal, project_id)
        await get_run(db, project_id, run_id)
        await db.commit()

    hub: RunEventHub | None = getattr(request.app.state, "run_event_hub", None)

    async def event_generator():
        yield "retry: 3000\n\n"
        current_seq = start_seq

        def event_query():
            query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > current_seq)
            if stage_run_id is not None:
                query = query.where(RunEvent.stage_run_id == stage_run_id)
            return query.order_by(RunEvent.seq.asc())

        # Historical events stream without pacing
        # Read the status first: a terminal status then means the query saw every event.
        async with factory() as db:
            run_status = await db.scalar(select(ResearchRun.status).where(ResearchRun.id == run_id))
            historical = (await db.execute(event_query())).scalars().all()
            for evt in historical:
                current_seq = evt.seq
                item = event_item(evt)
                data = item.model_dump_json(by_alias=True)
                yield f"event: run-event\nid: {evt.seq}\ndata: {data}\n\n"

            if run_status in ("completed", "failed", "budget_exceeded", "stopped"):
                yield f'event: run-ended\ndata: {{"status":"{run_status}"}}\n\n'
                return

        queue = None
        queue_ctx = None
        if hub:
            try:
                queue_ctx = hub.subscribe(run_id)
                queue = await queue_ctx.__aenter__()
            except Exception as exc:
                logger.warning("Could not subscribe to RunEventHub: %s", exc)

        try:
            while True:
                had_new_events = False
                try:
                    async with factory() as db:
                        await get_principal(request, db)
                        run_status = await db.scalar(
                            select(ResearchRun.status).where(ResearchRun.id == run_id)
                        )
                        new_events = (await db.execute(event_query())).scalars().all()
                        for evt in new_events:
                            current_seq = evt.seq
                            had_new_events = True
                            item = event_item(evt)
                            data = item.model_dump_json(by_alias=True)
                            yield f"event: run-event\nid: {evt.seq}\ndata: {data}\n\n"

                        if run_status in ("completed", "failed", "budget_exceeded", "stopped"):
                            yield f'event: run-ended\ndata: {{"status":"{run_status}"}}\n\n'
                            return
                except APIError as exc:
                    yield f'event: session-ended\ndata: {{"code":"{exc.code}"}}\n\n'
                    return

                if not had_new_events:
                    yield ": keep-alive\n\n"

                if queue:
                    try:
                        await asyncio.wait_for(queue.get(), timeout=KEEP_ALIVE_SECONDS)
                    except TimeoutError:
                        pass
                else:
                    await asyncio.sleep(3)
        finally:
            if queue_ctx:
                await queue_ctx.__aexit__(None, None, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post(
    "/{run_id}/gates/{gate_id}",
    summary="Answer an open gate",
    responses=RUN_ERRORS,
)
async def answer_gate(
    project_id: UUID,
    run_id: UUID,
    gate_id: str,
    body: GateAnswer,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    popper: PopperClient | None = Depends(get_popper_client),
):
    request_id = getattr(request.state, "request_id", None)
    popper = _require_popper(popper)
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)

    run = await get_run(db, project_id, run_id, lock=True)
    if run.status in FINISHED_RUN_STATUSES:
        raise APIError(409, "RUN_FINISHED", "Run is already finished")

    gate = await db.scalar(
        select(RunGate)
        .where(RunGate.run_id == run.id, RunGate.gate_key == gate_id)
        .with_for_update()
    )
    if gate is None:
        raise APIError(404, "NOT_FOUND", "Gate was not found")

    if gate.answer is not None:
        if (
            gate.answer.get("option_id") == body.option_id
            and gate.answer.get("dropped") == body.dropped
            and (gate.answer.get("kept") or []) == body.kept
        ):
            return ok(
                {"gate_id": gate.gate_key, "resolved_seq": gate.resolved_seq},
                "Answer recorded",
            )
        raise APIError(409, "GATE_ALREADY_RESOLVED", "Gate was already answered")

    spec = gate.spec or {}
    options = spec.get("options", [])
    matched_option = next((opt for opt in options if opt.get("id") == body.option_id), None)
    if not matched_option or matched_option.get("disabled", False):
        raise APIError(422, "INVALID_GATE_OPTION", f"Invalid or disabled option {body.option_id}")

    droppable = spec.get("droppable", [])
    if body.dropped:
        if not droppable:
            raise APIError(422, "INVALID_DROP", "This gate does not allow dropping items")
        droppable_set = set(droppable)
        for d in body.dropped:
            if d not in droppable_set:
                raise APIError(422, "INVALID_DROP", f"Item {d} is not in droppable list")
        if len(set(body.dropped)) != len(body.dropped):
            raise APIError(422, "INVALID_DROP", "Duplicate items in dropped list")
        if len(body.dropped) >= len(droppable) and len(droppable) > 0 and not body.kept:
            item = "paper" if gate.kind == "screen" else "hypothesis"
            raise APIError(422, "DROP_ALL_NOT_ALLOWED", f"Keep at least one {item}")
    if body.option_id == "drop" and not (body.dropped or body.kept):
        raise APIError(422, "INVALID_DROP", "Choose at least one item to drop or keep")
    if body.kept:
        keepable = set(spec.get("keepable", []))
        if not keepable:
            raise APIError(422, "INVALID_KEEP", "This gate does not hold anything back")
        for k in body.kept:
            if k not in keepable:
                raise APIError(422, "INVALID_KEEP", f"Item {k} was not set aside")
        if len(set(body.kept)) != len(body.kept):
            raise APIError(422, "INVALID_KEEP", "Duplicate items in kept list")

    clean_note = body.note.strip() if body.note else None
    # The engine hears the answer first: one it refuses (the gate moved on, an item it does not
    # know) is not recorded, so the run never waits on an answer the engine never took. When the
    # engine cannot be reached the answer is still recorded, as before.
    try:
        engine_run_id = run.popper_run_id or str(run.id)
        await popper.answer_gate(
            engine_run_id,
            gate_id=gate.gate_key,
            decision={
                "option_id": body.option_id,
                "dropped": body.dropped,
                "kept": body.kept,
                "note": clean_note,
            },
        )
    except PopperRejected as exc:
        raise APIError(422, "GATE_ANSWER_REJECTED", str(exc)) from exc
    except Exception as exc:
        logger.warning("Could not immediately notify engine of answered gate: %s", exc)

    now = datetime.now(UTC)
    gate.answer = {
        "option_id": body.option_id,
        "dropped": body.dropped,
        "kept": body.kept,
        "note": clean_note,
    }
    gate.answered_by_user_id = principal.user.id
    gate.answered_at = now

    resolved_seq = run.last_seq + 1
    run.last_seq = resolved_seq
    gate.resolved_seq = resolved_seq

    gate_event = RunEvent(
        run_id=run.id,
        seq=resolved_seq,
        source_seq=None,
        type="gate.resolved",
        # The screen and scope gates belong to the UI groups of the same name.
        stage_key=spec.get("stage_key") or gate.kind,
        actor="pi",
        payload={
            "gate_id": gate.gate_key,
            "kind": gate.kind,
            "option_id": body.option_id,
            "dropped": body.dropped,
            "kept": body.kept,
            "note": clean_note,
            "answer": dict(gate.answer),
            "summary": gate_answer_summary(gate.kind, body.option_id, body.dropped, body.kept),
        },
        created_at=now,
    )
    db.add(gate_event)

    new_status = "paused" if run.status == "paused" else "running"
    run.status = new_status
    status_seq = run.last_seq + 1
    run.last_seq = status_seq
    status_event = RunEvent(
        run_id=run.id,
        seq=status_seq,
        source_seq=None,
        type="run.status",
        payload={"status": new_status},
        created_at=now,
    )
    db.add(status_event)

    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="run.gate_answered",
        resource_type="run",
        resource_id=run.id,
        project_id=project_id,
        request_id=request_id,
        details={"gate_id": gate.gate_key, "option_id": body.option_id},
    )
    run_events_changed(db, run.id)
    await db.commit()

    return ok({"gate_id": gate.gate_key, "resolved_seq": resolved_seq}, "Answer recorded")


@router.post(
    "/{run_id}/pause",
    summary="Pause a running run",
    responses=RUN_ERRORS,
)
async def pause_run(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    popper: PopperClient | None = Depends(get_popper_client),
) -> ApiResponse[RunItem]:
    request_id = getattr(request.state, "request_id", None)
    popper = _require_popper(popper)
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    if run.status in FINISHED_RUN_STATUSES:
        raise APIError(409, "RUN_FINISHED", "This run has already finished")
    if run.status == "queued":
        _ensure_not_dispatching(run, request.app.state.settings)

    if run.status == "paused":
        return ok(await _item(db, run), "Run already paused")

    now = datetime.now(UTC)
    run.status = "paused"
    seq = run.last_seq + 1
    run.last_seq = seq
    db.add(
        RunEvent(
            run_id=run.id,
            seq=seq,
            source_seq=None,
            type="run.status",
            payload={"status": "paused"},
            created_at=now,
        )
    )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="run.paused",
        resource_type="run",
        resource_id=run.id,
        project_id=project_id,
        request_id=request_id,
    )
    run_events_changed(db, run.id)
    await db.commit()

    try:
        await popper.pause(run.popper_run_id or str(run.id))
    except Exception as exc:
        logger.warning("Could not notify engine of pause: %s", exc)

    return ok(await _item(db, run), "Run paused")


@router.post(
    "/{run_id}/resume",
    summary="Resume a paused run",
    responses=RUN_ERRORS,
)
async def resume_run(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    popper: PopperClient | None = Depends(get_popper_client),
) -> ApiResponse[RunItem]:
    request_id = getattr(request.state, "request_id", None)
    popper = _require_popper(popper)
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    if run.status in FINISHED_RUN_STATUSES:
        raise APIError(409, "RUN_FINISHED", "This run has already finished")

    if run.status != "paused":
        return ok(await _item(db, run), "Run not paused")

    open_gate = await db.scalar(
        select(RunGate).where(RunGate.run_id == run.id, RunGate.answer.is_(None))
    )
    new_status = "awaiting_review" if open_gate else "running"

    now = datetime.now(UTC)
    run.status = new_status
    seq = run.last_seq + 1
    run.last_seq = seq
    db.add(
        RunEvent(
            run_id=run.id,
            seq=seq,
            source_seq=None,
            type="run.status",
            payload={"status": new_status},
            created_at=now,
        )
    )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="run.resumed",
        resource_type="run",
        resource_id=run.id,
        project_id=project_id,
        request_id=request_id,
    )
    run_events_changed(db, run.id)
    await db.commit()

    try:
        await popper.resume(run.popper_run_id or str(run.id))
    except Exception as exc:
        logger.warning("Could not notify engine of resume: %s", exc)

    return ok(await _item(db, run), "Run resumed")


@router.post(
    "/{run_id}/stop",
    response_model=ApiResponse[RunItem],
    summary="Stop a pipeline run",
    description="Cancels the open stage on its engine. The run ends as stopped.",
    responses=RUN_ERRORS,
)
async def stop_run(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    body: StopIn | None = None,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[RunItem]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    await stop_research_run(
        db,
        request.app.state,
        run,
        actor="user",
        user_id=principal.user.id,
        reason=body.reason if body else None,
    )
    await mark_overridden(
        db,
        request.app.state,
        run,
        kinds=(
            "charter",
            "gate_question",
            "gate_review",
            "stage_review",
            "stage_outcome",
            "failure",
        ),
        what="stopped the run",
        reason=body.reason if body else None,
    )
    return ok(await _item(db, run), "Run stopped")


@router.post(
    "/{run_id}/sync",
    response_model=ApiResponse[RunItem],
    summary="Ask Popper for the run's current state",
    description=(
        "Use when a run looks stuck. A run Popper no longer knows, or never received, "
        "is marked failed so the project can start another. Answers 409 RUN_DISPATCHING "
        "while a run that was just created may still be on its way to Popper."
    ),
    responses=RUN_ERRORS,
)
async def sync_run(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
    popper: PopperClient | None = Depends(get_popper_client),
) -> ApiResponse[RunItem]:
    request_id = getattr(request.state, "request_id", None)
    popper = _require_popper(popper)
    await require_project_access(db, principal, project_id, contribute=True)
    run = await get_run(db, project_id, run_id)
    if run.status not in ACTIVE_RUN_STATUSES:
        return ok(await _item(db, run))
    _ensure_not_dispatching(run, request.app.state.settings)

    try:
        if run.popper_run_id is None:
            state = await popper.find_run(run.id)
        else:
            try:
                state = await popper.get_run(run.popper_run_id)
            except PopperNotFound:
                state = None
    except (PopperUnavailable, PopperUncertain) as exc:
        raise APIError(502, "POPPER_UNAVAILABLE", "Popper could not be reached") from exc

    await lock_project_scope(db, project_id)
    run = await get_run(db, project_id, run_id, lock=True)
    if run.status not in ACTIVE_RUN_STATUSES:
        return ok(await _item(db, run))
    if state is None:
        lost = (
            "Popper never received this run"
            if run.popper_run_id is None
            else ("Popper no longer has this run")
        )
        await apply_run_status(db, run, "failed", message=lost, request_id=request_id)
        return ok(await _item(db, run), "Run marked failed")

    try:
        status, stage = normalize_popper_status(state.status)
    except ValueError as exc:
        raise APIError(502, "POPPER_BAD_RESPONSE", "Popper reported an unknown status") from exc
    if run.popper_run_id is None:
        run.popper_run_id = state.popper_run_id
    try:
        await ingest_popper_state(
            db,
            store,
            run,
            status=status,
            cost_usd=state.cost_usd,
            message=state.message or stage,
            review=state.review,
            review_sequence=state.review_sequence,
            request_id=request_id,
        )
    except APIError as exc:
        if exc.code != "REVIEW_REQUIRED":
            raise
        raise APIError(
            502, "POPPER_BAD_RESPONSE", "Popper did not include the frame to review"
        ) from exc
    return ok(await _item(db, run), "Run synchronized")


@router.post(
    "/{run_id}/abandon",
    response_model=ApiResponse[RunItem],
    summary="Give up on a run that cannot finish",
    description=(
        "Project Manager only. Marks a run in progress as failed so the project can start "
        "another. Also notifies Popper to cancel its background tasks."
    ),
    responses={**RUN_ERRORS, 403: {"model": ErrorResponse, "description": "Project Manager only"}},
)
async def abandon_run(
    project_id: UUID,
    run_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    popper: PopperClient | None = Depends(get_popper_client),
) -> ApiResponse[RunItem]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True)
    ensure_writable_project(project)
    run = await get_run(db, project_id, run_id, lock=True)
    if run.status not in ACTIVE_RUN_STATUSES:
        raise APIError(409, "RUN_FINISHED", "This run has already finished")
    _ensure_not_dispatching(run, request.app.state.settings)
    request_id = getattr(request.state, "request_id", None)
    await apply_run_status(
        db,
        run,
        "failed",
        message="Abandoned by a project manager",
        actor_user_id=principal.user.id,
        request_id=request_id,
    )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="run.abandoned",
        resource_type="run",
        resource_id=run.id,
        project_id=project_id,
        request_id=request_id,
        details={"popper_run_id": run.popper_run_id},
    )
    run_events_changed(db, run.id)
    await db.commit()

    if popper and run.popper_run_id:
        try:
            await popper.cancel(run.popper_run_id)
        except Exception as exc:
            logger.warning("Could not notify Popper of cancel: %s", exc)

    return ok(await _item(db, run), "Run abandoned")
