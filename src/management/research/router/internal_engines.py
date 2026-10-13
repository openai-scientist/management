from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import select

from management.core.errors import APIError
from management.core.security import service_key_matches
from management.db.session import get_db
from management.infra.file_store import attachment_headers, safe_filename
from management.research.contracts import ArtifactMeta, EventBatch
from management.research.models import ResearchRun, RunArtifact, StageRun
from management.research.orchestrator import apply_engine_event
from management.research.stage_runs import append_run_event


def require_engine_service(engine_key: str, request: Request):
    settings = request.app.state.settings.engines.get(engine_key)
    if not service_key_matches(
        settings.service_key.get_secret_value() if settings else None,
        request.headers.get("X-Service-Key"),
    ):
        raise APIError(401, "SERVICE_KEY_INVALID", "A valid service key is required")


router = APIRouter(
    prefix="/internal/engines",
    tags=["engine callbacks"],
    dependencies=[Depends(require_engine_service)],
)


async def locked_stage(db, engine_key, stage_run_id):
    stage = await db.scalar(
        select(StageRun).where(StageRun.id == stage_run_id, StageRun.engine_key == engine_key)
    )
    if stage is None:
        raise APIError(404, "NOT_FOUND", "Stage run was not found")
    run = await db.scalar(
        select(ResearchRun)
        .where(ResearchRun.id == stage.research_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    stage = await db.scalar(
        select(StageRun)
        .where(StageRun.id == stage_run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if stage.status in {"SUPERSEDED", "STOPPED", "FAILED"}:
        raise APIError(409, "STAGE_CLOSED", "Stage run is closed")
    return run, stage


@router.post("/{engine_key}/runs/{stage_run_id}/events")
async def events(engine_key: str, stage_run_id: UUID, body: EventBatch, db=Depends(get_db)):
    run, stage = await locked_stage(db, engine_key, stage_run_id)
    delivered = stage.last_source_seq
    accepted, skipped = [], 0
    for event in body.events:
        if event.source_seq <= delivered:
            skipped += 1
        elif event.source_seq != delivered + 1:
            return JSONResponse(
                status_code=409,
                content={
                    "code": "EVENT_GAP",
                    "message": "Event sequence has a gap",
                    "delivered_seq": stage.last_source_seq,
                },
            )
        else:
            accepted.append(event)
            delivered = event.source_seq
    for event in accepted:
        append_run_event(
            db,
            run,
            stage,
            event.type,
            event.payload,
            actor=event.actor,
            source_seq=event.source_seq,
        )
        await apply_engine_event(db, stage, event)
        stage.last_source_seq = event.source_seq
    await db.flush()
    return {"accepted": len(accepted), "skipped": skipped}


@router.post("/{engine_key}/runs/{stage_run_id}/artifacts", status_code=201)
async def artifacts(
    engine_key: str,
    stage_run_id: UUID,
    request: Request,
    meta: str = Form(...),
    file: UploadFile = File(...),
    db=Depends(get_db),
):
    try:
        descriptor = ArtifactMeta.model_validate_json(meta)
    except ValidationError:
        raise APIError(422, "VALIDATION_ERROR", "Invalid artifact metadata") from None
    run, stage = await locked_stage(db, engine_key, stage_run_id)
    existing = await db.scalar(
        select(RunArtifact).where(
            RunArtifact.stage_run_id == stage.id,
            RunArtifact.schema_id == descriptor.schema_id,
            RunArtifact.version == descriptor.version,
        )
    )
    if existing is None and (
        stage.phase == "published" or stage.status in {"COMPLETED", "ACCEPTED"}
    ):
        raise APIError(409, "STAGE_CLOSED", "Stage run is published")
    filename = safe_filename(descriptor.filename, "artifact")
    key = f"runs/{run.id}/stages/{stage.id}/{uuid4().hex}/{filename}"

    async def chunks():
        while chunk := await file.read(64 * 1024):
            yield chunk

    store = request.app.state.file_store
    try:
        stored = await store.put(key, chunks())
    finally:
        await file.close()
    if stored.sha256.lower() != descriptor.sha256.lower():
        await store.delete(key)
        raise APIError(422, "SHA256_MISMATCH", "Artifact hash does not match")
    if existing is not None:
        await store.delete(key)
        if existing.sha256 != stored.sha256:
            raise APIError(409, "ARTIFACT_CONFLICT", "Artifact version is immutable")
        return {"artifact_id": str(existing.id)}
    artifact = RunArtifact(
        run_id=run.id,
        stage_run_id=stage.id,
        schema_id=descriptor.schema_id,
        version=descriptor.version,
        is_primary=descriptor.primary,
        review_round=descriptor.review_round,
        artifact_metadata=descriptor.metadata,
        kind="other",
        filename=filename,
        content_type=descriptor.media_type,
        storage_key=key,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    db.add(artifact)
    await db.flush()
    return {"artifact_id": str(artifact.id)}


@router.get("/{engine_key}/artifacts/{artifact_id}")
async def download(engine_key: str, artifact_id: UUID, request: Request, db=Depends(get_db)):
    artifact = await db.get(RunArtifact, artifact_id)
    if (
        artifact is None
        or await db.scalar(
            select(StageRun.id)
            .where(StageRun.research_run_id == artifact.run_id, StageRun.engine_key == engine_key)
            .limit(1)
        )
        is None
    ):
        raise APIError(404, "NOT_FOUND", "Artifact was not found")
    store = request.app.state.file_store
    if not await store.exists(artifact.storage_key):
        raise APIError(404, "NOT_FOUND", "Artifact was not found")
    await db.commit()
    return StreamingResponse(
        store.open(artifact.storage_key),
        media_type=artifact.content_type,
        headers={
            **attachment_headers(artifact.filename),
            "Content-Length": str(artifact.size_bytes),
        },
    )
