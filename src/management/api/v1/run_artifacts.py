from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import require_project_access
from management.api.v1.internal_popper import ArtifactKind
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import Principal, require_active_principal
from management.db.session import get_db
from management.infra.file_store import FileStore, attachment_headers, get_file_store
from management.research.models import RunArtifact
from management.services.runs import get_run

router = APIRouter(prefix="/projects/{project_id}/runs/{run_id}/artifacts", tags=["runs"])

NOT_FOUND = {404: {"model": ErrorResponse, "description": "The run or file was not found"}}


class RunArtifactItem(BaseModel):
    id: str
    run_id: str
    kind: ArtifactKind
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    created_at: datetime
    stage_run_id: str | None = None
    schema_id: str | None = None
    version: int | None = None
    primary: bool = False
    review_round: int | None = None


@router.get(
    "",
    response_model=ApiResponse[list[RunArtifactItem]],
    summary="List the result files of a run",
    responses=NOT_FOUND,
)
async def list_run_artifacts(
    project_id: UUID,
    run_id: UUID,
    stage_run_id: UUID | None = None,
    schema_id: str | None = Query(default=None, alias="schema"),
    version: int | None = Query(default=None, ge=1),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[RunArtifactItem]]:
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    query = select(RunArtifact).where(RunArtifact.run_id == run.id)
    if stage_run_id is not None:
        query = query.where(RunArtifact.stage_run_id == stage_run_id)
    if schema_id is not None:
        query = query.where(RunArtifact.schema_id == schema_id)
    if version is not None:
        query = query.where(RunArtifact.version == version)
    artifacts = (await db.scalars(query.order_by(RunArtifact.filename))).all()
    return ok(
        [
            RunArtifactItem(
                id=str(artifact.id),
                run_id=str(artifact.run_id),
                kind=artifact.kind,
                filename=artifact.filename,
                content_type=artifact.content_type,
                size_bytes=artifact.size_bytes,
                sha256=artifact.sha256,
                created_at=artifact.created_at,
                stage_run_id=str(artifact.stage_run_id) if artifact.stage_run_id else None,
                schema_id=artifact.schema_id,
                version=artifact.version,
                primary=artifact.is_primary,
                review_round=artifact.review_round,
            )
            for artifact in artifacts
        ]
    )


@router.get(
    "/{artifact_id}/download",
    summary="Download a result file",
    description="Always served as a download, never rendered in the browser.",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}, **NOT_FOUND},
)
async def download_run_artifact(
    project_id: UUID,
    run_id: UUID,
    artifact_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> StreamingResponse:
    await require_project_access(db, principal, project_id)
    run = await get_run(db, project_id, run_id)
    artifact = await db.scalar(
        select(RunArtifact).where(RunArtifact.id == artifact_id, RunArtifact.run_id == run.id)
    )
    if artifact is None or not await store.exists(artifact.storage_key):
        raise APIError(404, "NOT_FOUND", "Result file was not found")
    # End the transaction now so a slow download does not hold a database connection.
    await db.commit()
    return StreamingResponse(
        store.open(artifact.storage_key),
        media_type=artifact.content_type,
        headers={
            **attachment_headers(artifact.filename),
            "Content-Length": str(artifact.size_bytes),
        },
    )
