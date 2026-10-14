import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select

from management.access.service import ensure_writable_project, lock_project_scope
from management.core.errors import APIError
from management.core.roles import ProjectRole
from management.notifications.service import notify_project_members
from management.projects.project_status import refresh_project_status
from management.research.contracts import (
    CONTRACT_VERSION,
    SELECTION_HYPOTHESIS_IDS,
    ArtifactRef,
    Decisions,
    Gate,
    Inputs,
    RunCost,
    RunCreateRequest,
    parse_schema_id,
    paths,
)
from management.research.engines.client import EngineContractMismatch, EngineUnavailable
from management.research.model_profiles import load_integrations, load_models
from management.research.models import (
    ModelProfile,
    ResearchRun,
    RunArtifact,
    RunGate,
    StageDecision,
    StageRun,
    active_run_filter,
)
from management.research.stage_runs import append_run_event, sync_run_status

ALLOWED = {
    ("RUNNING", "publish"): "COMPLETED",
    ("RUNNING", "fail"): "FAILED",
    ("PENDING", "fail"): "FAILED",
    ("RUNNING", "cancel"): "STOPPED",
    ("PENDING", "cancel"): "STOPPED",
    ("COMPLETED", "accept"): "ACCEPTED",
}


def transition(stage, action):
    target = ALLOWED.get((stage.status, action))
    if target is None:
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage does not allow this action")
    stage.status = target
    if target in {"COMPLETED", "FAILED", "STOPPED"}:
        stage.finished_at = datetime.now(UTC)


async def _capabilities(app_state, engine_key):
    try:
        return await app_state.engines.get(engine_key).capabilities()
    except EngineContractMismatch:
        raise APIError(
            409, "ENGINE_CONTRACT_MISMATCH", "Engine contract major version does not match"
        ) from None
    except EngineUnavailable:
        raise APIError(503, "ENGINE_UNAVAILABLE", "Engine could not be reached") from None


async def start_research_run(
    db,
    app_state,
    *,
    project,
    principal,
    topic,
    interaction_mode,
    budget_usd,
    pipeline_key=None,
    domains=None,
):
    settings = app_state.settings
    pipeline_key = pipeline_key or settings.default_pipeline
    pipeline = settings.pipelines.get(pipeline_key)
    if not pipeline:
        raise APIError(422, "PIPELINE_NOT_FOUND", "Unknown pipeline")
    ensure_writable_project(project)
    await lock_project_scope(db, project.id)
    if await db.scalar(
        select(ResearchRun.id).where(ResearchRun.project_id == project.id, active_run_filter())
    ):
        raise APIError(409, "PROJECT_HAS_ACTIVE_RUN", "Project already has an active run")
    for config in pipeline:
        caps = await _capabilities(app_state, config.engine)
        if config.stage_key not in caps.stage_keys:
            raise APIError(
                409, "ENGINE_CONTRACT_MISMATCH", "Engine does not support the pipeline stage"
            )
    await load_models(db, app_state.secret_box)
    run = ResearchRun(
        project_id=project.id,
        created_by_user_id=principal.user.id,
        topic=topic,
        domains=domains or [],
        status="running",
        pipeline_key=pipeline_key,
        interaction_mode=interaction_mode,
        iteration=1,
        budget_usd=Decimal(str(budget_usd)),
        started_at=datetime.now(UTC),
        review_mode="auto" if interaction_mode == "auto" else "copilot",
    )
    db.add(run)
    await db.flush()
    await start_stage(
        db,
        app_state,
        run,
        pipeline[0],
        inputs=Inputs(params={"topic": topic, "domains": run.domains}),
    )
    await refresh_project_status(db, project)
    return run


async def start_stage(
    db, app_state, run, stage, *, inputs: Inputs, prior_stage_run_id=None, origin_decision_id=None
):
    settings = app_state.settings
    caps = await _capabilities(app_state, stage.engine)
    models = await load_models(db, app_state.secret_box)
    if any(model.provider not in caps.providers for model in (models.stage, models.review)):
        raise APIError(
            422, "PROVIDER_NOT_SUPPORTED", "Engine does not support a configured model provider"
        )
    integrations = await load_integrations(db, app_state.secret_box)
    if set(caps.integrations) - integrations.keys():
        raise APIError(
            422, "INTEGRATION_MISSING", "A required engine integration is not configured"
        )
    integrations = {
        name: value
        for name, value in integrations.items()
        if name in {*caps.integrations, *caps.optional_integrations}
    }
    pricing = {
        profile.role: {
            "input": str(profile.input_usd_per_mtok)
            if profile.input_usd_per_mtok is not None
            else None,
            "output": str(profile.output_usd_per_mtok)
            if profile.output_usd_per_mtok is not None
            else None,
        }
        for profile in await db.scalars(select(ModelProfile).where(ModelProfile.scope == "global"))
    }
    if any(value is None for prices in pricing.values() for value in prices.values()):
        run.cost_unknown = True
    remaining = Decimal(run.budget_usd) - Decimal(run.cost_usd or 0)
    row = StageRun(
        research_run_id=run.id,
        iteration=run.iteration,
        stage_key=stage.stage_key,
        engine_key=stage.engine,
        prior_stage_run_id=prior_stage_run_id,
        origin_decision_id=origin_decision_id,
        status="PENDING",
        input_refs=[ref.model_dump(mode="json", by_alias=True) for ref in inputs.artifacts],
        config_snapshot={
            "models": {
                role: {
                    "provider": getattr(models, role).provider,
                    "model": getattr(models, role).model,
                }
                for role in ("stage", "review")
            },
            "pricing": pricing,
            "inputs": inputs.model_dump(mode="json", by_alias=True),
        },
    )
    db.add(row)
    run.current_stage_key = stage.stage_key
    await db.flush()
    append_run_event(
        db, run, row, "stage.status_changed", {"status": "PENDING", "stage_run_id": str(row.id)}
    )
    if remaining <= 0 and not run.cost_unknown:
        run.status, row.phase = "budget_exceeded", "paused"
        await db.commit()
        return row
    callback_base = (settings.engine_callback_base_url or settings.public_base_url).rstrip("/")
    req = RunCreateRequest(
        contract_version=CONTRACT_VERSION,
        platform_run_id=str(run.id),
        stage_run_id=str(row.id),
        stage_key=row.stage_key,
        iteration=run.iteration,
        interaction_mode=run.interaction_mode,
        models=models,
        integrations=integrations,
        inputs=inputs,
        review_policy={"max_auto_revisions": settings.max_auto_revisions},
        budget_usd=remaining if remaining > 0 else run.budget_usd,
        callback_url=callback_base
        + settings.api_prefix
        + paths.PLATFORM_ENGINE_RUN_BASE.format(engine_key=stage.engine, stage_run_id=row.id),
    )
    # The engine can call back during create. Its stage must exist in a committed transaction.
    await db.commit()
    try:
        response = await app_state.engines.get(stage.engine).create_run(req)
    except EngineUnavailable:
        row.engine_unreachable_since = datetime.now(UTC)
        await db.flush()
        return row
    # Callbacks can still be committing when create returns. Lock the rows as callbacks do and
    # re-read them, so the seq and status below come from committed state.
    for model, key in ((ResearchRun, run.id), (StageRun, row.id)):
        await db.scalar(
            select(model)
            .where(model.id == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    row.engine_run_id = response.engine_run_id
    if row.status == "PENDING":
        row.status = "RUNNING"
        row.phase = response.phase
        append_run_event(db, run, row, "stage.status_changed", {"status": "RUNNING"})
    await sync_run_status(db, run, stage=row)
    await db.flush()
    return row


async def apply_engine_event(db, stage_run, event):
    run = await db.get(ResearchRun, stage_run.research_run_id)
    old_status = stage_run.status
    if event.step_key is not None:
        stage_run.step_key = event.step_key
    if event.type == "run.phase_changed":
        stage_run.phase = event.phase
        if stage_run.status == "PENDING":
            stage_run.status = "RUNNING"
    elif event.type == "run.gate_opened":
        gate = Gate.model_validate(event.payload)
        stage_run.open_gate = gate.model_dump(mode="json", by_alias=True)
        stage_run.waiting_input = True
        stored = await db.scalar(
            select(RunGate).where(
                RunGate.stage_run_id == stage_run.id, RunGate.gate_key == gate.gate_id
            )
        )
        if stored is None:
            db.add(
                RunGate(
                    run_id=run.id,
                    stage_run_id=stage_run.id,
                    gate_key=gate.gate_id,
                    kind=gate.kind,
                    spec=stage_run.open_gate,
                    opened_seq=run.last_seq,
                )
            )
    elif event.type == "run.gate_closed":
        stored = await db.scalar(
            select(RunGate).where(
                RunGate.stage_run_id == stage_run.id, RunGate.gate_key == event.payload["gate_id"]
            )
        )
        if stored is not None:
            stored.answer = event.payload
            stored.answered_at = datetime.now(UTC)
            stored.resolved_seq = run.last_seq
        stage_run.open_gate = None
        stage_run.waiting_input = False
    elif event.type == "run.cost":
        cost = RunCost.model_validate(event.payload)
        if cost.cost_usd is not None:
            stage_run.cost_usd = cost.cost_usd
        else:
            prices = stage_run.config_snapshot.get("pricing", {})
            total = Decimal("0")
            known = True
            for role, usage in cost.usage.items():
                price = prices.get(role, {})
                if price.get("input") is None or price.get("output") is None:
                    known = False
                    break
                total += (
                    Decimal(price["input"]) * usage.input_tokens
                    + Decimal(price["output"]) * usage.output_tokens
                ) / 1_000_000
            if known:
                stage_run.cost_usd = total
            else:
                run.cost_unknown = True
        await db.flush()
        run.cost_usd = await db.scalar(
            select(func.coalesce(func.sum(StageRun.cost_usd), 0)).where(
                StageRun.research_run_id == run.id
            )
        )
    elif event.type == "run.published":
        artifact = await db.get(RunArtifact, UUID(event.payload["artifact_id"]))
        if artifact is None or artifact.stage_run_id != stage_run.id or not artifact.is_primary:
            raise APIError(
                409, "ARTIFACT_NOT_READY", "Published primary artifact has not been delivered"
            )
        transition(stage_run, "publish")
        stage_run.phase = event.phase
        stage_run.open_gate, stage_run.waiting_input = None, False
        stage_run.config_snapshot = stage_run.config_snapshot | {
            "published_artifact_id": str(artifact.id)
        }
    elif event.type == "run.failed":
        transition(stage_run, "fail")
        stage_run.phase, stage_run.failure = event.phase, event.payload
    elif event.type == "run.cancelled":
        transition(stage_run, "cancel")
        stage_run.phase = event.phase
    await sync_run_status(db, run, stage=stage_run)
    if old_status != stage_run.status:
        append_run_event(db, run, stage_run, "stage.status_changed", {"status": stage_run.status})
    if run.interaction_mode != "auto" and (
        event.type == "run.gate_opened"
        or (old_status != stage_run.status and stage_run.status in {"COMPLETED", "FAILED"})
    ):
        append_run_event(
            db,
            run,
            stage_run,
            "attention.required",
            {
                "stage_run_id": str(stage_run.id),
                "gate_id": (stage_run.open_gate or {}).get("gate_id"),
            },
        )
        await notify_project_members(
            db,
            run.project_id,
            "run_awaiting_review",
            run_id=run.id,
            roles=(ProjectRole.MANAGER, ProjectRole.RESEARCHER),
        )
    await db.flush()


async def accept_stage(db, app_state, stage_run, *, selection, note, actor, user_id):
    if stage_run.status != "COMPLETED":
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage is not completed")
    run = await db.get(ResearchRun, stage_run.research_run_id)
    pipeline = app_state.settings.pipelines[run.pipeline_key]
    index = next(i for i, config in enumerate(pipeline) if config.stage_key == stage_run.stage_key)
    next_config = pipeline[index + 1] if index + 1 < len(pipeline) else None
    primary_id = stage_run.config_snapshot.get("published_artifact_id")
    artifact = await db.get(RunArtifact, UUID(primary_id)) if primary_id else None
    if artifact is None or artifact.stage_run_id != stage_run.id:
        raise APIError(409, "ARTIFACT_NOT_READY", "Published primary artifact is missing")
    if next_config:
        caps = await _capabilities(app_state, next_config.engine)
        if parse_schema_id(artifact.schema_id) not in [
            parse_schema_id(schema) for schema in caps.input_schemas
        ]:
            raise APIError(
                409, "SCHEMA_MISMATCH", "Next engine does not accept the primary artifact schema"
            )
    transition(stage_run, "accept")
    decision = StageDecision(
        stage_run_id=stage_run.id,
        action="accept",
        actor=actor,
        decided_by_user_id=user_id,
        payload={"selection": selection, "note": note},
    )
    db.add(decision)
    append_run_event(
        db,
        run,
        stage_run,
        "decision.recorded",
        {"action": "accept", "selection": selection, **({"note": note} if note else {})},
        actor=actor,
    )
    append_run_event(db, run, stage_run, "stage.status_changed", {"status": "ACCEPTED"})
    if next_config is None:
        await sync_run_status(db, run, stage=stage_run)
        await db.flush()
        return None
    base = (
        app_state.settings.engine_callback_base_url or app_state.settings.public_base_url
    ).rstrip("/")
    ref = ArtifactRef(
        artifact_id=str(artifact.id),
        schema=artifact.schema_id,
        version=artifact.version,
        sha256=artifact.sha256,
        primary=True,
        download_url=f"{base}{app_state.settings.api_prefix}/internal/engines/{next_config.engine}/artifacts/{artifact.id}",
    )
    return await start_stage(
        db,
        app_state,
        run,
        next_config,
        inputs=Inputs(
            artifacts=[ref],
            decisions=Decisions(
                selection=selection,
                note=note,
                actor=actor,
                decided_by=str(user_id) if user_id else None,
            ),
        ),
    )


async def validate_selection(db, app_state, stage_run, selection):
    """X4: a hypothesis stage is accepted with at least one id found in its primary artifact."""
    if stage_run.stage_key != "hypothesis":
        return
    ids = selection.get(SELECTION_HYPOTHESIS_IDS) or []
    if not ids:
        raise APIError(422, "INVALID_SELECTION", "Select at least one hypothesis")
    primary_id = stage_run.config_snapshot.get("published_artifact_id")
    artifact = await db.get(RunArtifact, UUID(primary_id)) if primary_id else None
    if artifact is None or artifact.stage_run_id != stage_run.id:
        raise APIError(409, "ARTIFACT_NOT_READY", "Published primary artifact is missing")
    store = app_state.file_store
    try:
        body = b"".join([chunk async for chunk in store.open(artifact.storage_key)])
        known = {item["id"] for item in json.loads(body)["hypotheses"]}
    except (OSError, ValueError, KeyError, TypeError):
        raise APIError(
            409, "ARTIFACT_NOT_READY", "Published primary artifact cannot be read"
        ) from None
    if not set(ids) <= known:
        raise APIError(422, "INVALID_SELECTION", "Selection contains an unknown hypothesis id")
