"""Build the ConsultationRequest: a situation summary and a catalogue of artifacts, not content."""

from sqlalchemy import select

from management.research.contracts import (
    CONTRACT_VERSION,
    PIPELINE_RECOMMENDATION,
    REVIEW_FINDINGS,
    SELECTION_HYPOTHESIS_IDS,
    AllowedAction,
    BudgetView,
    Charter,
    ConsultArtifact,
    ConsultationRequest,
    CriticReport,
    FailureInfo,
    NotebookEntry,
    Obligation,
    PipelineStageView,
    Situation,
    paths,
)
from management.research.models import PiConsultation, PiNote, RunArtifact, StageRun
from management.research.pi.obligations import load, targeted_at

NOTEBOOK_LIMIT = 40
#: The PI may send a stage back to itself this many times per run; more is not offered.
MAX_PI_SAME_STAGE_SEND_BACKS = 1


def callback_base(app_state) -> str:
    settings = app_state.settings
    return (settings.engine_callback_base_url or settings.public_base_url).rstrip("/")


def result_url(app_state, consultation_id) -> str:
    return (
        callback_base(app_state)
        + app_state.settings.api_prefix
        + paths.PLATFORM_PI_RESULT.format(consultation_id=consultation_id)
    )


def artifact_url(app_state, artifact_id) -> str:
    return (
        callback_base(app_state)
        + app_state.settings.api_prefix
        + paths.PLATFORM_PI_ARTIFACT.format(artifact_id=artifact_id)
    )


def allowed_actions(
    app_state, run, kind, stage_run, *, same_stage_send_backs=0
) -> list[AllowedAction]:
    if kind == "charter":
        return [AllowedAction(action="charter")]
    if kind == "stage_review":
        return []
    if kind == "failure":
        return [AllowedAction(action="rerun"), AllowedAction(action="stop")]
    if kind == "close":
        return [AllowedAction(action="report")]
    config = next(
        c
        for c in app_state.settings.pipelines[run.pipeline_key]
        if c.stage_key == stage_run.stage_key
    )
    actions = [
        AllowedAction(
            action="accept",
            selection_key=SELECTION_HYPOTHESIS_IDS if stage_run.stage_key == "hypothesis" else None,
        )
    ]
    if run.iteration < app_state.settings.max_iterations:
        targets = list(config.loop_back_to)
        if same_stage_send_backs < MAX_PI_SAME_STAGE_SEND_BACKS:
            targets.append(stage_run.stage_key)
        if targets:
            actions.append(AllowedAction(action="send_back", targets=targets))
    actions.append(AllowedAction(action="stop"))
    return actions


async def pipeline_view(db, app_state, run) -> list[PipelineStageView]:
    rows = await db.scalars(
        select(StageRun)
        .where(StageRun.research_run_id == run.id, StageRun.iteration == run.iteration)
        .order_by(StageRun.created_at)
    )
    latest = {row.stage_key: row.status for row in rows}
    return [
        PipelineStageView(
            stage_key=config.stage_key,
            engine=config.engine,
            status=latest.get(config.stage_key),
            checkpoint=config.checkpoint,
            loop_back_to=config.loop_back_to,
        )
        for config in app_state.settings.pipelines[run.pipeline_key]
    ]


def _role(artifact, stage, current_id) -> str:
    if stage.id == current_id:
        if artifact.is_primary:
            return "primary"
        return {REVIEW_FINDINGS: "review", PIPELINE_RECOMMENDATION: "recommendation"}.get(
            artifact.schema_id, "other"
        )
    if stage.status == "SUPERSEDED":
        return "history"
    if artifact.schema_id == REVIEW_FINDINGS:
        return "review"
    return "input" if stage.status == "ACCEPTED" else "other"


async def artifact_catalogue(db, app_state, run, stage_run) -> list[ConsultArtifact]:
    """Every artifact the PI may need. The PI reads what it chooses."""
    current_id = stage_run.id if stage_run else None
    rows = await db.execute(
        select(RunArtifact, StageRun)
        .join(StageRun, StageRun.id == RunArtifact.stage_run_id)
        .where(RunArtifact.run_id == run.id)
        .order_by(StageRun.created_at, RunArtifact.created_at)
    )
    catalogue = []
    for artifact, stage in rows:
        if stage.id != current_id and not (
            artifact.is_primary or artifact.schema_id == REVIEW_FINDINGS
        ):
            continue
        catalogue.append(
            ConsultArtifact(
                artifact_id=str(artifact.id),
                schema=artifact.schema_id,
                version=artifact.version,
                sha256=artifact.sha256,
                download_url=artifact_url(app_state, artifact.id),
                primary=artifact.is_primary,
                role=_role(artifact, stage, current_id),
                stage_key=stage.stage_key,
                stage_run_id=str(stage.id),
                size_bytes=artifact.size_bytes,
            )
        )
    return catalogue


async def same_stage_send_backs(db, run, stage_run) -> int:
    """Times the PI already sent this stage back to itself in this run."""
    results = await db.scalars(
        select(PiConsultation.result)
        .join(StageRun, StageRun.id == PiConsultation.stage_run_id)
        .where(
            PiConsultation.research_run_id == run.id,
            PiConsultation.kind == "stage_outcome",
            PiConsultation.status.in_(("applied", "endorsed")),
            PiConsultation.result.is_not(None),
            StageRun.stage_key == stage_run.stage_key,
        )
    )
    return sum(
        1
        for r in results
        if (d := r.get("decision") or {}).get("action") == "send_back"
        and d.get("target_stage_key") == stage_run.stage_key
    )


async def notebook_entries(db, run) -> list[NotebookEntry]:
    recent = await db.scalars(
        select(PiNote)
        .where(PiNote.research_run_id == run.id)
        .order_by(PiNote.created_at.desc())
        .limit(NOTEBOOK_LIMIT)
    )
    notes = {note.id: note for note in recent}
    # A user directive is never dropped, however old.
    directives = await db.scalars(
        select(PiNote).where(
            PiNote.research_run_id == run.id, PiNote.kind == "directive", PiNote.actor == "user"
        )
    )
    notes.update({note.id: note for note in directives})
    return [
        NotebookEntry(
            kind=note.kind,
            text=note.text,
            actor=note.actor,
            stage_key=note.stage_key,
            at=note.created_at,
            entry_id=str(note.id),
        )
        for note in sorted(notes.values(), key=lambda note: note.created_at)
    ]


async def critique_for(db, stage_run) -> CriticReport | None:
    """The Critic report of the stage run, when the Critic answered."""
    if stage_run is None:
        return None
    result = await db.scalar(
        select(PiConsultation.result)
        .where(
            PiConsultation.stage_run_id == stage_run.id,
            PiConsultation.kind == "stage_review",
            PiConsultation.status == "applied",
            PiConsultation.result.is_not(None),
        )
        .order_by(PiConsultation.created_at.desc())
        .limit(1)
    )
    report = (result or {}).get("report")
    return CriticReport.model_validate(report) if report else None


def run_obligations(run, kind, stage_run) -> list[Obligation]:
    """`stage_review` sees what its stage must control; the outcome and the close see all."""
    if kind == "stage_review" and stage_run is not None:
        return targeted_at(run, stage_run.stage_key)
    return load(run) if kind in {"stage_outcome", "close"} else []


async def build_request(
    db, app_state, run, kind, *, consultation_id, authority, models, stage_run=None
) -> ConsultationRequest:
    """`models` is the `PIModels` pair for this kind. A `stage_review` only advises."""
    settings = app_state.settings
    failure = None
    if kind == "failure" and stage_run is not None:
        info = stage_run.failure or {}
        failure = FailureInfo(
            code=str(info.get("code") or "unknown"), message=str(info.get("message") or "")
        )
    situation = Situation(
        stage_key=stage_run.stage_key if stage_run else None,
        stage_run_id=str(stage_run.id) if stage_run else None,
        iteration=stage_run.iteration if stage_run else run.iteration,
        max_iterations=settings.max_iterations,
        pipeline=await pipeline_view(db, app_state, run),
        budget=BudgetView(
            limit_usd=run.budget_usd, spent_usd=run.cost_usd or 0, cost_unknown=run.cost_unknown
        ),
        failure=failure,
    )
    return ConsultationRequest(
        contract_version=CONTRACT_VERSION,
        consultation_id=str(consultation_id),
        platform_run_id=str(run.id),
        kind=kind,
        interaction_mode=run.interaction_mode or "copilot",
        authority="decide" if authority == "pi" and kind != "stage_review" else "advise",
        topic=run.topic or "",
        charter=Charter.model_validate(run.charter) if run.charter and kind != "charter" else None,
        situation=situation,
        allowed=allowed_actions(
            app_state,
            run,
            kind,
            stage_run,
            same_stage_send_backs=await same_stage_send_backs(db, run, stage_run)
            if kind == "stage_outcome" and stage_run
            else 0,
        ),
        artifacts=await artifact_catalogue(db, app_state, run, stage_run),
        notebook=await notebook_entries(db, run),
        obligations=run_obligations(run, kind, stage_run),
        critique=await critique_for(db, stage_run) if kind == "stage_outcome" else None,
        models=models,
        callback_url=result_url(app_state, consultation_id),
    )
