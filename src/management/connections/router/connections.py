import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_writable_project,
    lock_project_scope,
    require_project_access,
)
from management.audit.service import record_audit
from management.connections.connectors import (
    ConnectorFactory,
    get_connector_factory,
)
from management.connections.connectors.base import (
    ConnectorError,
    Identifier,
)
from management.connections.connectors.gate import ConnectionGate, get_connection_gate
from management.connections.connectors.google_drive import GoogleDriveConnector
from management.connections.connectors.google_sheets import (
    GoogleSheetsConnector,
)
from management.connections.connectors.timeseries import live_source
from management.connections.connectors.values import approximate_size, to_text
from management.connections.google_drive_oauth import GoogleAccessRevoked
from management.connections.models import DataConnection
from management.connections.schemas.connections import (
    LIVE_MAX_ROWS,
    ColumnItem,
    ConnectionItem,
    CreateConnection,
    CreateGoogleConnection,
    CreateGoogleDriveConnection,
    CreateGoogleSheetsConnection,
    GooglePickerCredentials,
    InfluxSecret,
    LiveData,
    LiveRequest,
    PreviewData,
    PreviewRequest,
    ReauthorizeConnection,
    RenameConnection,
    TableItem,
)
from management.connections.service import (
    CONNECTION_ERRORS,
    PROBE_ERRORS,
    READ_ERRORS,
    _audit_failed_test,
    _audit_target,
    _free_name,
    _google_access,
    _GoogleAccess,
    _item,
    _name_exists,
    _name_taken,
    _preview_cell,
    _probe,
    _use_grant,
    listed_for,
    manages,
    open_secret,
    own_account,
    read_failed,
    reading,
    require_box,
    require_owner_or_manager,
    require_supported_source,
    same_source,
    saved_connection,
    source_audit_details,
    visible_connection,
)
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.search import SearchTerm, matches
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.identity.google_oauth import GoogleOAuthError
from management.infra.secret_box import SecretBox, get_secret_box

router = APIRouter(prefix="/projects/{project_id}/connections", tags=["data connections"])


logger = logging.getLogger("management.connectors")


MAX_TABLES = 500


# A preview of very wide rows stops early, however few rows that leaves.
PREVIEW_SIZE = 2 * 1024 * 1024


@router.post(
    "/{connection_id}/picker",
    response_model=ApiResponse[GooglePickerCredentials],
    summary="Open Google Picker with the caller's saved Google account",
    description=(
        "For the caller's own `google` connection: a short-lived access token for the "
        "browser's Google Picker, so they can pick a spreadsheet or folder without signing in "
        "again. Then create a `google_sheets` or `google_drive` connection with this "
        "connection as `account_id`. Another member's account answers 404. A revoked access "
        "answers 422 `CONNECTION_FAILED`, reason `access_revoked`: connect the account again. "
        "Never persist or log this response."
    ),
    responses={
        **PROBE_ERRORS,
        422: {"model": ErrorResponse, "description": "Not a Google account, or access revoked"},
        502: {"model": ErrorResponse, "description": "Google could not issue an access token"},
    },
)
async def account_picker_credentials(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
) -> ApiResponse[GooglePickerCredentials]:
    response.headers["Cache-Control"] = "no-store"
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    box = require_box(box)
    settings = request.app.state.settings
    if not (settings.google_picker_api_key and settings.google_picker_app_id):
        raise APIError(
            503,
            "GOOGLE_PICKER_NOT_CONFIGURED",
            "Google Drive file selection is not configured here.",
        )
    if request.app.state.google_drive_oauth is None:
        raise APIError(
            503, "CONNECTIONS_NOT_CONFIGURED", "Google connections are not configured here"
        )
    gate.check_rate(principal.user.id)
    access = await _account_access(db, box, principal, project_id, connection_id)
    await db.commit()
    try:
        async with gate.slot(project_id, principal.user.id):
            token = await request.app.state.google_drive_oauth.access_token(
                access.secret["refresh_token"]
            )
    except GoogleAccessRevoked:
        raise read_failed(ConnectorError("access_revoked")) from None
    except GoogleOAuthError:
        raise APIError(
            502, "GOOGLE_PICKER_UNAVAILABLE", "Google Drive could not be reached. Please try again."
        ) from None
    return ok(
        GooglePickerCredentials(
            access_token=token,
            api_key=settings.google_picker_api_key.get_secret_value(),
            app_id=settings.google_picker_app_id,
        )
    )


@router.get(
    "",
    response_model=ApiResponse[list[ConnectionItem]],
    summary="List the project's data connections, newest first",
    description=(
        "Any project member can read. A member's `google` connection (their Google account) "
        "is listed only to them. Credentials are never returned."
    ),
    responses={404: CONNECTION_ERRORS[404]},
)
async def list_connections(
    project_id: UUID,
    q: SearchTerm = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[ConnectionItem]]:
    await require_project_access(db, principal, project_id)
    filters = [DataConnection.project_id == project_id, *listed_for(principal)]
    if q:
        filters.append(matches(q, DataConnection.name))
    total = int(
        await db.scalar(select(func.count()).select_from(DataConnection).where(*filters)) or 0
    )
    rows = (
        await db.scalars(
            select(DataConnection)
            .where(*filters)
            .order_by(DataConnection.created_at.desc(), DataConnection.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return paginated([_item(row) for row in rows], total=total, limit=limit, offset=offset)


@router.post(
    "",
    response_model=ApiResponse[ConnectionItem],
    status_code=201,
    summary=(
        "Connect the project to an external database, a Google spreadsheet, a Drive folder, "
        "a Prometheus server or an InfluxDB server"
    ),
    description=(
        "Connects once to check the details; nothing is saved when that fails (422 "
        "`CONNECTION_FAILED`, with `error.reason`). Project Manager or Researcher only. "
        "Use a read-only database user: every member who can contribute can run queries "
        "with it. For `google`, `grant_id` takes the place of `secret`: it keeps the caller's "
        "Google account, for them alone, to pick spreadsheets and folders with "
        "(`.../{id}/picker`). A member has one in a project: the same account again renews it "
        "and answers 200; another account answers 409 `GOOGLE_ACCOUNT_CONNECTED` until the "
        "first is deleted. A source already connected with the same address and user (or "
        "service account) is not added again: the existing connection answers 200, and one "
        "that stopped working takes the new credentials when its creator or a manager sends "
        "them. Keeping or renewing it also gives its new access to the "
        "spreadsheets and folders the caller picked with the same Google account. For "
        "`bigquery`, a service account with the roles "
        "BigQuery Job User and BigQuery Data Viewer; queries are billed to its project. For "
        "`google_sheets` and `google_drive`, `account_id` (the caller's own `google` "
        "connection; another member's answers 404) takes the place of `secret`: every member "
        "who can contribute then reads that one spreadsheet, or the files directly in that "
        "one folder, as that Google account. A grant that is not this user's, not for this "
        "project, expired or already used answers 422 `GOOGLE_GRANT_INVALID`; one whose "
        "connection failed can be tried again. For "
        "`prometheus` and `influxdb`, `secret` may be left out when the server asks for no "
        "credentials; an address inside a private network is refused (reason "
        "`host_not_allowed`). An `influxdb` server that does not list the `database` "
        "answers with reason `permission_denied`. For authenticated InfluxDB 1.8, give "
        "`secret.username` and `secret.password`; for InfluxDB 2.x and 3, give "
        "`secret.token`."
    ),
    responses={
        **PROBE_ERRORS,
        422: {"model": ErrorResponse, "description": "Invalid body or failed test"},
    },
)
async def create_connection(
    project_id: UUID,
    body: CreateConnection,
    request: Request,
    response: Response,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[ConnectionItem]:
    project, membership = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    box = require_box(box)
    gate.check_rate(principal.user.id)

    # A member keeps their Google account from a grant; files are then picked with that account.
    picked = isinstance(body, CreateGoogleSheetsConnection | CreateGoogleDriveConnection)
    by_google = picked or isinstance(body, CreateGoogleConnection)
    if by_google and request.app.state.google_drive_oauth is None:
        raise APIError(
            503, "CONNECTIONS_NOT_CONFIGURED", "Google connections are not configured here"
        )
    if picked:
        access = await _account_access(db, box, principal, project_id, body.account_id)
    elif isinstance(body, CreateGoogleConnection):
        access = await _google_access(request, db, box, principal, project_id, body.grant_id)
        await _check_one_account(db, principal, project_id, access)
    if by_google:
        if isinstance(body, CreateGoogleSheetsConnection):
            config: dict[str, Any] = {"spreadsheet_id": body.config.spreadsheet}
        elif isinstance(body, CreateGoogleDriveConnection):
            config = {"folder_id": body.config.folder}
        else:
            config = {}
        config |= {"account_email": access.email, "google_subject": access.subject}
        secret = access.secret
    else:
        config = body.config.model_dump()
        secret = body.secret.model_dump(exclude_defaults=isinstance(body.secret, InfluxSecret))
    # The same source, read as the same user, is connected once: the one there is answers.
    existing = (
        None if by_google else await same_source(db, box, project_id, body.kind, config, secret)
    )
    # One that stopped working takes the new credentials, from its creator or a manager.
    renew = (
        existing is not None
        and existing.last_error_code is not None
        and (existing.created_by_user_id == principal.user.id or manages(membership))
    )
    if existing is not None and not renew:
        response.status_code = 200
        return ok(_item(existing), "Already connected")
    if existing is None and body.name is not None and await _name_taken(db, project_id, body.name):
        raise _name_exists()
    connection_id = uuid4()
    request_id = getattr(request.state, "request_id", None)
    # Hand the session back before talking to a server the user chose: it may never answer.
    await db.commit()
    connector, error = await _probe(
        gate, factory, project_id, principal.user.id, body.kind, config, secret
    )
    if error is not None:
        _audit_failed_test(
            db, request, principal, project_id, connection_id, body.kind, config, error
        )
        # Committed first: the rollback that follows an error would erase the audit row.
        await db.commit()
        raise APIError(422, "CONNECTION_FAILED", error.message, reason=error.reason)

    await lock_project_scope(db, project_id)
    project, _ = await require_project_access(db, principal, project_id, contribute=True, lock=True)
    ensure_writable_project(project)
    if picked:
        # The account may have been removed, or another connected, while Google was being asked.
        renewed = await _account_access(db, box, principal, project_id, body.account_id)
        if renewed.subject != access.subject:
            raise APIError(409, "GOOGLE_ACCOUNT_CHANGED", "Your Google account changed. Try again.")
    if isinstance(body, CreateGoogleConnection):
        existing = await _check_one_account(db, principal, project_id, access)
        if existing is not None:
            # One account per member: the same one again renews it.
            await _use_grant(db, principal, project_id, body.grant_id)
            existing.secret_ciphertext = box.seal(secret)
            existing.config = config
            existing.last_tested_at = datetime.now(UTC)
            existing.last_error_code = None
            record_audit(
                db,
                actor_user_id=principal.user.id,
                action="connection.reauthorized",
                resource_type="data_connection",
                resource_id=existing.id,
                project_id=project_id,
                request_id=request_id,
                details={"kind": existing.kind},
            )
            await _renew_picked(db, box, principal, project_id, access, request_id)
            await db.flush()
            response.status_code = 200
            return ok(_item(existing), "Connection renewed")
    if existing is not None:
        row = await db.scalar(
            select(DataConnection)
            .where(DataConnection.id == existing.id, DataConnection.project_id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is not None:
            row.secret_ciphertext = box.seal(secret)
            row.config = config
            row.last_tested_at = datetime.now(UTC)
            row.last_error_code = None
            record_audit(
                db,
                actor_user_id=principal.user.id,
                action="connection.reauthorized",
                resource_type="data_connection",
                resource_id=row.id,
                project_id=project_id,
                request_id=request_id,
                details={"kind": row.kind, **_audit_target(config)},
            )
            await db.flush()
            response.status_code = 200
            return ok(_item(row), "Already connected")
    # Not the account's address by default: the name is audited, and the address is personal.
    name = body.name or await _free_name(db, project_id, "Google account")
    if await _name_taken(db, project_id, name):
        raise _name_exists()
    if by_google:
        if isinstance(body, CreateGoogleConnection):
            # With the row, in one transaction: a grant is spent only by a connection that exists.
            await _use_grant(db, principal, project_id, body.grant_id)
        if isinstance(connector, GoogleSheetsConnector):
            config["title"] = connector.title
        if isinstance(connector, GoogleDriveConnector):
            config["folder_name"] = connector.folder_name
    now = datetime.now(UTC)
    row = DataConnection(
        id=connection_id,
        project_id=project_id,
        name=name,
        kind=body.kind,
        config=config,
        secret_ciphertext=box.seal(secret),
        last_tested_at=now,
        created_by_user_id=principal.user.id,
    )
    db.add(row)
    if isinstance(body, CreateGoogleConnection):
        await _renew_picked(db, box, principal, project_id, access, request_id)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.created",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=request_id,
        details={
            "name": name,
            "kind": body.kind,
            **{key: value for key, value in _audit_target(config).items() if key != "port"},
        },
    )
    await db.flush()
    return ok(_item(row), "Connection created")


async def _renew_picked(
    db: AsyncSession,
    box: SecretBox,
    principal: Principal,
    project_id: UUID,
    access: _GoogleAccess,
    request_id: str | None,
    skip: UUID | None = None,
) -> None:
    """Give the spreadsheets and folders the member picked with this Google account its new access.

    Each holds a copy of the account's access, and Google takes all of them back at once
    (after 7 days while the app is in testing, or when the member removes it). Signing in
    again restores them together, instead of one Reconnect each. Another member's files, and
    files of another Google account, are left alone.
    """
    rows = await db.scalars(
        select(DataConnection)
        .where(
            DataConnection.project_id == project_id,
            DataConnection.created_by_user_id == principal.user.id,
            DataConnection.kind.in_(("google_sheets", "google_drive")),
        )
        .with_for_update()
    )
    for row in rows:
        if row.id == skip or row.config.get("google_subject") != access.subject:
            continue
        row.secret_ciphertext = box.seal(access.secret)
        # The same account may have changed its address since.
        row.config = {**row.config, "account_email": access.email}
        if row.last_error_code == "access_revoked":
            row.last_error_code = None
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="connection.reauthorized",
            resource_type="data_connection",
            resource_id=row.id,
            project_id=project_id,
            request_id=request_id,
            details={"kind": row.kind, **_audit_target(row.config)},
        )


async def _check_one_account(
    db: AsyncSession,
    principal: Principal,
    project_id: UUID,
    access: _GoogleAccess,
) -> DataConnection | None:
    """The caller's Google account in the project, which new access of the same account renews.

    Another account is refused: the member deletes the one they have first.
    """
    existing = await db.scalar(
        select(DataConnection)
        .where(
            DataConnection.project_id == project_id,
            DataConnection.kind == "google",
            DataConnection.created_by_user_id == principal.user.id,
        )
        .with_for_update()
    )
    if existing is not None and existing.config.get("google_subject") != access.subject:
        raise APIError(
            409,
            "GOOGLE_ACCOUNT_CONNECTED",
            "You already connected another Google account here. "
            "Disconnect it first to use this one.",
        )
    return existing


async def _account_access(
    db: AsyncSession, box: SecretBox, principal: Principal, project_id: UUID, account_id: UUID
) -> _GoogleAccess:
    """What the caller's own Google account connection holds; only they may use it."""
    row = await db.scalar(
        select(DataConnection).where(
            DataConnection.id == account_id, DataConnection.project_id == project_id
        )
    )
    if row is None or not own_account(row, principal):
        raise APIError(404, "NOT_FOUND", "Google account was not found")
    return _GoogleAccess(
        row.config["google_subject"], row.config.get("account_email", ""), open_secret(box, row)
    )


@router.get(
    "/{connection_id}",
    response_model=ApiResponse[ConnectionItem],
    summary="Get a data connection",
    description=(
        "Any project member can read; another member's Google account answers 404. "
        "Credentials are never returned."
    ),
    responses={404: CONNECTION_ERRORS[404]},
)
async def get_connection(
    project_id: UUID,
    connection_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ConnectionItem]:
    await require_project_access(db, principal, project_id)
    return ok(_item(await visible_connection(db, principal, project_id, connection_id)))


@router.patch(
    "/{connection_id}",
    response_model=ApiResponse[ConnectionItem],
    summary="Rename a data connection",
    description=(
        "Only the name can change. To point at another server, database or user, delete the "
        "connection and create a new one. Its creator or a Project Manager only."
    ),
    responses=CONNECTION_ERRORS,
)
async def rename_connection(
    project_id: UUID,
    connection_id: UUID,
    body: RenameConnection,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ConnectionItem]:
    await lock_project_scope(db, project_id)
    project, membership = await require_project_access(
        db, principal, project_id, contribute=True, lock=True
    )
    ensure_writable_project(project)
    row = await visible_connection(db, principal, project_id, connection_id)
    require_owner_or_manager(row, principal, membership)
    if await _name_taken(db, project_id, body.name, except_id=row.id):
        raise _name_exists()
    previous = row.name
    row.name = body.name
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.renamed",
        resource_type="data_connection",
        resource_id=row.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"from": previous, "to": body.name},
    )
    await db.flush()
    return ok(_item(row), "Connection renamed")


@router.delete(
    "/{connection_id}",
    response_model=ApiResponse[None],
    summary="Delete a data connection",
    description=(
        "Removes the connection and its stored credentials. Datasets already imported from it "
        "are unaffected. Its creator or a Project Manager only."
    ),
    responses=CONNECTION_ERRORS,
)
async def delete_connection(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    project, membership = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    row = await visible_connection(db, principal, project_id, connection_id)
    require_owner_or_manager(row, principal, membership)
    await db.delete(row)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.deleted",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"name": row.name, "kind": row.kind},
    )
    return ok(None, "Connection deleted")


@router.post(
    "/{connection_id}/test",
    response_model=ApiResponse[ConnectionItem],
    summary="Test a saved connection again",
    description=(
        "Connects with the stored credentials and records the outcome in `last_tested_at` and "
        "`last_error_code`. A failed test still answers 200: read `last_error_code`. "
        "409 `CONNECTION_SECRET_UNREADABLE` means the server's key changed and the "
        "connection has to be created again."
    ),
    responses=PROBE_ERRORS,
)
async def test_connection(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[ConnectionItem]:
    project, membership = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    box = require_box(box)
    gate.check_rate(principal.user.id)
    row = await visible_connection(db, principal, project_id, connection_id)
    secret = open_secret(box, row)
    kind, config = row.kind, row.config
    await db.commit()

    _, error = await _probe(gate, factory, project_id, principal.user.id, kind, config, secret)
    # The wait may have been long: the project, the caller's role and the row itself are
    # read again, and the row is locked so a concurrent delete cannot slip in before the write.
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    row = await db.scalar(
        select(DataConnection)
        .where(DataConnection.id == connection_id, DataConnection.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Connection was not found")
    row.last_tested_at = datetime.now(UTC)
    row.last_error_code = error.reason if error else None
    if error is None:
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="connection.tested",
            resource_type="data_connection",
            resource_id=connection_id,
            project_id=project_id,
            request_id=getattr(request.state, "request_id", None),
        )
    else:
        _audit_failed_test(db, request, principal, project_id, connection_id, kind, config, error)
    await db.flush()
    return ok(_item(row), "Connection tested")


@router.post(
    "/{connection_id}/reauthorize",
    response_model=ApiResponse[ConnectionItem],
    summary="Give a Google connection a fresh access to the same Google account",
    description=(
        "For a connection whose `last_error_code` or read failure says `access_revoked`: "
        "send the user through `connections/google/start` again and pass the new "
        "`google_grant` here. The connection keeps its ID and what it points at. The grant "
        "must come from the Google account the connection was created with (422 "
        "`GOOGLE_ACCOUNT_MISMATCH` otherwise), and the spreadsheet or folder must open with it "
        "(422 `CONNECTION_FAILED`); when either fails the stored access stays as it was. "
        "When the caller created it, their own Google account and the other spreadsheets and "
        "folders they picked with the same Google account get the new access too."
    ),
    responses={
        **PROBE_ERRORS,
        422: {
            "model": ErrorResponse,
            "description": (
                "`GOOGLE_GRANT_INVALID`, `GOOGLE_ACCOUNT_MISMATCH`, `CONNECTION_FAILED`, or "
                "`CONNECTION_NOT_GOOGLE` for a connection of another kind"
            ),
        },
    },
)
async def reauthorize_connection(
    project_id: UUID,
    connection_id: UUID,
    body: ReauthorizeConnection,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[ConnectionItem]:
    project, membership = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    box = require_box(box)
    gate.check_rate(principal.user.id)
    row = await visible_connection(db, principal, project_id, connection_id)
    kind, config = row.kind, row.config
    if "google_subject" not in config:
        raise APIError(422, "CONNECTION_NOT_GOOGLE", "Only a Google connection can be reauthorized")
    access = await _google_access(request, db, box, principal, project_id, body.grant_id)

    def same_account(config: dict[str, Any]) -> None:
        # A connection says whose access it reads with. A token of another account would
        # make that untrue, and hand the project whatever that account can open.
        if access.subject != config["google_subject"]:
            raise APIError(
                422,
                "GOOGLE_ACCOUNT_MISMATCH",
                "Use the Google account this connection was created with",
            )

    same_account(config)
    await db.commit()

    _, error = await _probe(
        gate, factory, project_id, principal.user.id, kind, config, access.secret
    )
    if error is not None:
        _audit_failed_test(db, request, principal, project_id, connection_id, kind, config, error)
        # Committed first: the rollback that follows an error would erase the audit row.
        await db.commit()
        raise APIError(422, "CONNECTION_FAILED", error.message, reason=error.reason)

    # As after any wait: everything is read again, and the row is locked for the write.
    project, _ = await require_project_access(db, principal, project_id, contribute=True)
    ensure_writable_project(project)
    row = await db.scalar(
        select(DataConnection)
        .where(DataConnection.id == connection_id, DataConnection.project_id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise APIError(404, "NOT_FOUND", "Connection was not found")
    same_account(row.config)
    await _use_grant(db, principal, project_id, body.grant_id)
    row.secret_ciphertext = box.seal(access.secret)
    # The same account may have changed its address since.
    row.config = {**row.config, "account_email": access.email}
    row.last_tested_at = datetime.now(UTC)
    row.last_error_code = None
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.reauthorized",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"kind": kind, **_audit_target(config)},
    )
    if row.created_by_user_id == principal.user.id:
        # One Reconnect renews the member's Google account and every file they picked with it.
        account = await db.scalar(
            select(DataConnection)
            .where(
                DataConnection.project_id == project_id,
                DataConnection.kind == "google",
                DataConnection.created_by_user_id == principal.user.id,
            )
            .with_for_update()
        )
        if account is not None and account.config.get("google_subject") == access.subject:
            account.secret_ciphertext = box.seal(access.secret)
            account.config = {**account.config, "account_email": access.email}
            account.last_error_code = None
        await _renew_picked(
            db,
            box,
            principal,
            project_id,
            access,
            getattr(request.state, "request_id", None),
            skip=row.id,
        )
    await db.flush()
    return ok(_item(row), "Connection reauthorized")


@router.get(
    "/{connection_id}/schemas",
    response_model=ApiResponse[list[str]],
    summary="List the schemas of the connected database",
    description=(
        "Read live from the external database: only schemas its user can read from. "
        "Project Manager or Researcher only."
    ),
    responses=READ_ERRORS,
)
async def list_schemas(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[list[str]]:
    saved = await saved_connection(db, principal, project_id, connection_id, box, gate)
    async with reading(
        request, db, gate, factory, project_id, principal.user.id, saved
    ) as connector:
        return ok(await connector.list_schemas())


@router.get(
    "/{connection_id}/tables",
    response_model=ApiResponse[list[TableItem]],
    summary="List the tables and views of one schema",
    description=(
        f"Read live, by name, at most {MAX_TABLES}; narrow a larger schema with `search`. "
        "Materialized views are listed as views."
    ),
    responses=READ_ERRORS,
)
async def list_tables(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    schema: Annotated[Identifier, Query()],
    search: Annotated[
        str | None,
        Query(
            max_length=120,
            pattern=r"^[^\x00]*$",
            description=(
                "Only tables whose name contains this text, in any letter case. "
                "Empty means all of them."
            ),
        ),
    ] = None,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[list[TableItem]]:
    saved = await saved_connection(db, principal, project_id, connection_id, box, gate)
    async with reading(
        request, db, gate, factory, project_id, principal.user.id, saved
    ) as connector:
        tables = await connector.list_tables(schema, search=search or None, limit=MAX_TABLES)
    return ok(
        [
            TableItem(
                schema_name=table.schema,
                name=table.name,
                type=table.type,
                column_count=table.column_count,
            )
            for table in tables
        ]
    )


@router.get(
    "/{connection_id}/columns",
    response_model=ApiResponse[list[ColumnItem]],
    summary="List the columns of one table or view",
    description=(
        "Names and types in the table's own order, read from the database's metadata: no row "
        "is read. 422 `SOURCE_INVALID` with reason `source_not_found` when the table is not "
        "there."
    ),
    responses=READ_ERRORS,
)
async def list_columns(
    project_id: UUID,
    connection_id: UUID,
    request: Request,
    schema: Annotated[Identifier, Query()],
    table: Annotated[Identifier, Query()],
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[list[ColumnItem]]:
    saved = await saved_connection(db, principal, project_id, connection_id, box, gate)
    async with reading(
        request, db, gate, factory, project_id, principal.user.id, saved
    ) as connector:
        columns = await connector.list_columns(schema, table)
    return ok([ColumnItem.of(column) for column in columns])


@router.post(
    "/{connection_id}/preview",
    response_model=ApiResponse[PreviewData],
    summary="Preview the rows of a table, a SELECT statement or a time series",
    description=(
        "Reads live and returns the first rows; nothing is stored. SQL sources run inside a "
        "read-only transaction; a time series source is a form, not query text. One source "
        "per call. When the database rejects a query, its own message is returned (422 "
        "`SOURCE_INVALID`, reason `query_failed`). Every preview is audited, "
        "with a hash of the SQL rather than its text. On BigQuery a table is read without a "
        "query, at no cost; a query is checked first, and one that would scan more than the "
        "server allows is refused (reason `scan_limit_exceeded`)."
    ),
    responses=READ_ERRORS,
)
async def preview_source(
    project_id: UUID,
    connection_id: UUID,
    body: PreviewRequest,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[PreviewData]:
    saved = await saved_connection(db, principal, project_id, connection_id, box, gate)
    source = body.source
    require_supported_source(saved.kind, source)
    # Committed before the query runs, so one that fails or never returns is on record too.
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="connection.previewed",
        resource_type="data_connection",
        resource_id=connection_id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details=source_audit_details(source),
    )

    limit = request.app.state.settings.connection_preview_max_rows
    rows: list[list[str | None]] = []
    size = 0
    truncated = False
    async with (
        reading(request, db, gate, factory, project_id, principal.user.id, saved) as connector,
        # One row more than is shown tells whether the source goes on.
        connector.open_rows(source, max_rows=limit + 1) as stream,
    ):
        async for row in stream.rows:
            if len(rows) == limit or size >= PREVIEW_SIZE:
                truncated = True
                break
            rows.append([_preview_cell(value) for value in row])
            size += approximate_size(row)
        columns = [ColumnItem.of(column) for column in stream.columns]
    return ok(PreviewData(columns=columns, rows=rows, truncated=truncated))


@router.post(
    "/{connection_id}/live",
    response_model=ApiResponse[LiveData],
    summary="Read the latest points of a metric or measurement, for a chart that follows it",
    description=(
        "For `prometheus` and `influxdb` connections; any other kind answers 422 "
        "`SOURCE_INVALID` with reason `unsupported_source`. Returns one value per bucket for "
        "the span that ends now, read live; nothing is stored. Call it again to follow the "
        "metric: every call counts against the same budget as the other reads and holds a "
        "slot while it runs, so wait for one answer before asking for the next, and stop "
        "while the page is hidden or an import is running. The bucket still being filled is "
        "left out. Viewing is audited once in 10 minutes for each user, connection and "
        "metric, not on every call."
    ),
    responses=READ_ERRORS,
)
async def live_source_points(
    project_id: UUID,
    connection_id: UUID,
    body: LiveRequest,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    box: SecretBox | None = Depends(get_secret_box),
    gate: ConnectionGate = Depends(get_connection_gate),
    factory: ConnectorFactory = Depends(get_connector_factory),
) -> ApiResponse[LiveData]:
    saved = await saved_connection(db, principal, project_id, connection_id, box, gate)
    source = live_source(
        body.name, body.fields, body.tags, body.aggregate, body.last, now=datetime.now(UTC)
    )
    require_supported_source(saved.kind, source)

    viewer = (principal.user.id, connection_id, body.name)
    rows: list[list[str | None]] = []
    truncated = False
    async with reading(
        request, db, gate, factory, project_id, principal.user.id, saved
    ) as connector:
        # Once a slot is held and the connector is built, so a call that got no further
        # than that leaves nothing behind, and before the points are asked for, so a read
        # the server fails or never answers is on record too.
        if gate.first_view_in_window(*viewer):
            record_audit(
                db,
                actor_user_id=principal.user.id,
                action="connection.live_viewed",
                resource_type="data_connection",
                resource_id=connection_id,
                project_id=project_id,
                request_id=getattr(request.state, "request_id", None),
                details={"name": body.name, "last": body.last, "aggregate": body.aggregate},
            )
            try:
                await db.commit()
            except BaseException:
                # Not on record after all: the next call is the one to record.
                gate.forget_view(*viewer)
                raise
        # One row more than is returned tells whether there were more.
        async with connector.open_rows(source, max_rows=LIVE_MAX_ROWS + 1) as stream:
            async for row in stream.rows:
                if len(rows) == LIVE_MAX_ROWS:
                    truncated = True
                    break
                rows.append([to_text(value) for value in row])
            columns = [ColumnItem.of(column) for column in stream.columns]
    return ok(
        LiveData(
            columns=columns,
            rows=rows,
            start=source.start,
            end=source.end,
            bucket=source.bucket,
            truncated=truncated,
        )
    )
