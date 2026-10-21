from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, File, Request, Response, UploadFile
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import lock_user
from management.audit.service import record_audit
from management.core.config import Settings
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import (
    Principal,
    clear_session_cookie,
    csrf_for_principal,
    email_local_part,
    get_principal,
    normalize_email,
    require_csrf,
    require_origin,
)
from management.core.urls import api_prefix
from management.db.session import get_db
from management.identity.avatars import (
    AVATAR_UPLOAD_ERRORS,
    remove_avatar,
    replace_avatar,
)
from management.identity.models import AuthSession, User, UserStatus
from management.identity.one_time_codes import (
    OtpPurpose,
    check_code,
    consume_sign_in_link,
    issue_sign_in,
    sign_in_link_owner,
)
from management.identity.schemas.auth import (
    CsrfToken,
    CurrentUser,
    EmailCodeSignIn,
    EmailRequest,
    MembershipSummary,
    ProfileUpdate,
    SessionResult,
    SignInLink,
    SignInLinkInfo,
    UserProfile,
    VerificationPending,
)
from management.identity.service import (
    _fail_after_commit,
    _find_user,
    _invalid_code,
    _pending,
    _send_code,
    _session_details,
    _user_profile,
    masked_email,
    record_profile_update,
    start_session,
)
from management.infra.email_sender import EmailSender, get_email_sender
from management.infra.file_store import FileStore, get_file_store
from management.projects.models import Project, ProjectMembership

router = APIRouter(prefix="/auth", tags=["authentication"])


SESSION_REQUIRED = {
    401: {
        "model": ErrorResponse,
        "description": "The Platform session is missing, expired, revoked, or suspended",
    },
}


SIGN_IN_ERRORS = {
    403: {
        "model": ErrorResponse,
        "description": (
            "Origin is not allowed, the user is suspended, or the email is not verified yet"
        ),
    },
    413: {
        "model": ErrorResponse,
        "description": "The request body exceeds the configured maximum size",
    },
    429: {
        "model": ErrorResponse,
        "description": "The per-instance sign-in rate limit was exceeded",
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying",
                "schema": {"type": "integer"},
            }
        },
    },
}


CSRF_ERRORS = {
    **SESSION_REQUIRED,
    403: {
        "model": ErrorResponse,
        "description": "Origin is not allowed or the CSRF token is invalid",
    },
}


CODE_ERRORS = {
    400: {
        "model": ErrorResponse,
        "description": (
            "`OTP_INVALID`: the code or link is wrong, expired, used, spent after too many "
            "checks or locked, or the account is suspended. One answer for all of them."
        ),
    },
    403: {"model": ErrorResponse, "description": "Origin is not allowed"},
    413: SIGN_IN_ERRORS[413],
    429: {**SIGN_IN_ERRORS[429], "description": "The per-instance code rate limit was exceeded"},
}


async def _signed_in_by_email(
    db: AsyncSession, request: Request, response: Response, settings: Settings, user: User
) -> SessionResult:
    """Start a session for a user who just proved the inbox with a code or a link."""
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
        record_audit(
            db,
            actor_user_id=user.id,
            action="user.email_verified",
            resource_type="user",
            resource_id=user.id,
            request_id=getattr(request.state, "request_id", None),
        )
    return await start_session(db, response, settings, user)


@router.post(
    "/email/start",
    response_model=ApiResponse[VerificationPending],
    summary="Email a sign-in code and link",
    description=(
        "Sends a 6-digit code and a sign-in link to the address. An address without an "
        "account gets one, signed in as soon as the code or link is used. Always answers "
        "the same, whatever the address: nothing is sent to a suspended account, or while "
        "the last code is younger than `resend_after_seconds`, and then the earlier code "
        "and link keep working."
    ),
    responses={key: CODE_ERRORS[key] for key in (403, 413, 429)},
)
async def start_email_sign_in(
    body: EmailRequest,
    request: Request,
    background: BackgroundTasks,
    _: None = Depends(require_origin),
    db: AsyncSession = Depends(get_db),
    sender: EmailSender = Depends(get_email_sender),
) -> ApiResponse[VerificationPending]:
    settings: Settings = request.app.state.settings
    answer = ok(_pending(settings, body.email), "If the address can sign in, a code is on its way")
    user = await _find_user(db, body.email)
    if user is None:
        user = User(
            email=body.email,
            email_normalized=normalize_email(body.email),
            display_name=email_local_part(body.email),
            status=UserStatus.ACTIVE,
        )
        try:
            async with db.begin_nested():
                db.add(user)
                await db.flush()
        except IntegrityError:
            # Someone started with the same email between the check and the insert.
            user = await _find_user(db, body.email)
            if user is None:
                return answer
        else:
            record_audit(
                db,
                actor_user_id=user.id,
                action="user.registered",
                resource_type="user",
                resource_id=user.id,
                request_id=getattr(request.state, "request_id", None),
                details={"status": UserStatus.ACTIVE, "method": "email"},
            )
    user = await lock_user(db, user.id)
    if user.status == UserStatus.SUSPENDED:
        return answer
    issued = await issue_sign_in(db, settings, user)
    if issued is None:
        return answer
    code, link_token = issued
    await _send_code(db, background, sender, settings, user, OtpPurpose.SIGN_IN, code, link_token)
    return answer


@router.post(
    "/email/verify",
    response_model=ApiResponse[SessionResult],
    summary="Sign in with the emailed code",
    description=(
        "Starts a session and verifies the email the first time. The link sent with the "
        "code stops working. `user.onboarded` false means the app should ask for a name."
    ),
    responses=CODE_ERRORS,
)
async def verify_email_code(
    body: EmailCodeSignIn,
    request: Request,
    response: Response,
    _: None = Depends(require_origin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SessionResult]:
    settings: Settings = request.app.state.settings
    user = await _find_user(db, body.email)
    if user is None:
        raise _invalid_code()
    user = await lock_user(db, user.id)
    if user.status == UserStatus.SUSPENDED:
        raise _invalid_code()
    if not await check_code(db, settings, user, OtpPurpose.SIGN_IN, body.code):
        await _fail_after_commit(db, _invalid_code())
    return ok(await _signed_in_by_email(db, request, response, settings, user), "Signed in")


@router.post(
    "/email/link-info",
    response_model=ApiResponse[SignInLinkInfo],
    summary="Show whom a sign-in link signs in",
    description=(
        "Changes nothing. The app shows the partly hidden address and asks the user to "
        "confirm before calling `email/verify-link`."
    ),
    responses=CODE_ERRORS,
)
async def sign_in_link_info(
    body: SignInLink,
    _: None = Depends(require_origin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SignInLinkInfo]:
    owner_id = await sign_in_link_owner(db, body.token)
    user = await db.get(User, owner_id) if owner_id else None
    if user is None or user.status == UserStatus.SUSPENDED:
        raise _invalid_code()
    return ok(SignInLinkInfo(email=masked_email(user.email)), "Link works")


@router.post(
    "/email/verify-link",
    response_model=ApiResponse[SessionResult],
    summary="Sign in with the emailed link",
    description=(
        "Same as `email/verify`, with the token from the link instead of the code. The "
        "code sent with it stops working."
    ),
    responses=CODE_ERRORS,
)
async def verify_sign_in_link(
    body: SignInLink,
    request: Request,
    response: Response,
    _: None = Depends(require_origin),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SessionResult]:
    settings: Settings = request.app.state.settings
    owner_id = await sign_in_link_owner(db, body.token)
    if owner_id is None:
        raise _invalid_code()
    user = await lock_user(db, owner_id)
    if user.status == UserStatus.SUSPENDED or not await consume_sign_in_link(db, user, body.token):
        raise _invalid_code()
    return ok(await _signed_in_by_email(db, request, response, settings, user), "Signed in")


@router.get(
    "/me",
    response_model=ApiResponse[CurrentUser],
    summary="Get the signed-in user and current session",
    description=(
        "Use after a page reload to restore the signed-in user, role, session expiry, "
        "and the projects to show in the menu."
    ),
    responses=SESSION_REQUIRED,
)
async def me(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[CurrentUser]:
    rows = await db.execute(
        select(Project, ProjectMembership.role_code)
        .join(ProjectMembership, ProjectMembership.project_id == Project.id)
        .where(ProjectMembership.user_id == principal.user.id, ProjectMembership.status == "active")
        .order_by(Project.created_at.desc(), Project.id)
    )
    return ok(
        CurrentUser(
            user=await _user_profile(db, principal.user, prefix),
            session=_session_details(principal.session),
            memberships=[
                MembershipSummary(
                    project_id=str(project.id),
                    project_name=project.name,
                    role=role,
                    archived=project.archived_at is not None,
                )
                for project, role in rows
            ],
        )
    )


@router.patch(
    "/me",
    response_model=ApiResponse[UserProfile],
    summary="Edit your own profile",
    description="Changes your display name. The email cannot be changed.",
    responses=CSRF_ERRORS,
)
async def update_my_profile(
    body: ProfileUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_csrf),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserProfile]:
    user = principal.user
    record_profile_update(
        db, request, actor_user_id=user.id, user=user, display_name=body.display_name
    )
    if user.onboarded_at is None:
        user.onboarded_at = datetime.now(UTC)
    return ok(await _user_profile(db, user, prefix), "Profile updated")


@router.post(
    "/me/avatar",
    response_model=ApiResponse[UserProfile],
    summary="Upload or replace your picture",
    description=(
        "Send a multipart form with one `file`: a PNG, JPEG or WebP image. The image is "
        "stored as it is uploaded, so crop and re-encode it in the browser first."
    ),
    responses={**CSRF_ERRORS, **AVATAR_UPLOAD_ERRORS},
)
async def upload_my_avatar(
    request: Request,
    file: UploadFile = File(description="A PNG, JPEG or WebP image"),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_csrf),
    store: FileStore = Depends(get_file_store),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserProfile]:
    user = await replace_avatar(
        db,
        store,
        request,
        actor_user_id=principal.user.id,
        user_id=principal.user.id,
        upload=file,
    )
    return ok(await _user_profile(db, user, prefix), "Picture updated")


@router.delete(
    "/me/avatar",
    response_model=ApiResponse[UserProfile],
    summary="Remove your picture",
    responses=CSRF_ERRORS,
)
async def delete_my_avatar(
    request: Request,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_csrf),
    store: FileStore = Depends(get_file_store),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[UserProfile]:
    user = await remove_avatar(
        db, store, request, actor_user_id=principal.user.id, user_id=principal.user.id
    )
    return ok(await _user_profile(db, user, prefix), "Picture removed")


@router.get(
    "/csrf-token",
    response_model=ApiResponse[CsrfToken],
    summary="Get the CSRF token for the current session",
    description="Send the token in the X-CSRF-Token header on every mutating request.",
    responses=SESSION_REQUIRED,
)
async def get_csrf_token(
    request: Request,
    principal: Principal = Depends(get_principal),
) -> ApiResponse[CsrfToken]:
    return ok(CsrfToken(csrf_token=csrf_for_principal(principal, request.app.state.settings)))


@router.post(
    "/logout",
    response_model=ApiResponse[None],
    summary="Sign out of the current session",
    description=(
        "Revokes the current Platform session and clears its cookie. Requires the "
        "X-CSRF-Token header."
    ),
    responses=CSRF_ERRORS,
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_csrf),
) -> ApiResponse[None]:
    principal.session.revoked_at = datetime.now(UTC)
    clear_session_cookie(response, request.app.state.settings)
    await db.flush()
    return ok(None, "Signed out")


@router.post(
    "/logout-all",
    response_model=ApiResponse[None],
    summary="Sign out of every session of the current user",
    description=(
        "Revokes all Platform sessions of the signed-in user, including other browsers "
        "and devices. Use after a lost device. Requires the "
        "X-CSRF-Token header."
    ),
    responses=CSRF_ERRORS,
)
async def logout_all(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(require_csrf),
) -> ApiResponse[None]:
    await db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == principal.user.id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    clear_session_cookie(response, request.app.state.settings)
    return ok(None, "Signed out of all sessions")
