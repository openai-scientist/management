import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    require_project_access,
)
from management.audit.service import record_audit
from management.connections.connectors import (
    Connector,
    ConnectorError,
    ConnectorFactory,
)
from management.connections.connectors.base import (
    AnySource,
    QuerySource,
    TimeSeriesSource,
    ensure_source_supported,
)
from management.connections.connectors.gate import ConnectionGate
from management.connections.connectors.values import to_text
from management.connections.models import DataConnection, GoogleConnectionGrant
from management.connections.schemas.connections import PREVIEW_CELL_CHARS, ConnectionItem
from management.core.errors import APIError
from management.core.responses import ErrorResponse
from management.core.sessions import Principal
from management.infra.secret_box import SecretBox, SecretBoxError

logger = logging.getLogger("management.connectors")


CONNECTION_ERRORS = {
    403: {"model": ErrorResponse, "description": "Project Manager or Researcher role is required"},
    404: {"model": ErrorResponse, "description": "The project or connection was not found"},
    409: {"model": ErrorResponse, "description": "The project is archived or the name is taken"},
}


# For the endpoints that contact the external server.
PROBE_ERRORS = {
    **CONNECTION_ERRORS,
    429: {"model": ErrorResponse, "description": "Too many connection requests"},
    503: {"model": ErrorResponse, "description": "Data connections are not configured"},
}


# For the endpoints that read through a saved connection.
READ_ERRORS = {
    **PROBE_ERRORS,
    422: {
        "model": ErrorResponse,
        "description": (
            "`SOURCE_INVALID` when the table or query is the problem, `CONNECTION_FAILED` when "
            "the server could not be used; `error.reason` says which"
        ),
    },
}


def require_box(box: SecretBox | None) -> SecretBox:
    if box is None:
        raise APIError(
            503, "CONNECTIONS_NOT_CONFIGURED", "Data connections are not configured on this server"
        )
    return box


async def find_connection(
    db: AsyncSession, project_id: UUID, connection_id: UUID
) -> DataConnection:
    row = await db.scalar(
        select(DataConnection).where(
            DataConnection.id == connection_id, DataConnection.project_id == project_id
        )
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Connection was not found")
    return row


def open_secret(box: SecretBox, row: DataConnection) -> dict[str, Any]:
    try:
        return box.open(row.secret_ciphertext)
    except SecretBoxError:
        raise APIError(
            409,
            "CONNECTION_SECRET_UNREADABLE",
            "The stored credentials cannot be read. Delete this connection and create it again.",
        ) from None


@dataclass(frozen=True, slots=True)
class SavedConnection:
    name: str
    kind: str
    config: dict[str, Any]
    # Kept out of the repr, so it cannot reach a log or a traceback through this object.
    secret: dict[str, Any] = field(repr=False)


async def saved_connection(
    db: AsyncSession,
    principal: Principal,
    project_id: UUID,
    connection_id: UUID,
    box: SecretBox | None,
    gate: ConnectionGate,
) -> SavedConnection:
    """Check that the caller may read through a connection; return what a read needs of it.

    Everything a read needs from the Platform's database, so the caller can commit before it
    contacts the external server.
    """
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    box = require_box(box)
    gate.check_query_rate(principal.user.id)
    row = await find_connection(db, project_id, connection_id)
    return SavedConnection(row.name, row.kind, row.config, open_secret(box, row))


@asynccontextmanager
async def reading(
    request: Request,
    db: AsyncSession,
    gate: ConnectionGate,
    factory: ConnectorFactory,
    project_id: UUID,
    user_id: UUID,
    saved: SavedConnection,
    *,
    deadline: float | None = None,
) -> AsyncIterator[Connector]:
    """A connector to use within a slot and a deadline; its failures become 422 responses.

    The Platform's own transaction is committed once the slot is held: nothing of it stays
    open while the external server is contacted, and a request that finds no slot leaves
    nothing behind. The deadline is the one for a query unless another is given.
    """
    if deadline is None:
        deadline = request.app.state.settings.connection_query_timeout_seconds
    async with gate.slot(project_id, user_id):
        await db.commit()
        try:
            # The server-side timeout cannot be relied on: the server is whatever the user
            # pointed at, and may simply stop answering.
            async with asyncio.timeout(deadline):
                yield await factory(saved.kind, saved.config, saved.secret)
        except TimeoutError:
            logger.info("connection read abandoned after %s seconds", deadline)
            raise read_failed(ConnectorError("query_timeout")) from None
        except ConnectorError as exc:
            raise read_failed(exc) from None


def read_failed(error: ConnectorError) -> APIError:
    about_source = error.reason in (
        "source_not_found",
        "query_failed",
        "query_timeout",
        "scan_limit_exceeded",
        "unsupported_source",
        "source_malformed",
        "source_too_large",
        "too_many_points",
    )
    code = "SOURCE_INVALID" if about_source else "CONNECTION_FAILED"
    return APIError(422, code, error.message, reason=error.reason)


def require_supported_source(kind: str, source: AnySource) -> None:
    """Refuse a source this kind of connection cannot read: 422, and nothing is audited."""
    try:
        ensure_source_supported(kind, source)
    except ConnectorError as exc:
        raise read_failed(exc) from None
    if kind == "prometheus" and isinstance(source, TimeSeriesSource) and source.fields:
        # A mistake in the form, not something about the server.
        raise APIError(
            422,
            "VALIDATION_ERROR",
            "A Prometheus metric has one value, in the column `value`: leave `fields` empty",
        )
    if kind == "influxdb" and isinstance(source, TimeSeriesSource) and not source.fields:
        raise APIError(
            422,
            "VALIDATION_ERROR",
            "An InfluxDB measurement is read by its fields: name at least one in `fields`",
        )


def _utc_text(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def source_audit_details(source: AnySource) -> dict[str, Any]:
    """What an audit event records about a source that was read."""
    if isinstance(source, TimeSeriesSource):
        # The name of a metric is not sensitive the way the text of a query can be.
        return {
            "source_type": "timeseries",
            "name": source.name,
            "start": _utc_text(source.start),
            "end": _utc_text(source.end),
            "bucket": source.bucket,
            "aggregate": source.aggregate,
        }
    if isinstance(source, QuerySource):
        # Not the text: SQL can carry sensitive constants.
        return {
            "source_type": "query",
            "sql_sha256": hashlib.sha256(source.sql.encode()).hexdigest(),
        }
    return {"source_type": "table", "schema": source.schema_name, "name": source.name}


def _item(row: DataConnection) -> ConnectionItem:
    return ConnectionItem(
        id=str(row.id),
        project_id=str(row.project_id),
        name=row.name,
        kind=row.kind,
        config=row.config,
        last_tested_at=row.last_tested_at,
        last_error_code=row.last_error_code,
        created_by_user_id=str(row.created_by_user_id),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _name_taken(
    db: AsyncSession, project_id: UUID, name: str, *, except_id: UUID | None = None
) -> bool:
    query = select(DataConnection.id).where(
        DataConnection.project_id == project_id, func.lower(DataConnection.name) == name.lower()
    )
    if except_id is not None:
        query = query.where(DataConnection.id != except_id)
    return await db.scalar(query) is not None


def _preview_cell(value: Any) -> str | None:
    text = to_text(value)
    if text is not None and len(text) > PREVIEW_CELL_CHARS:
        return text[:PREVIEW_CELL_CHARS] + "…"
    return text


def _name_exists() -> APIError:
    return APIError(
        409, "CONNECTION_NAME_EXISTS", "The project already has a connection by that name"
    )


async def _probe(
    gate: ConnectionGate,
    factory: ConnectorFactory,
    project_id: UUID,
    user_id: UUID,
    kind: str,
    config: dict[str, Any],
    secret: dict[str, Any],
) -> tuple[Connector | None, ConnectorError | None]:
    """Connect for real: the connector that answered, or what went wrong.

    Holds a slot, never the Platform's database session: the caller commits first.
    """
    async with gate.slot(project_id, user_id):
        try:
            connector = await factory(kind, config, secret)
            await connector.test()
        except ConnectorError as exc:
            return None, exc
    return connector, None


def _audit_target(config: dict[str, Any]) -> dict[str, Any]:
    """Where a connection points, for an audit event: a server, a BigQuery project, a
    spreadsheet or a Drive folder. Never the Google account: its address is personal data."""
    if "host" in config:
        return {"host": config["host"], "port": config["port"]}
    if "spreadsheet_id" in config:
        return {"spreadsheet_id": config["spreadsheet_id"]}
    if "folder_id" in config:
        return {"folder_id": config["folder_id"]}
    return {"project_id": config["project_id"]}


def _grant_invalid() -> APIError:
    return APIError(
        422,
        "GOOGLE_GRANT_INVALID",
        "The Google access has expired or was already used. Connect the Google account again.",
    )


def _usable_grant(grant_id: UUID, principal: Principal, project_id: UUID) -> list[Any]:
    """What makes a grant usable here: it is this user's, for this project, holds Google's
    answer and has not expired."""
    return [
        GoogleConnectionGrant.id == grant_id,
        GoogleConnectionGrant.user_id == principal.user.id,
        GoogleConnectionGrant.project_id == project_id,
        GoogleConnectionGrant.secret_ciphertext.is_not(None),
        GoogleConnectionGrant.expires_at > datetime.now(UTC),
    ]


@dataclass(frozen=True, slots=True)
class _GoogleAccess:
    subject: str
    email: str
    # The refresh token. Kept out of the repr, like the secret of a saved connection.
    secret: dict[str, Any] = field(repr=False)


async def _google_access(
    request: Request,
    db: AsyncSession,
    box: SecretBox,
    principal: Principal,
    project_id: UUID,
    grant_id: UUID,
) -> _GoogleAccess:
    """Read what a grant holds without using it up: the connection has yet to be tried.

    Grants that expired go away here as well as when a new one is started, so a refresh
    token nobody used does not outlive its 10 minutes for long.
    """
    if request.app.state.google_drive_oauth is None:
        raise APIError(
            503, "CONNECTIONS_NOT_CONFIGURED", "Google connections are not configured here"
        )
    await db.execute(
        delete(GoogleConnectionGrant).where(GoogleConnectionGrant.expires_at <= datetime.now(UTC))
    )
    grant = await db.scalar(
        select(GoogleConnectionGrant).where(*_usable_grant(grant_id, principal, project_id))
    )
    try:
        if grant is None:
            raise _grant_invalid()
        return _GoogleAccess(
            grant.google_subject, grant.account_email, box.open(grant.secret_ciphertext)
        )
    except (APIError, SecretBoxError):
        # Committed first: the rollback that follows an error would bring the expired back.
        await db.commit()
        raise _grant_invalid() from None


async def _use_grant(
    db: AsyncSession, principal: Principal, project_id: UUID, grant_id: UUID
) -> None:
    """Spend a grant in the caller's transaction, or refuse: it is good for one connection.

    One statement, so of two requests with the same grant only one finds it.
    """
    used = await db.execute(
        delete(GoogleConnectionGrant).where(*_usable_grant(grant_id, principal, project_id))
    )
    if used.rowcount != 1:
        raise _grant_invalid()


def _audit_failed_test(
    db: AsyncSession,
    request: Request,
    principal: Principal,
    project_id: UUID,
    connection_id: UUID,
    kind: str,
    config: dict[str, Any],
    error: ConnectorError,
) -> None:
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.test_failed",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"kind": kind, **_audit_target(config), "reason": error.reason},
    )
