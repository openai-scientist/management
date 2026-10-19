import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from sqlalchemy import select

from management.access.service import ensure_writable_project, lock_project_scope
from management.audit.service import record_audit
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
    GateAnswer,
    Inputs,
    PipelineFeedback,
    ResearchProfile,
    RunCost,
    RunCreateRequest,
    parse_schema_id,
    paths,
)
from management.research.engines.client import (
    EngineContractMismatch,
    EngineRejected,
    EngineUnavailable,
)
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
from management.research.pi.authority import authority_for, engine_mode, review_gate_applies
from management.research.pi.guidance import build_guidance, with_guidance
from management.research.pi.obligations import carry_forward
from management.research.pi.service import (
    emit_attention,
    pi_active,
    request_consultation,
    stage_config,
)
from management.research.reviews import validate_gate_action
from management.research.stage_runs import append_run_event, recompute_run_cost, sync_run_status

Actor = Literal["user", "pi", "system"]

DECISION_ACTION = {"answer": "answer_gate", "approve": "approve", "comment": "comment"}

ALLOWED = {
    ("RUNNING", "publish"): "COMPLETED",
    ("RUNNING", "fail"): "FAILED",
    ("PENDING", "fail"): "FAILED",
    ("RUNNING", "cancel"): "STOPPED",
    ("PENDING", "cancel"): "STOPPED",
    ("COMPLETED", "accept"): "ACCEPTED",
    ("COMPLETED", "supersede"): "SUPERSEDED",
    ("ACCEPTED", "supersede"): "SUPERSEDED",
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
        charter_status="pending" if settings.pi_service else "none",
    )
    db.add(run)
    await db.flush()
    if run.charter_status == "pending":
        # The first stage starts when the charter is settled.
        await request_consultation(db, app_state, run, "charter")
    else:
        await start_stage(db, app_state, run, pipeline[0], inputs=first_stage_inputs(run))
    await refresh_project_status(db, project)
    return run


def first_stage_inputs(run) -> Inputs:
    # The engine requires a topic of 12..2000 characters after strip.
    question = ((run.charter or {}).get("question") or "").strip()
    topic = question[:2000] if len(question) >= 12 else run.topic
    return Inputs(params={"topic": topic, "domains": run.domains})


def _engine_profile(run, caps) -> ResearchProfile:
    """The first run domain the engine supports exactly; the base profile when none is."""
    for domain in run.domains or []:
        profile = ResearchProfile(domain=domain.strip().lower())
        if caps.exact_support(profile):
            return profile
    return ResearchProfile()


async def start_stage(
    db, app_state, run, stage, *, inputs: Inputs, prior_stage_run_id=None, origin_decision_id=None
):
    settings = app_state.settings
    caps = await _capabilities(app_state, stage.engine)
    models = await load_models(db, app_state.secret_box)
    pi = pi_active(app_state, run)
    if pi:
        inputs = with_guidance(inputs, await build_guidance(db, run, stage.stage_key))
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
        if profile.role in {"stage", "review"}  # the engine does not use the Critic model
    }
    if any(value is None for prices in pricing.values() for value in prices.values()):
        run.cost_unknown = True
    enforced = settings.run_budget_enforced
    remaining = Decimal(run.budget_usd) - Decimal(run.cost_usd or 0)
    # Not enforced: engines get a large nominal cap, so they never pause on their own.
    engine_budget = (
        (remaining if remaining > 0 else run.budget_usd)
        if enforced
        else settings.run_max_budget_usd * 100
    )
    profile = _engine_profile(run, caps)
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
            "profile": profile.model_dump(mode="json"),
        },
    )
    db.add(row)
    run.current_stage_key = stage.stage_key
    await db.flush()
    append_run_event(
        db, run, row, "stage.status_changed", {"status": "PENDING", "stage_run_id": str(row.id)}
    )
    if enforced and remaining <= 0 and not run.cost_unknown:
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
        interaction_mode=engine_mode(run) if pi else run.interaction_mode,
        models=models,
        integrations=integrations,
        inputs=inputs,
        profile=profile,
        review_policy={"max_auto_revisions": settings.max_auto_revisions},
        budget_usd=engine_budget,
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


async def _attention(db, run, stage_run) -> None:
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


async def apply_engine_event(db, stage_run, event, app_state=None):
    run = await db.get(ResearchRun, stage_run.research_run_id)
    old_status = stage_run.status
    opened_gate = None
    if event.step_key is not None:
        stage_run.step_key = event.step_key
    if event.type == "run.phase_changed":
        stage_run.phase = event.phase
        if stage_run.status == "PENDING":
            stage_run.status = "RUNNING"
    elif event.type == "run.gate_opened":
        gate = Gate.model_validate(event.payload)
        opened_gate = gate
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
        await recompute_run_cost(db, run)
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
    changed = old_status != stage_run.status
    if app_state is not None and pi_active(app_state, run):
        if opened_gate is not None:
            # The review-gate rule answers a review gate after this transaction commits; the
            # researcher owns every other gate, in every mode.
            if not review_gate_applies(run, opened_gate.kind):
                await _attention(db, run, stage_run)
        elif changed and stage_run.status == "COMPLETED":
            await request_consultation(db, app_state, run, "stage_review", stage_run=stage_run)
            config = stage_config(app_state, run, stage_run.stage_key)
            if authority_for(run, "stage_outcome", config) == "researcher":
                # The decision is the researcher's: say so now, the Critic report follows.
                await emit_attention(
                    db,
                    run,
                    stage_run,
                    {
                        "kind": "stage_outcome",
                        "reason": "stage_completed",
                        "text": "The stage is complete. The Critic report will follow.",
                    },
                )
        elif changed and stage_run.status == "FAILED":
            await request_consultation(db, app_state, run, "failure", stage_run=stage_run)
    elif run.interaction_mode != "auto" and (
        event.type == "run.gate_opened" or (changed and stage_run.status in {"COMPLETED", "FAILED"})
    ):
        await _attention(db, run, stage_run)
    await db.flush()


def wire_actor(actor: Actor, user_id, decided_by: str | None = None):
    """The engine knows `user` and `system`. The PI acts as `system` and names itself."""
    if actor == "user":
        return "user", decided_by or (str(user_id) if user_id else None)
    return "system", decided_by or ("pi" if actor == "pi" else None)


def artifact_ref(app_state, engine_key, artifact, *, primary=True):
    base = (
        app_state.settings.engine_callback_base_url or app_state.settings.public_base_url
    ).rstrip("/")
    return ArtifactRef(
        artifact_id=str(artifact.id),
        schema=artifact.schema_id,
        version=artifact.version,
        sha256=artifact.sha256,
        primary=primary,
        download_url=f"{base}{app_state.settings.api_prefix}/internal/engines/{engine_key}/artifacts/{artifact.id}",
    )


async def accept_stage(
    db,
    app_state,
    stage_run,
    *,
    selection,
    note,
    actor,
    user_id,
    reason=None,
    decided_by=None,
    consultation_id=None,
):
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
    extra_refs = await _extra_input_refs(db, app_state, stage_run, artifact, next_config)
    transition(stage_run, "accept")
    if pi_active(app_state, run):
        await carry_forward(db, app_state, run, stage_run, selection, next_config)
    decision = StageDecision(
        stage_run_id=stage_run.id,
        action="accept",
        actor=actor,
        decided_by_user_id=user_id,
        reason=reason,
        payload={"selection": selection, "note": note} | _consultation_payload(consultation_id),
    )
    db.add(decision)
    _audit_decision(db, run, stage_run, decision)
    append_run_event(
        db,
        run,
        stage_run,
        "decision.recorded",
        {"action": "accept", "selection": selection}
        | ({"note": note} if note else {})
        | ({"reason": reason} if reason else {})
        | _consultation_payload(consultation_id),
        actor=actor,
    )
    append_run_event(db, run, stage_run, "stage.status_changed", {"status": "ACCEPTED"})
    if next_config is None:
        await sync_run_status(db, run, stage=stage_run)
        await db.flush()
        if pi_active(app_state, run):
            await request_consultation(db, app_state, run, "close", stage_run=stage_run)
        return None
    ref = artifact_ref(app_state, next_config.engine, artifact)
    wire, by = wire_actor(actor, user_id, decided_by)
    return await start_stage(
        db,
        app_state,
        run,
        next_config,
        inputs=Inputs(
            artifacts=[ref, *extra_refs],
            decisions=Decisions(selection=selection, note=note, actor=wire, decided_by=by),
        ),
    )


async def _extra_input_refs(db, app_state, stage_run, primary, next_config) -> list[ArtifactRef]:
    """The latest version of each other artifact of the stage that the next engine accepts."""
    if next_config is None:
        return []
    caps = await _capabilities(app_state, next_config.engine)
    accepted = {parse_schema_id(schema) for schema in caps.input_schemas}
    rows = await db.scalars(
        select(RunArtifact)
        .where(RunArtifact.stage_run_id == stage_run.id, RunArtifact.id != primary.id)
        .order_by(RunArtifact.schema_id, RunArtifact.version)
    )
    latest = {}
    for artifact in rows:
        if (
            artifact.schema_id != primary.schema_id
            and parse_schema_id(artifact.schema_id) in accepted
        ):
            latest[artifact.schema_id] = artifact
    return [
        artifact_ref(app_state, next_config.engine, artifact, primary=False)
        for artifact in latest.values()
    ]


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


async def _relock(db, model, key):
    """Re-read a row under lock after an engine call, so callbacks that committed are visible."""
    return await db.scalar(
        select(model)
        .where(model.id == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _consultation_payload(consultation_id) -> dict:
    return {"consultation_id": str(consultation_id)} if consultation_id else {}


def _audit_decision(db, run, stage_run, decision) -> None:
    """Every decision on a stage, by a person, the AI PI or the system, for the activity feed."""
    record_audit(
        db,
        actor_user_id=decision.decided_by_user_id,
        action="run.decision_recorded",
        resource_type="run",
        resource_id=run.id,
        project_id=run.project_id,
        details={
            "decision": decision.action,
            "stage_key": stage_run.stage_key,
            "decided_by": decision.actor,
        },
    )


def _decision_event(db, run, stage_run, decision, extra=None, *, actor):
    _audit_decision(db, run, stage_run, decision)
    payload = (
        {"action": decision.action}
        | (extra or {})
        | _consultation_payload(decision.payload.get("consultation_id"))
    )
    if decision.reason:
        payload["reason"] = decision.reason
    append_run_event(db, run, stage_run, "decision.recorded", payload, actor=actor)


async def answer_stage_gate(
    db,
    app_state,
    stage_run,
    answer: GateAnswer,
    *,
    actor: Actor,
    user_id,
    reason=None,
    decided_by=None,
    gate_id=None,
    consultation_id=None,
    event_extra=None,
):
    """Record a gate answer and send it to the engine. Researcher and PI share this path.

    `answer.actor` and `answer.decided_by` are replaced: the engine hears `user`, or `system`
    with `decided_by` for the PI. `gate_id` defaults to the stage's open gate.
    """
    gate_id = gate_id or (stage_run.open_gate or {}).get("gate_id")
    if gate_id is None:
        raise APIError(409, "GATE_NOT_OPEN", "Gate is not open")
    wire, by = wire_actor(actor, user_id, decided_by)
    payload = answer.model_dump(mode="json", exclude={"actor", "decided_by"})
    answer = answer.model_copy(update={"actor": wire, "decided_by": by})
    decision = await db.scalar(
        select(StageDecision).where(
            StageDecision.stage_run_id == stage_run.id, StageDecision.gate_id == gate_id
        )
    )
    if decision is not None:
        # The same answer again is a retry; a different one cannot reopen the gate.
        if {k: v for k, v in decision.payload.items() if k != "consultation_id"} != payload:
            raise APIError(409, "GATE_NOT_OPEN", "Gate was already answered")
        if decision.actor == "user":
            # A retry reaches the engine as the original decision maker: the same answer.
            answer = answer.model_copy(update={"decided_by": str(decision.decided_by_user_id)})
        if decision.status == "applied":
            return decision
    else:
        if not stage_run.open_gate or stage_run.open_gate.get("gate_id") != gate_id:
            raise APIError(409, "GATE_NOT_OPEN", "Gate is not open")
        validate_gate_action(stage_run.open_gate, answer)
        decision = StageDecision(
            stage_run_id=stage_run.id,
            gate_id=gate_id,
            action=DECISION_ACTION[answer.action],
            payload=payload | _consultation_payload(consultation_id),
            actor=actor,
            reason=reason,
            status="pending",
            decided_by_user_id=user_id,
        )
        db.add(decision)
        await db.flush()
    if not stage_run.engine_run_id:
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage has not started on the engine")
    decision_id, run_id, stage_id = decision.id, stage_run.research_run_id, stage_run.id
    engine_key, engine_run_id = stage_run.engine_key, stage_run.engine_run_id
    # The engine calls back while it answers; commit first so the callback sees the stage.
    await db.commit()
    try:
        await app_state.engines.get(engine_key).answer_gate(engine_run_id, gate_id, answer)
    except EngineUnavailable:
        raise APIError(503, "ENGINE_UNAVAILABLE", "Engine could not be reached") from None
    except EngineRejected as exc:
        await db.delete(await db.get(StageDecision, decision_id))
        await db.commit()
        message = "Engine rejected the answer"
        raise APIError(
            409 if exc.status_code == 409 else 422,
            exc.code,
            f"{message}: {exc.detail}" if exc.detail else message,
        ) from None
    run = await _relock(db, ResearchRun, run_id)
    stage_run = await _relock(db, StageRun, stage_id)
    decision = await db.get(StageDecision, decision_id, populate_existing=True)
    decision.status = "applied"
    # The choice itself, so a client replaying the run can show what was decided.
    choice = {
        key: decision.payload[key]
        for key in ("option_id", "text", "params", "findings")
        if decision.payload.get(key)
    }
    _decision_event(
        db,
        run,
        stage_run,
        decision,
        {"gate_id": gate_id} | choice | (event_extra or {}),
        actor=actor,
    )
    return decision


def _pipeline_config(app_state, run, stage_key):
    pipeline = app_state.settings.pipelines[run.pipeline_key]
    index = next(i for i, config in enumerate(pipeline) if config.stage_key == stage_key)
    return pipeline[index], index, pipeline


async def _stage_runs(db, run):
    return list(
        await db.scalars(
            select(StageRun)
            .where(StageRun.research_run_id == run.id)
            .order_by(StageRun.iteration, StageRun.created_at)
        )
    )


async def send_back(
    db,
    app_state,
    stage_run,
    *,
    target_stage_key,
    note,
    artifact_ids,
    actor: Actor,
    user_id,
    reason=None,
    decided_by=None,
    consultation_id=None,
):
    """Send a COMPLETED stage back to `target_stage_key`: a new iteration (B6).

    The target is a stage in `loop_back_to`, or the stage itself (refine).
    Returns the decision; the new stage run has it as `origin_decision_id`.
    """
    if stage_run.status != "COMPLETED":
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage is not completed")
    run = await db.get(ResearchRun, stage_run.research_run_id)
    config, _, pipeline = _pipeline_config(app_state, run, stage_run.stage_key)
    if target_stage_key not in {*config.loop_back_to, stage_run.stage_key}:
        raise APIError(422, "LOOP_BACK_NOT_DECLARED", "Stage cannot loop back to that stage")
    if run.iteration >= app_state.settings.max_iterations:
        raise APIError(409, "MAX_ITERATIONS_REACHED", "The run reached its maximum iterations")
    target, target_index, _ = _pipeline_config(app_state, run, target_stage_key)
    caps = await _capabilities(app_state, target.engine)
    rows = await _stage_runs(db, run)
    same_target = [row for row in rows if row.stage_key == target_stage_key]
    last = same_target[-1]
    from_target = {config.stage_key for config in pipeline[target_index:]}
    superseded = [
        row
        for row in rows
        if row.iteration == run.iteration
        and row.stage_key in from_target
        and row.status in {"COMPLETED", "ACCEPTED"}
    ]
    decision = StageDecision(
        stage_run_id=stage_run.id,
        action="send_back",
        actor=actor,
        decided_by_user_id=user_id,
        reason=reason,
        payload={
            "target_stage_key": target_stage_key,
            "note": note,
            "artifact_ids": artifact_ids,
        }
        | ({"decided_by": decided_by} if decided_by else {})
        | _consultation_payload(consultation_id),
    )
    db.add(decision)
    for row in superseded:
        transition(row, "supersede")
        row.open_gate, row.waiting_input = None, False
        append_run_event(db, run, row, "stage.status_changed", {"status": "SUPERSEDED"})
    await db.flush()
    history = []
    for row in same_target:
        artifact_id = row.config_snapshot.get("published_artifact_id")
        artifact = await db.get(RunArtifact, UUID(artifact_id)) if artifact_id else None
        if row.status == "SUPERSEDED" and artifact is not None:
            history.append(artifact_ref(app_state, target.engine, artifact))
    inputs = Inputs.model_validate(last.config_snapshot["inputs"]).model_copy(
        update={
            "feedback": [
                PipelineFeedback(
                    source_stage_key=stage_run.stage_key, note=note, artifact_ids=artifact_ids
                )
            ],
            "prior_engine_run_id": last.engine_run_id if caps.reuses_prior_run else None,
            "history": history,
        }
    )
    run.iteration += 1
    _decision_event(
        db, run, stage_run, decision, {"target_stage_key": target_stage_key}, actor=actor
    )
    append_run_event(
        db,
        run,
        stage_run,
        "iteration.started",
        {
            "iteration": run.iteration,
            "target_stage_key": target_stage_key,
            "from_stage_run_id": str(stage_run.id),
        },
        actor=actor,
    )
    await start_stage(
        db,
        app_state,
        run,
        target,
        inputs=inputs,
        prior_stage_run_id=last.id,
        origin_decision_id=decision.id,
    )
    return decision


async def rerun_stage(
    db,
    app_state,
    stage_run,
    *,
    actor: Actor,
    user_id,
    reason=None,
    decided_by=None,
    consultation_id=None,
):
    """Start a FAILED stage again in the same iteration with the same inputs."""
    if stage_run.status != "FAILED":
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage has not failed")
    run = await db.get(ResearchRun, stage_run.research_run_id)
    if stage_run.iteration != run.iteration or await db.scalar(
        select(StageRun.id).where(StageRun.prior_stage_run_id == stage_run.id)
    ):
        raise APIError(409, "STAGE_STATE_CONFLICT", "Stage was already started again")
    if await db.scalar(
        select(ResearchRun.id).where(
            ResearchRun.project_id == run.project_id,
            ResearchRun.id != run.id,
            active_run_filter(),
        )
    ):
        raise APIError(409, "PROJECT_HAS_ACTIVE_RUN", "Project already has an active run")
    config, _, _ = _pipeline_config(app_state, run, stage_run.stage_key)
    caps = await _capabilities(app_state, config.engine)
    inputs = Inputs.model_validate(stage_run.config_snapshot["inputs"])
    if caps.reuses_prior_run and stage_run.engine_run_id:
        inputs = inputs.model_copy(update={"prior_engine_run_id": stage_run.engine_run_id})
    decision = StageDecision(
        stage_run_id=stage_run.id,
        action="rerun",
        actor=actor,
        decided_by_user_id=user_id,
        reason=reason,
        payload=({"decided_by": decided_by} if decided_by else {})
        | _consultation_payload(consultation_id),
    )
    db.add(decision)
    await db.flush()
    _decision_event(db, run, stage_run, decision, actor=actor)
    run.status, run.finished_at, run.failure_message = "running", None, None
    await start_stage(
        db,
        app_state,
        run,
        config,
        inputs=inputs,
        prior_stage_run_id=stage_run.id,
        origin_decision_id=decision.id,
    )
    return decision


async def stop_research_run(
    db,
    app_state,
    run,
    *,
    actor: Actor,
    user_id,
    reason=None,
    decided_by=None,
    consultation_id=None,
):
    """Cancel the open stage on its engine; the stage becomes STOPPED and the run `stopped`."""
    stage = await db.scalar(
        select(StageRun)
        .where(
            StageRun.research_run_id == run.id,
            StageRun.status.in_(("PENDING", "RUNNING", "COMPLETED")),
        )
        .order_by(StageRun.iteration.desc(), StageRun.created_at.desc())
        .limit(1)
    )
    if stage is None:
        raise APIError(409, "RUN_FINISHED", "Run has no stage to stop")
    run_id, stage_id, engine_run_id = run.id, stage.id, stage.engine_run_id
    engine_key, running = stage.engine_key, stage.status in {"PENDING", "RUNNING"}
    # The engine calls back while it cancels; commit first to release the row locks.
    await db.commit()
    if running and engine_run_id:
        try:
            await app_state.engines.get(engine_key).cancel(engine_run_id)
        except EngineUnavailable:
            raise APIError(503, "ENGINE_UNAVAILABLE", "Engine could not be reached") from None
    run = await _relock(db, ResearchRun, run_id)
    stage = await _relock(db, StageRun, stage_id)
    # A callback may already have stopped the stage; do not stop it twice.
    if stage.status in {"PENDING", "RUNNING"}:
        transition(stage, "cancel")
    elif stage.status == "COMPLETED":
        stage.status, stage.finished_at = "STOPPED", datetime.now(UTC)
    if stage.status == "STOPPED" and run.status != "stopped":
        append_run_event(db, run, stage, "stage.status_changed", {"status": "STOPPED"})
        await sync_run_status(db, run, stage=stage)
    decision = StageDecision(
        stage_run_id=stage_id,
        action="stop",
        actor=actor,
        decided_by_user_id=user_id,
        reason=reason,
        payload=({"decided_by": decided_by} if decided_by else {})
        | _consultation_payload(consultation_id),
    )
    db.add(decision)
    _decision_event(db, run, stage, decision, actor=actor)
    await db.flush()
    return decision
