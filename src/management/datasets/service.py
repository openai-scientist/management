from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import IO, Any
from uuid import UUID, uuid4

from fastapi import Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.service import record_audit
from management.connections.connectors import ConnectorFactory
from management.connections.connectors.base import (
    ConnectorError,
    QuerySource,
    Source,
    TimeSeriesSource,
)
from management.connections.connectors.csv_export import (
    CellTooLarge,
    EmptyResult,
    ResultTooLarge,
    export_csv,
)
from management.connections.connectors.gate import ConnectionGate
from management.connections.connectors.google_drive import NOT_XLSX, XlsxBook, open_workbook
from management.connections.service import (
    SavedConnection,
    reading,
    require_supported_source,
    source_audit_details,
)
from management.core.errors import APIError
from management.core.sessions import Principal
from management.datasets.csv_inspection import DuplicateColumns, InvalidCsv, check_header
from management.datasets.dataset_ingest import (
    StoredCsv,
    ensure_file_type_allowed,
    storage_key,
    store_csv,
)
from management.datasets.models import Dataset, DatasetVersion
from management.datasets.schemas import DatasetItem, DatasetVersionItem, ImportVersion
from management.infra.file_store import (
    FileStore,
    safe_filename,
)
from management.projects.service import ProjectAccess, project_access

# Uploaded file extensions the Platform reads, by the name `data.types` gives them.
FILE_TYPES = {".csv": "CSV", ".xlsx": "Excel"}


async def require_dataset_reader(
    db: AsyncSession, principal: Principal, project_id: UUID
) -> ProjectAccess:
    """Any member, except a Reviewer while `access.reviewers_see_data` is off (403)."""
    rights = await project_access(db, principal, project_id)
    if not rights.can_read_raw_data:
        raise APIError(
            403,
            "DATASET_ACCESS_DENIED",
            "Reviewers can't see this project's data. Ask a Project Manager for access.",
        )
    return rights


def _version_item(version: DatasetVersion) -> DatasetVersionItem:
    return DatasetVersionItem(
        id=str(version.id),
        dataset_id=str(version.dataset_id),
        version_number=version.version_number,
        original_filename=version.original_filename,
        size_bytes=version.size_bytes,
        sha256=version.sha256,
        row_count=version.row_count,
        column_names=version.column_names,
        source_type=version.source_type,
        source=version.source_details,
        created_by_user_id=str(version.created_by_user_id),
        created_at=version.created_at,
    )


def _dataset_item(dataset: Dataset, latest: DatasetVersion | None) -> DatasetItem:
    return DatasetItem(
        id=str(dataset.id),
        project_id=str(dataset.project_id),
        name=dataset.name,
        description=dataset.description,
        created_by_user_id=str(dataset.created_by_user_id),
        created_at=dataset.created_at,
        updated_at=dataset.updated_at,
        latest_version=_version_item(latest) if latest else None,
    )


async def _get_dataset(db: AsyncSession, project_id: UUID, dataset_id: UUID) -> Dataset:
    dataset = await db.scalar(
        select(Dataset).where(Dataset.id == dataset_id, Dataset.project_id == project_id)
    )
    if dataset is None:
        raise APIError(404, "NOT_FOUND", "Dataset was not found")
    return dataset


async def _latest_versions(db: AsyncSession, dataset_ids: list[UUID]) -> dict[UUID, DatasetVersion]:
    if not dataset_ids:
        return {}
    newest = (
        select(DatasetVersion.dataset_id, func.max(DatasetVersion.version_number).label("number"))
        .where(DatasetVersion.dataset_id.in_(dataset_ids))
        .group_by(DatasetVersion.dataset_id)
        .subquery()
    )
    rows = await db.scalars(
        select(DatasetVersion).join(
            newest,
            (DatasetVersion.dataset_id == newest.c.dataset_id)
            & (DatasetVersion.version_number == newest.c.number),
        )
    )
    return {version.dataset_id: version for version in rows}


async def _workbook_csv(file: IO[bytes], max_bytes: int) -> IO[bytes]:
    """The first sheet of an uploaded Excel workbook, as a temporary CSV file.

    Its first row names the columns, the way a tab read through a connection is a table.
    """
    try:
        workbook = await run_in_threadpool(open_workbook, file, max_bytes)
        try:
            book = XlsxBook(workbook, run_in_threadpool)
            sheets = await book.tables()
            if not sheets:
                raise ConnectorError("source_malformed", NOT_XLSX)
            stream = await book.read(sheets[0], max_rows=None)
            check_header([column.name for column in stream.columns])
            return await export_csv(stream, max_bytes=max_bytes)
        finally:
            await run_in_threadpool(workbook.close)
    except ResultTooLarge:
        raise APIError(
            413, "DATASET_TOO_LARGE", f"The first sheet takes more than {max_bytes} bytes as CSV"
        ) from None
    except CellTooLarge as exc:
        raise APIError(422, "INVALID_DATASET", str(exc)) from None
    except EmptyResult:
        raise APIError(
            422, "INVALID_DATASET", "The first sheet needs a header row and at least one data row"
        ) from None
    except InvalidCsv as exc:
        raise APIError(422, "INVALID_DATASET", str(exc)) from exc
    except ConnectorError as exc:
        if exc.reason == "source_too_large":
            raise APIError(413, "DATASET_TOO_LARGE", exc.message) from None
        raise APIError(422, "INVALID_DATASET", exc.message) from None


async def _receive_csv(
    upload: UploadFile,
    store: FileStore,
    version_id: UUID,
    key: str,
    max_bytes: int,
    data: dict[str, Any],
) -> StoredCsv:
    """Validate the uploaded CSV or Excel file and store it as CSV.

    The extension picks the parser and the parser must read the file, so a renamed file
    fails as INVALID_DATASET. Nothing is stored when the file is rejected.
    """
    filename = safe_filename(upload.filename, "dataset.csv")
    extension = PurePosixPath(filename).suffix.lower()
    if extension not in FILE_TYPES:
        raise APIError(415, "UNSUPPORTED_FILE_TYPE", "Only .csv and .xlsx files are accepted")
    # Checked before reading so a refused file is never stored; again under the lock.
    ensure_file_type_allowed(data, FILE_TYPES[extension])
    if extension == ".csv":
        summary, stored = await store_csv(upload.file, store, key)
    elif extension == ".xlsx":
        with await _workbook_csv(upload.file, max_bytes) as handle:
            summary, stored = await store_csv(handle, store, key)
        # What is stored and downloaded is the CSV, so the name says so.
        filename = filename[: -len(extension)] + ".csv"
    return StoredCsv(version_id, key, filename, summary, stored, file_type=FILE_TYPES[extension])


def _import_filename(source: Source) -> str:
    if isinstance(source, QuerySource):
        return "query.csv"
    # A table or a metric, by its name. Room is left for the extension: the name can be
    # longer than a file name may be.
    return safe_filename(source.name, "table")[:196] + ".csv"


async def _fetch_csv(
    request: Request,
    db: AsyncSession,
    gate: ConnectionGate,
    factory: ConnectorFactory,
    project_id: UUID,
    principal: Principal,
    connection_id: UUID,
    saved: SavedConnection,
    source: Source,
) -> IO[bytes]:
    """Read a source through a saved connection into a temporary CSV file.

    Holds a slot and no database session while the rows arrive. Whatever the reason it stops
    early, nothing has been stored yet.
    """
    settings = request.app.state.settings
    require_supported_source(saved.kind, source)
    # A form has no SELECT statement to narrow it with.
    series = isinstance(source, TimeSeriesSource)
    # Committed before the query runs, so one that fails or never returns is on record too.
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.import_started",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details=source_audit_details(source),
    )
    handle: IO[bytes] | None = None
    try:
        async with (
            reading(
                request,
                db,
                gate,
                factory,
                project_id,
                principal.user.id,
                saved,
                deadline=settings.connection_import_timeout_seconds,
            ) as connector,
            connector.open_rows(source, max_rows=None) as stream,
        ):
            # Before the first row is read: a bad header is not worth fetching a table for.
            check_header([column.name for column in stream.columns])
            handle = await export_csv(stream, max_bytes=settings.dataset_max_upload_bytes)
    except DuplicateColumns:
        raise APIError(
            422,
            "INVALID_DATASET",
            "Two columns of the result share a name. Give each one its own name with AS "
            "in a SELECT statement.",
        ) from None
    except InvalidCsv as exc:
        raise APIError(422, "INVALID_DATASET", str(exc)) from exc
    except EmptyResult:
        raise APIError(
            422, "INVALID_DATASET", "The source has no rows, and a dataset needs at least one"
        ) from None
    except ResultTooLarge:
        raise APIError(
            413,
            "DATASET_TOO_LARGE",
            f"The rows take more than {settings.dataset_max_upload_bytes} bytes as CSV. "
            + (
                "Choose a larger bucket, a shorter span or fewer tags."
                if series
                else "Import fewer rows or columns with a SELECT statement."
            ),
        ) from None
    except CellTooLarge as exc:
        raise APIError(
            422,
            "SOURCE_INVALID",
            f"A value in column {exc.column!r} is longer than {exc.limit} characters. "
            + (
                "Leave the column out."
                if series
                else "Leave the column out or shorten it in a SELECT statement."
            ),
            reason="cell_too_large",
        ) from None
    except BaseException:
        # The rows were all read, and closing the connection then failed or ran out of time.
        if handle is not None:
            handle.close()
        raise
    return handle


def _import_source(
    connection_id: UUID, saved: SavedConnection, source: Source, fetched_at: datetime
) -> tuple[dict[str, Any], dict[str, Any]]:
    """What a version keeps about the connection it was read from, and what its audit says."""
    details = {
        "connection_id": str(connection_id),
        "connection_name": saved.name,
        "connection_kind": saved.kind,
        # As JSON: the times of a time series are stored as text.
        "source": source.model_dump(mode="json", by_alias=True),
        "fetched_at": fetched_at.isoformat().replace("+00:00", "Z"),
    }
    audit = {"connection_id": str(connection_id)}
    if isinstance(source, QuerySource):
        audit["sql_sha256"] = source_audit_details(source)["sql_sha256"]
    return details, audit


async def _import_csv(
    request: Request,
    db: AsyncSession,
    store: FileStore,
    gate: ConnectionGate,
    factory: ConnectorFactory,
    project_id: UUID,
    dataset_id: UUID,
    principal: Principal,
    body: ImportVersion,
    saved: SavedConnection,
) -> StoredCsv:
    """Read the source and store the result as a file no record refers to yet."""
    version_id = uuid4()
    key = storage_key(project_id, dataset_id, version_id)
    fetched_at = datetime.now(UTC)
    handle = await _fetch_csv(
        request, db, gate, factory, project_id, principal, body.connection_id, saved, body.source
    )
    with handle:
        summary, stored = await store_csv(handle, store, key)
    details, audit = _import_source(body.connection_id, saved, body.source, fetched_at)
    return StoredCsv(
        version_id,
        key,
        _import_filename(body.source),
        summary,
        stored,
        source_type="connection",
        source_details=details,
        audit_details=audit,
    )
