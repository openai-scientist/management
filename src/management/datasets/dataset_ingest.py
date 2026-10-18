"""Turn a CSV file into a dataset version, whether it was uploaded or read from a connection."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import IO, Any, Literal
from uuid import UUID, uuid4

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.audit.service import record_audit
from management.core.errors import APIError
from management.core.sessions import Principal
from management.datasets.csv_inspection import CsvSummary, InvalidCsv, inspect_csv
from management.datasets.models import Dataset, DatasetVersion
from management.infra.file_store import FileStore, StoredFile, iter_file
from management.projects.project_status import refresh_project_status
from management.projects.settings_service import effective_section

logger = logging.getLogger("management.datasets")


@dataclass(frozen=True, slots=True)
class StoredCsv:
    """A file already in the store, waiting for the version record that will own it."""

    version_id: UUID
    storage_key: str
    filename: str
    summary: CsvSummary
    stored: StoredFile
    source_type: str = "upload"
    # The kind of file uploaded, as `data.types` names it; None for rows read from a connection.
    file_type: str | None = None
    source_details: dict[str, Any] | None = None
    # What the audit event says about the source, beyond its type.
    audit_details: dict[str, Any] = field(default_factory=dict)


def storage_key(project_id: UUID, dataset_id: UUID, version_id: UUID) -> str:
    return f"projects/{project_id}/datasets/{dataset_id}/{version_id}/original.csv"


async def name_taken(
    db: AsyncSession, project_id: UUID, name: str, *, except_id: UUID | None = None
) -> bool:
    query = select(Dataset.id).where(
        Dataset.project_id == project_id, func.lower(Dataset.name) == name.lower()
    )
    if except_id is not None:
        query = query.where(Dataset.id != except_id)
    return await db.scalar(query) is not None


def name_exists() -> APIError:
    return APIError(409, "DATASET_NAME_EXISTS", "The project already has a dataset by that name")


# What an upload under an existing name did, by the project's `data.same_name` setting.
UploadOutcome = Literal["created_dataset", "created_version", "created_copy"]
MAX_NAME_LENGTH = 160


def ensure_file_type_allowed(data: dict[str, Any], file_type: str | None) -> None:
    """The project's `data.types` decides which uploaded files it takes."""
    if file_type is not None and file_type not in data["types"]:
        raise APIError(
            422,
            "DATASET_FILE_TYPE_NOT_ALLOWED",
            f"This project doesn't accept {file_type} files. Upload "
            + " or ".join(data["types"])
            + ", or ask a Project Manager to change the data settings.",
        )


async def dataset_by_name(db: AsyncSession, project_id: UUID, name: str) -> Dataset | None:
    return await db.scalar(
        select(Dataset).where(
            Dataset.project_id == project_id, func.lower(Dataset.name) == name.lower()
        )
    )


async def _copy_name(db: AsyncSession, project_id: UUID, name: str) -> str:
    """`name (2)`, `name (3)`, ...: the first one free, the name cut to fit the length limit."""
    taken = set(
        (
            await db.scalars(
                select(func.lower(Dataset.name)).where(Dataset.project_id == project_id)
            )
        ).all()
    )
    number = 2
    while True:
        suffix = f" ({number})"
        candidate = name[: MAX_NAME_LENGTH - len(suffix)].rstrip() + suffix
        if candidate.lower() not in taken:
            return candidate
        number += 1


async def _locked_writable_project(
    db: AsyncSession, principal: Principal, project_id: UUID, csv: StoredCsv
):
    """Lock the project, check access again and the file type under the settings as they are now."""
    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    data = await effective_section(db, project_id, "data")
    ensure_file_type_allowed(data, csv.file_type)
    return project, data


async def store_csv(handle: IO[bytes], store: FileStore, key: str) -> tuple[CsvSummary, StoredFile]:
    """Check the CSV and store it under a key nothing uses yet. Nothing is stored when it raises."""
    try:
        summary = await run_in_threadpool(inspect_csv, handle)
    except InvalidCsv as exc:
        raise APIError(422, "INVALID_DATASET", str(exc)) from exc
    try:
        stored = await store.put(key, iter_file(handle))
    except BaseException:
        # Also when cancelled, which can arrive after the file is in place.
        await asyncio.shield(_discard(store, key))
        raise
    return summary, stored


async def _discard(store: FileStore, key: str) -> None:
    """Remove a file no record refers to. A failure here must not hide the error being raised."""
    try:
        await store.delete(key)
    except Exception:
        logger.exception("could not remove the unused file %s", key)


async def _commit(db: AsyncSession, store: FileStore, csv: StoredCsv) -> None:
    """Commit the version's record; when that fails, remove the file unless the record exists.

    A commit can raise after the database applied it: the connection drops before the answer,
    or the request is cancelled while waiting for it. Removing the file then would leave a
    version that can never be read, which is worse than a file nobody refers to.
    """
    try:
        await db.commit()
    except BaseException:
        await asyncio.shield(_discard_unless_recorded(db, store, csv))
        raise


async def _discard_unless_recorded(db: AsyncSession, store: FileStore, csv: StoredCsv) -> None:
    try:
        await db.rollback()
        recorded = await db.scalar(
            select(DatasetVersion.id).where(DatasetVersion.id == csv.version_id)
        )
    except Exception:
        logger.exception(
            "could not tell whether version %s was recorded; its file %s is kept",
            csv.version_id,
            csv.storage_key,
        )
        return
    if recorded is None:
        await _discard(store, csv.storage_key)


def _version(dataset_id: UUID, number: int, csv: StoredCsv, user_id: UUID) -> DatasetVersion:
    return DatasetVersion(
        id=csv.version_id,
        dataset_id=dataset_id,
        version_number=number,
        storage_key=csv.storage_key,
        original_filename=csv.filename,
        size_bytes=csv.stored.size_bytes,
        sha256=csv.stored.sha256,
        row_count=csv.summary.row_count,
        column_names=csv.summary.column_names,
        source_type=csv.source_type,
        source_details=csv.source_details,
        created_by_user_id=user_id,
    )


def _audit_source(csv: StoredCsv) -> dict[str, Any]:
    return {
        "sha256": csv.stored.sha256,
        "size_bytes": csv.stored.size_bytes,
        "source_type": csv.source_type,
        **csv.audit_details,
    }


async def create_dataset_with_version(
    db: AsyncSession,
    store: FileStore,
    *,
    project_id: UUID,
    principal: Principal,
    dataset_id: UUID,
    name: str,
    description: str | None,
    csv: StoredCsv,
    request_id: str | None,
) -> tuple[Dataset, DatasetVersion, UploadOutcome]:
    """Record the stored file under `name`, and commit.

    When the project already has a dataset by that name, `data.same_name` decides:
    `version` adds the file as that dataset's next version, `copy` makes a new dataset
    named `name (2)`, `name (3)`, ... The file was stored without any lock held, so access,
    the file type and the name are checked here, under the project lock.
    When this raises, for any reason, the file is removed unless its record was committed.
    """
    try:
        project, data = await _locked_writable_project(db, principal, project_id, csv)
        existing = await dataset_by_name(db, project_id, name)
        outcome: UploadOutcome = "created_dataset"
        if existing is not None and data["same_name"] == "version":
            version = await _add_version(
                db,
                project_id=project_id,
                principal=principal,
                dataset_id=existing.id,
                csv=csv,
                request_id=request_id,
            )
            dataset, outcome = existing, "created_version"
        else:
            if existing is not None:
                name, outcome = await _copy_name(db, project_id, name), "created_copy"
            if await db.get(Dataset, dataset_id) is not None:
                # The file was stored for a same-name version and the setting changed since.
                dataset_id = uuid4()
            dataset, version = await _add_dataset(
                db,
                project_id=project_id,
                principal=principal,
                dataset_id=dataset_id,
                name=name,
                description=description,
                csv=csv,
                request_id=request_id,
            )
        await refresh_project_status(db, project)
    except BaseException:
        # Not only Exception: a cancelled request must not leave the file behind either.
        await asyncio.shield(_discard(store, csv.storage_key))
        raise
    await _commit(db, store, csv)
    return dataset, version, outcome


async def _add_dataset(
    db: AsyncSession,
    *,
    project_id: UUID,
    principal: Principal,
    dataset_id: UUID,
    name: str,
    description: str | None,
    csv: StoredCsv,
    request_id: str | None,
) -> tuple[Dataset, DatasetVersion]:
    dataset = Dataset(
        id=dataset_id,
        project_id=project_id,
        name=name,
        description=description,
        created_by_user_id=principal.user.id,
    )
    db.add(dataset)
    await db.flush()
    version = _version(dataset_id, 1, csv, principal.user.id)
    db.add(version)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="dataset.created",
        resource_type="dataset",
        resource_id=dataset_id,
        project_id=project_id,
        request_id=request_id,
        details={"name": name, **_audit_source(csv)},
    )
    return dataset, version


async def append_version(
    db: AsyncSession,
    store: FileStore,
    *,
    project_id: UUID,
    principal: Principal,
    dataset_id: UUID,
    csv: StoredCsv,
    request_id: str | None,
) -> DatasetVersion:
    """Record the stored file as the dataset's next version, and commit.

    The number is given out under the project lock, so versions added together never share one.
    When this raises, for any reason, the file is removed unless its record was committed.
    """
    try:
        project, _ = await _locked_writable_project(db, principal, project_id, csv)
        version = await _add_version(
            db,
            project_id=project_id,
            principal=principal,
            dataset_id=dataset_id,
            csv=csv,
            request_id=request_id,
        )
        await refresh_project_status(db, project)
    except BaseException:
        # Not only Exception: a cancelled request must not leave the file behind either.
        await asyncio.shield(_discard(store, csv.storage_key))
        raise
    await _commit(db, store, csv)
    return version


async def _add_version(
    db: AsyncSession,
    *,
    project_id: UUID,
    principal: Principal,
    dataset_id: UUID,
    csv: StoredCsv,
    request_id: str | None,
) -> DatasetVersion:
    """The next version number is given out under the project lock the caller holds."""
    newest = await db.scalar(
        select(func.max(DatasetVersion.version_number)).where(
            DatasetVersion.dataset_id == dataset_id
        )
    )
    version = _version(dataset_id, int(newest or 0) + 1, csv, principal.user.id)
    db.add(version)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="dataset.version_added",
        resource_type="dataset_version",
        resource_id=csv.version_id,
        project_id=project_id,
        request_id=request_id,
        details={
            "dataset_id": str(dataset_id),
            "version_number": version.version_number,
            **_audit_source(csv),
        },
    )
    return version
