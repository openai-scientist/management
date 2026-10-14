from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from management.core.errors import APIError
from management.core.security import service_key_matches
from management.db.session import get_db
from management.infra.file_store import attachment_headers
from management.research.contracts import ConsultationResult
from management.research.models import PiConsultation, RunArtifact
from management.research.pi.apply import handle_result
from management.research.pi.service import flush_consultations


def require_pi_service(request: Request):
    settings = request.app.state.settings.pi_service
    if not service_key_matches(
        settings.service_key.get_secret_value() if settings else None,
        request.headers.get("X-Service-Key"),
    ):
        raise APIError(401, "SERVICE_KEY_INVALID", "A valid service key is required")


router = APIRouter(
    prefix="/internal/pi",
    tags=["pi callbacks"],
    dependencies=[Depends(require_pi_service)],
)


@router.post("/consultations/{consultation_id}/result")
async def result(
    consultation_id: UUID, body: ConsultationResult, request: Request, db=Depends(get_db)
):
    """The PI answers. Answering again with a stored result is harmless."""
    if body.consultation_id != str(consultation_id):
        raise APIError(422, "VALIDATION_ERROR", "consultation_id does not match the path")
    processed = await handle_result(db, request.app.state, consultation_id, body)
    consultation = await db.get(PiConsultation, consultation_id)
    await flush_consultations(db, request.app.state, consultation.research_run_id)
    return {"processed": processed}


@router.get("/artifacts/{artifact_id}")
async def download(artifact_id: UUID, request: Request, db=Depends(get_db)):
    artifact = await db.get(RunArtifact, artifact_id)
    store = request.app.state.file_store
    if artifact is None or not await store.exists(artifact.storage_key):
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
