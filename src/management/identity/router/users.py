import math
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    ensure_user_suspension_keeps_project_managers,
    lock_user,
    lock_user_project_scopes,
)
from management.audit.service import record_audit
from management.core.config import Settings
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.roles import PlatformRole
from management.core.search import SearchTerm, matches
from management.core.sessions import (
    Principal,
    as_utc,
    normalize_email,
    require_active_csrf,
    require_active_principal,
    require_platform_admin,
    require_user_id,
)
from management.core.urls import api_prefix
from management.db.session import get_db
from management.identity.avatars import (
    AVATAR_TYPES,
    AVATAR_UPLOAD_ERRORS,
    remove_avatar,
    replace_avatar,
    serve_avatar,
)
from management.identity.models import AuthSession, User, UserPlatformRole, UserStatus
from management.identity.one_time_codes import OtpPurpose, clear_code
from management.identity.schemas.auth import ProfileUpdate
from management.identity.schemas.users import (
    PlatformRoleUpdate,
    UserAdminItem,
    UserCreate,
    UserCreated,
    UserStatusUpdate,
)
from management.identity.service import (
    _active_platform_admin_count,
    _admin_item,
    _created_item,
    _lock_platform_admin_set,
    _send_account_invite,
    _user_item,
    display_name_or_local_part,
    record_profile_update,
)
from management.infra.email_sender import EmailSender, get_email_sender
from management.infra.file_store import FileStore, get_file_store

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=ApiResponse[list[UserAdminItem]])
async def list_users(
    q: SearchTerm = None,
    status: UserStatus | None = None,
    platform_role: PlatformRole | None = None,
    email_verified: bool | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[list[UserAdminItem]]:
    filters = []
    if q:
        filters.append(matches(q, User.email, User.display_name))
    if status is not None:
        filters.append(User.status == status)
    if platform_role is not None:
        # A stored row means Platform Admin; no row means `user`.
        is_admin = User.id.in_(select(UserPlatformRole.user_id))
        filters.append(is_admin if platform_role == PlatformRole.PLATFORM_ADMIN else ~is_admin)
    if email_verified is not None:
        verified = User.email_verified_at.is_not(None)
        filters.append(verified if email_verified else ~verified)
    total = int(await db.scalar(select(func.count()).select_from(User).where(*filters)) or 0)
    rows = (
        await db.execute(
            select(User, UserPlatformRole.role_code)
            .outerjoin(UserPlatformRole, UserPlatformRole.user_id == User.id)
            .where(*filters)
            .order_by(User.created_at.desc(), User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = [_admin_item(user, role, prefix) for user, role in rows]
    return paginated(items, total=total, limit=limit, offset=offset)


@router.post(
    "",
    status_code=201,
    response_model=ApiResponse[UserCreated],
    summary="Create an account for someone else",
    description=(
        "A Platform Admin creates an active account from an email alone, always with the "
        "`user` role. Nobody is signed in, and the email is not verified until the user "
        "signs in with an emailed code. With `send_email` the user is also told by email "
        "how to sign in; `invite_email_sent` says whether that worked. An address that "
        "started signing in but never verified is taken over and stays unverified."
    ),
    responses={
        409: {"model": ErrorResponse, "description": "A verified account has this email"},
    },
)
async def create_user(
    body: UserCreate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
    sender: EmailSender = Depends(get_email_sender),
) -> ApiResponse[UserCreated]:
    settings: Settings = request.app.state.settings
    normalized = normalize_email(body.email)
    taken = APIError(409, "EMAIL_ALREADY_REGISTERED", "An account with this email already exists")
    display_name = display_name_or_local_part(body.display_name, body.email)
    invite_sent_at = datetime.now(UTC) if body.send_email else None
    existing_id = await db.scalar(select(User.id).where(User.email_normalized == normalized))
    if existing_id is not None:
        # Someone registered this address and never verified it. The admin's account
        # replaces theirs; whoever owns the inbox still has to verify.
        user = await lock_user(db, existing_id)
        if user.email_verified_at is not None or user.status == UserStatus.SUSPENDED:
            raise taken
        user.display_name = display_name
        if body.display_name:
            user.onboarded_at = user.onboarded_at or datetime.now(UTC)
        user.created_by_user_id = principal.user.id
        user.invite_sent_at = invite_sent_at
        for purpose in OtpPurpose:
            await clear_code(db, user, purpose)
    else:
        user = User(
            email=body.email,
            email_normalized=normalized,
            display_name=display_name,
            status=UserStatus.ACTIVE,
            onboarded_at=datetime.now(UTC) if body.display_name else None,
            created_by_user_id=principal.user.id,
            invite_sent_at=invite_sent_at,
        )
        db.add(user)
        try:
            await db.flush()
        except IntegrityError as exc:
            # Someone took the same email between the check and the insert.
            raise taken from exc
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="user.created",
        resource_type="user",
        resource_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        details={
            "status": UserStatus.ACTIVE,
            "send_email": body.send_email,
            "replaced_unverified": existing_id is not None,
        },
    )
    # Commit first: the email must never describe an account that was rolled back.
    await db.commit()
    # Built before sending: nothing may touch the database once the email has gone.
    item = _admin_item(user, None, prefix)
    sent = body.send_email and await _send_account_invite(settings, sender, user)
    return ok(_created_item(item, user, invite_email_sent=sent), "User created")


@router.post(
    "/{user_id}/invite",
    response_model=ApiResponse[UserCreated],
    summary="Email a user's sign-in details again",
    description=(
        "Emails the user again how to sign in. Works only while the user has never "
        "signed in (`email_verified` is false)."
    ),
    responses={
        409: {
            "model": ErrorResponse,
            "description": "The user has already signed in, or is suspended",
        },
        429: {
            "model": ErrorResponse,
            "description": "The details were sent a moment ago",
            "headers": {
                "Retry-After": {
                    "description": "Seconds to wait before sending again",
                    "schema": {"type": "integer"},
                }
            },
        },
    },
)
async def resend_account_invite(
    user_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
    sender: EmailSender = Depends(get_email_sender),
) -> ApiResponse[UserCreated]:
    settings: Settings = request.app.state.settings
    target = await lock_user(db, user_id)
    if target.status == UserStatus.SUSPENDED:
        raise APIError(409, "USER_SUSPENDED", "This account is suspended")
    if target.email_verified_at is not None:
        raise APIError(409, "INVITE_NOT_PENDING", "The user has already signed in")
    now = datetime.now(UTC)
    if target.invite_sent_at is not None:
        waited = (now - as_utc(target.invite_sent_at)).total_seconds()
        remaining = settings.invite_resend_cooldown_seconds - waited
        if remaining > 0:
            raise APIError(
                429,
                "INVITE_COOLDOWN",
                "The sign-in details were sent a moment ago",
                retry_after=math.ceil(remaining),
            )
    target.invite_sent_at = now
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="user.invite_sent",
        resource_type="user",
        resource_id=target.id,
        request_id=getattr(request.state, "request_id", None),
    )
    item = await _user_item(db, target, prefix)
    await db.commit()
    sent = await _send_account_invite(settings, sender, target)
    return ok(_created_item(item, target, invite_email_sent=sent), "Invite sent")


@router.patch(
    "/{user_id}",
    response_model=ApiResponse[UserAdminItem],
    summary="Edit a user's profile",
    description="A Platform Admin changes a user's display name. The email cannot be changed.",
)
async def update_user_profile(
    user_id: str,
    body: ProfileUpdate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserAdminItem]:
    target = await lock_user(db, require_user_id(user_id))
    record_profile_update(
        db, request, actor_user_id=principal.user.id, user=target, display_name=body.display_name
    )
    await db.flush()
    return ok(await _user_item(db, target, prefix), "Profile updated")


@router.post(
    "/{user_id}/avatar",
    response_model=ApiResponse[UserAdminItem],
    summary="Upload or replace a user's picture",
    description="Platform Admin only. A multipart form with one `file`: PNG, JPEG or WebP.",
    responses=AVATAR_UPLOAD_ERRORS,
)
async def upload_user_avatar(
    user_id: UUID,
    request: Request,
    file: UploadFile = File(description="A PNG, JPEG or WebP image"),
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserAdminItem]:
    target = await replace_avatar(
        db, store, request, actor_user_id=principal.user.id, user_id=user_id, upload=file
    )
    return ok(await _user_item(db, target, prefix), "Picture updated")


@router.delete(
    "/{user_id}/avatar",
    response_model=ApiResponse[UserAdminItem],
    summary="Remove a user's picture",
    description="Platform Admin only.",
)
async def delete_user_avatar(
    user_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserAdminItem]:
    target = await remove_avatar(
        db, store, request, actor_user_id=principal.user.id, user_id=user_id
    )
    return ok(await _user_item(db, target, prefix), "Picture removed")


@router.get(
    "/{user_id}/avatar",
    summary="Get a user's picture",
    description=(
        "Any signed-in user. With the `v` value from `avatar_url` the answer may be cached "
        "for good; without it, or with an old one, it is checked again on every use."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {content_type: {} for content_type in AVATAR_TYPES.values()},
            "description": "The image exactly as it was uploaded",
        },
        404: {"model": ErrorResponse, "description": "The user has no picture"},
    },
)
async def get_user_avatar(
    user_id: UUID,
    v: str | None = Query(default=None, max_length=32),
    _: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> StreamingResponse:
    return await serve_avatar(db, store, user_id, v)


@router.patch("/{user_id}/status", response_model=ApiResponse[UserAdminItem])
async def update_user_status(
    user_id: str,
    body: UserStatusUpdate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserAdminItem]:
    target_id = require_user_id(user_id)
    await _lock_platform_admin_set(db)
    if body.status == UserStatus.SUSPENDED:
        await lock_user_project_scopes(db, target_id)
    target = await lock_user(db, target_id)
    if target.status == body.status:
        return ok(await _user_item(db, target, prefix))
    if body.status == UserStatus.SUSPENDED and await db.get(UserPlatformRole, target.id):
        if target.status == UserStatus.ACTIVE and await _active_platform_admin_count(db) <= 1:
            raise APIError(
                409, "LAST_PLATFORM_ADMIN", "The last active Platform Admin cannot be suspended"
            )
    if body.status == UserStatus.SUSPENDED and target.status == UserStatus.ACTIVE:
        await ensure_user_suspension_keeps_project_managers(db, target)
    before = target.status
    target.status = body.status
    now = datetime.now(UTC)
    if body.status == UserStatus.SUSPENDED:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.user_id == target.id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="user.status_changed",
        resource_type="user",
        resource_id=target.id,
        request_id=getattr(request.state, "request_id", None),
        details={"before": before, "after": body.status},
    )
    await db.flush()
    return ok(await _user_item(db, target, prefix))


@router.put("/{user_id}/platform-role", response_model=ApiResponse[UserAdminItem])
async def update_platform_role(
    user_id: str,
    body: PlatformRoleUpdate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    _: Principal = Depends(require_platform_admin),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserAdminItem]:
    target_id = require_user_id(user_id)
    await _lock_platform_admin_set(db)
    target = await lock_user(db, target_id)
    existing = await db.get(UserPlatformRole, target.id)
    if body.role == PlatformRole.USER and existing is not None:
        if target.status == UserStatus.ACTIVE and await _active_platform_admin_count(db) <= 1:
            raise APIError(
                409, "LAST_PLATFORM_ADMIN", "The last active Platform Admin role cannot be removed"
            )
        await db.delete(existing)
        action = "platform_admin.role_removed"
    elif body.role == PlatformRole.PLATFORM_ADMIN and existing is None:
        if target.email_verified_at is None:
            raise APIError(409, "EMAIL_NOT_VERIFIED", "This user must verify their email first")
        db.add(UserPlatformRole(user_id=target.id, role_code=body.role))
        action = "platform_admin.role_granted"
    else:
        return ok(await _user_item(db, target, prefix))
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action=action,
        resource_type="user",
        resource_id=target.id,
        request_id=getattr(request.state, "request_id", None),
        details={"role": PlatformRole.PLATFORM_ADMIN},
    )
    await db.flush()
    return ok(await _user_item(db, target, prefix))
