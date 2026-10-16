import logging
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.audit.service import record_audit
from management.connections.connectors import ConnectorFactory, get_connector_factory
from management.connections.connectors.gate import ConnectionGate, get_connection_gate
from management.connections.service import (
    READ_ERRORS,
    saved_connection,
)
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.search import SearchTerm, matches
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.datasets.dataset_ingest import (
    append_version,
    create_dataset_with_version,
    name_exists,
    name_taken,
    storage_key,
)
from management.datasets.models import Dataset, DatasetVersion
from management.datasets.schemas import (
    DatasetItem,
    DatasetPatch,
    DatasetVersionItem,
    ImportDataset,
    ImportVersion,
    _clean_name,
)
from management.datasets.service import (
    _dataset_item,
    _get_dataset,
    _import_csv,
    _latest_versions,
    _receive_csv,
    _version_item,
)
from management.db.session import get_db
from management.infra.file_store import (
    FileStore,
    attachment_headers,
    get_file_store,
)
from management.infra.secret_box import SecretBox, get_secret_box
from management.projects.project_status import refresh_project_status
from management.research.models import ResearchRun

router = APIRouter(prefix="/projects/{project_id}/datasets", tags=["datasets"])
logger = logging.getLogger("management.datasets")


DATASET_ERRORS = {
    403: {"model": ErrorResponse, "description": "Project Manager or Researcher role is required"},
    404: {"model": ErrorResponse, "description": "The project or dataset was not found"},
    409: {"model": ErrorResponse, "description": "The project is archived or the name is taken"},
}


UPLOAD_ERRORS = {
    **DATASET_ERRORS,
    413: {"model": ErrorResponse, "description": "The file is larger than the upload limit"},
    415: {"model": ErrorResponse, "description": "Only .csv and .xlsx files are accepted"},
    422: {"model": ErrorResponse, "description": "The file is not a usable dataset"},
}


IMPORT_ERRORS = {
    **READ_ERRORS,
    404: {
        "model": ErrorResponse,
        "description": "The project, dataset or connection was not found",
    },
    413: {"model": ErrorResponse, "description": "The rows are larger than a dataset file may be"},
    422: {
        "model": ErrorResponse,
        "description": (
            "`INVALID_DATASET` when the rows cannot be a dataset (none at all, or columns "
            "without distinct names), `SOURCE_INVALID` when the table or query is the "
            "problem, `CONNECTION_FAILED` when the server could not be used; `error.reason` "
            "says which"
        ),
    },
}


@router.get(
    "",
    response_model=ApiResponse[list[DatasetItem]],
    summary="List the project's datasets",
    description="Each dataset comes with its newest version. Any project member can read.",
    responses={404: DATASET_ERRORS[404]},
)
async def list_datasets(
    project_id: UUID,
    q: SearchTerm = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[DatasetItem]]:
    await require_project_access(db, principal, project_id)
    filters = [Dataset.project_id == project_id]
    if q:
        filters.append(matches(q, Dataset.name))
    total = int(await db.scalar(select(func.count()).select_from(Dataset).where(*filters)) or 0)
    datasets = (
        await db.scalars(
            select(Dataset)
            .where(*filters)
            .order_by(Dataset.created_at.desc(), Dataset.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    latest = await _latest_versions(db, [dataset.id for dataset in datasets])
    return paginated(
        [_dataset_item(dataset, latest.get(dataset.id)) for dataset in datasets],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ApiResponse[DatasetItem],
    status_code=201,
    summary="Upload a new dataset",
    description=(
        "Send a multipart form with `name`, optional `description`, and a UTF-8 `.csv` file or an "
        "Excel `.xlsx` workbook, whose first sheet is stored as CSV. "
        "The file becomes version 1. Project Manager or Researcher only."
    ),
    responses=UPLOAD_ERRORS,
)
async def create_dataset(
    project_id: UUID,
    request: Request,
    name: str = Form(min_length=2, max_length=160),
    description: str | None = Form(default=None, max_length=5000),
    file: UploadFile = File(description="UTF-8 CSV or .xlsx workbook, with a header row"),
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> ApiResponse[DatasetItem]:
    try:
        name = _clean_name(name)
    except ValueError as exc:
        raise APIError(422, "VALIDATION_ERROR", str(exc)) from exc
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    if await name_taken(db, project_id, name):
        raise name_exists()

    # The file is stored before any lock is taken, so a slow upload never blocks the project.
    dataset_id, version_id = uuid4(), uuid4()
    csv = await _receive_csv(
        file,
        store,
        version_id,
        storage_key(project_id, dataset_id, version_id),
        request.app.state.settings.dataset_max_upload_bytes,
    )
    dataset, version = await create_dataset_with_version(
        db,
        store,
        project_id=project_id,
        principal=principal,
        dataset_id=dataset_id,
        name=name,
        description=description,
        csv=csv,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok(_dataset_item(dataset, version), "Dataset uploaded")


@router.post(
    "/from-connection",
    response_model=ApiResponse[DatasetItem],
    status_code=201,
    summary="Import a new dataset from a data connection",
    description=(
        "Reads one table, one SELECT statement or one time series through a saved connection "
        "and stores the "
        "rows as version 1, a CSV file checked the way an upload is. The request stays open "
        "until the rows are in, which can take minutes: when it times out on the way, list "
        "the datasets before trying again, since the import may still have finished. "
        "A result too large for a dataset file is refused (413), never cut. "
        "Project Manager or Researcher only."
    ),
    responses=IMPORT_ERRORS,
)
async def import_dataset(
    project_id: UUID,
    body: ImportDataset,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[DatasetItem]:
    saved = await saved_connection(db, principal, project_id, body.connection_id, box, gate)
    if await name_taken(db, project_id, body.name):
        raise name_exists()

    dataset_id = uuid4()
    csv = await _import_csv(
        request, db, store, gate, factory, project_id, dataset_id, principal, body, saved
    )
    dataset, version = await create_dataset_with_version(
        db,
        store,
        project_id=project_id,
        principal=principal,
        dataset_id=dataset_id,
        name=body.name,
        description=body.description,
        csv=csv,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok(_dataset_item(dataset, version), "Dataset imported")


@router.get(
    "/{dataset_id}",
    response_model=ApiResponse[DatasetItem],
    summary="Get a dataset",
    responses={404: DATASET_ERRORS[404]},
)
async def get_dataset(
    project_id: UUID,
    dataset_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[DatasetItem]:
    await require_project_access(db, principal, project_id)
    dataset = await _get_dataset(db, project_id, dataset_id)
    latest = await _latest_versions(db, [dataset.id])
    return ok(_dataset_item(dataset, latest.get(dataset.id)))


@router.patch(
    "/{dataset_id}",
    response_model=ApiResponse[DatasetItem],
    summary="Rename or describe a dataset",
    description="Changes the label only; uploaded files never change.",
    responses=DATASET_ERRORS,
)
async def update_dataset(
    project_id: UUID,
    dataset_id: UUID,
    body: DatasetPatch,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[DatasetItem]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    if not body.model_fields_set:
        raise APIError(422, "EMPTY_UPDATE", "Provide at least one dataset field to update")
    dataset = await _get_dataset(db, project_id, dataset_id)
    if "name" in body.model_fields_set and await name_taken(
        db, project_id, body.name, except_id=dataset.id
    ):
        raise name_exists()
    for field in body.model_fields_set:
        setattr(dataset, field, getattr(body, field))
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="dataset.updated",
        resource_type="dataset",
        resource_id=dataset.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"fields": sorted(body.model_fields_set)},
    )
    await db.flush()
    latest = await _latest_versions(db, [dataset.id])
    return ok(_dataset_item(dataset, latest.get(dataset.id)), "Dataset updated")


@router.delete(
    "/{dataset_id}",
    response_model=ApiResponse[None],
    summary="Delete a dataset and all its versions",
    description=(
        "Only a dataset no research run has used: a run must always be traceable to its input, "
        "so `DATASET_IN_USE` (409) names how many runs use it instead."
    ),
    responses=DATASET_ERRORS,
)
async def delete_dataset(
    project_id: UUID,
    dataset_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> ApiResponse[None]:
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    dataset = await _get_dataset(db, project_id, dataset_id)
    versions = (
        await db.scalars(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id))
    ).all()
    runs = int(
        await db.scalar(
            select(func.count())
            .select_from(ResearchRun)
            .where(ResearchRun.dataset_version_id.in_([version.id for version in versions]))
        )
        or 0
    )
    if runs:
        raise APIError(
            409,
            "DATASET_IN_USE",
            f"{dataset.name} is used by {runs} research run{'s' if runs != 1 else ''} "
            "and can't be deleted",
        )
    storage_keys = [version.storage_key for version in versions]
    for version in versions:
        await db.delete(version)
    await db.flush()
    await db.delete(dataset)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="dataset.deleted",
        resource_type="dataset",
        resource_id=dataset_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"name": dataset.name, "versions": len(versions)},
    )
    await refresh_project_status(db, project)
    # Commit first: a row pointing at a missing file is worse than a file nothing points at.
    await db.commit()
    for key in storage_keys:
        try:
            await store.delete(key)
        except Exception:
            logger.warning("stored file was not removed", exc_info=True, extra={"key": key})
    return ok(None, "Dataset deleted")


@router.get(
    "/{dataset_id}/versions",
    response_model=ApiResponse[list[DatasetVersionItem]],
    summary="List a dataset's versions, newest first",
    responses={404: DATASET_ERRORS[404]},
)
async def list_dataset_versions(
    project_id: UUID,
    dataset_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[DatasetVersionItem]]:
    await require_project_access(db, principal, project_id)
    dataset = await _get_dataset(db, project_id, dataset_id)
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(DatasetVersion)
            .where(DatasetVersion.dataset_id == dataset.id)
        )
        or 0
    )
    versions = (
        await db.scalars(
            select(DatasetVersion)
            .where(DatasetVersion.dataset_id == dataset.id)
            .order_by(DatasetVersion.version_number.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return paginated(
        [_version_item(version) for version in versions], total=total, limit=limit, offset=offset
    )


@router.post(
    "/{dataset_id}/versions",
    response_model=ApiResponse[DatasetVersionItem],
    status_code=201,
    summary="Upload a new version of a dataset",
    description=(
        "A `.csv` file or an `.xlsx` workbook, as for a new dataset. Earlier versions stay as "
        "they are. Project Manager or Researcher only."
    ),
    responses=UPLOAD_ERRORS,
)
async def add_dataset_version(
    project_id: UUID,
    dataset_id: UUID,
    request: Request,
    file: UploadFile = File(description="UTF-8 CSV or .xlsx workbook, with a header row"),
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> ApiResponse[DatasetVersionItem]:
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    await _get_dataset(db, project_id, dataset_id)

    version_id = uuid4()
    csv = await _receive_csv(
        file,
        store,
        version_id,
        storage_key(project_id, dataset_id, version_id),
        request.app.state.settings.dataset_max_upload_bytes,
    )
    version = await append_version(
        db,
        store,
        project_id=project_id,
        principal=principal,
        dataset_id=dataset_id,
        csv=csv,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok(_version_item(version), "Dataset version uploaded")


@router.post(
    "/{dataset_id}/versions/from-connection",
    response_model=ApiResponse[DatasetVersionItem],
    status_code=201,
    summary="Import a new version of a dataset from a data connection",
    description=(
        "Reads the source again and stores what it holds now as the next version; earlier "
        "versions stay as they are. Importing the same source twice gives two versions. "
        "The request stays open until the rows are in: when it times out on the way, list "
        "the versions before trying again. Project Manager or Researcher only."
    ),
    responses=IMPORT_ERRORS,
)
async def import_dataset_version(
    project_id: UUID,
    dataset_id: UUID,
    body: ImportVersion,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[DatasetVersionItem]:
    saved = await saved_connection(db, principal, project_id, body.connection_id, box, gate)
    await _get_dataset(db, project_id, dataset_id)

    csv = await _import_csv(
        request, db, store, gate, factory, project_id, dataset_id, principal, body, saved
    )
    version = await append_version(
        db,
        store,
        project_id=project_id,
        principal=principal,
        dataset_id=dataset_id,
        csv=csv,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok(_version_item(version), "Dataset version imported")


@router.get(
    "/{dataset_id}/versions/{version_id}/download",
    summary="Download the file of a dataset version",
    response_class=StreamingResponse,
    responses={
        200: {"content": {"text/csv": {}}, "description": "The version's CSV file"},
        404: DATASET_ERRORS[404],
    },
)
async def download_dataset_version(
    project_id: UUID,
    dataset_id: UUID,
    version_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> StreamingResponse:
    await require_project_access(db, principal, project_id)
    dataset = await _get_dataset(db, project_id, dataset_id)
    version = await db.scalar(
        select(DatasetVersion).where(
            DatasetVersion.id == version_id, DatasetVersion.dataset_id == dataset.id
        )
    )
    if version is None or not await store.exists(version.storage_key):
        raise APIError(404, "NOT_FOUND", "Dataset version was not found")
    # End the transaction now so a slow download does not hold a database connection.
    await db.commit()
    return StreamingResponse(
        store.open(version.storage_key),
        media_type="text/csv; charset=utf-8",
        headers={
            **attachment_headers(version.original_filename),
            "Content-Length": str(version.size_bytes),
        },
    )
