import logging
from datetime import UTC, datetime, timedelta

from fastapi import BackgroundTasks, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.service import record_audit
from management.core.config import Settings
from management.core.errors import APIError
from management.core.roles import platform_role_from_code
from management.core.security import (
    new_session_secret,
    token_digest,
)
from management.core.sessions import (
    Principal,
    csrf_for_principal,
    email_local_part,
    normalize_email,
    set_session_cookie,
)
from management.core.urls import avatar_url
from management.identity import auth_emails
from management.identity.models import AuthSession, User, UserPlatformRole, UserStatus
from management.identity.one_time_codes import (
    OtpPurpose,
)
from management.identity.schemas.auth import (
    SessionDetails,
    SessionResult,
    UserProfile,
    VerificationPending,
)
from management.identity.schemas.users import UserAdminItem, UserCreated
from management.infra.email_sender import EmailSender
from management.infra.email_templates import account_invite

logger = logging.getLogger("management.identity")


def masked_email(email: str) -> str:
    """`a***@example.com`: enough to recognise an address, not to learn it."""
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}"


async def _user_profile(db: AsyncSession, user: User, prefix: str) -> UserProfile:
    role = await db.get(UserPlatformRole, user.id)
    return UserProfile(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        avatar_url=avatar_url(prefix, user.id, user.avatar_storage_key),
        status=UserStatus(user.status),
        platform_role=platform_role_from_code(role.role_code if role else None),
        email_verified=user.email_verified_at is not None,
        onboarded=user.onboarded_at is not None,
        created_at=user.created_at,
    )


def display_name_or_local_part(display_name: str | None, email: str) -> str:
    """The given name, or the part of the email before the @ when none was given."""
    return (display_name or "").strip() or email_local_part(email)


def _session_details(session: AuthSession) -> SessionDetails:
    return SessionDetails(
        created_at=session.created_at,
        last_seen_at=session.last_seen_at,
        idle_expires_at=session.idle_expires_at,
        absolute_expires_at=session.absolute_expires_at,
    )


async def start_session(
    db: AsyncSession, response: Response, settings: Settings, user: User
) -> SessionResult:
    raw_secret = new_session_secret()
    now = datetime.now(UTC)
    session = AuthSession(
        user_id=user.id,
        token_digest=token_digest(raw_secret),
        created_at=now,
        last_seen_at=now,
        idle_expires_at=now + timedelta(minutes=settings.session_idle_minutes),
        absolute_expires_at=now + timedelta(days=settings.session_absolute_days),
    )
    db.add(session)
    user.last_login_at = now
    # The response leaves before the request's own commit runs, and the cookie must never
    # name a session that is not stored yet.
    await db.commit()
    set_session_cookie(response, settings, raw_secret)
    return SessionResult(
        user=await _user_profile(db, user, settings.api_prefix.rstrip("/")),
        session=_session_details(session),
        csrf_token=csrf_for_principal(
            Principal(user=user, session=session, raw_secret=raw_secret), settings
        ),
    )


def _pending(settings: Settings, email: str) -> VerificationPending:
    """Built from settings alone, so the answer is the same whatever the account's state."""
    return VerificationPending(
        email=email,
        expires_in_seconds=settings.otp_ttl_minutes * 60,
        resend_after_seconds=settings.otp_resend_cooldown_seconds,
    )


async def _commit_then_send(
    db: AsyncSession,
    background: BackgroundTasks,
    sender: EmailSender,
    to: str,
    message: tuple[str, str, str],
) -> None:
    """Commit, then queue the email to go out once the response is sent.

    The email never describes something that was rolled back, and the provider call does
    not hold the transaction open. The handler must return normally afterwards: a raised
    error would discard the queued email.
    """
    await db.commit()
    subject, text, html = message
    background.add_task(_send_or_warn, sender, to=to, subject=subject, text=text, html=html)


async def _send_or_warn(sender: EmailSender, *, to: str, subject: str, text: str, html: str):
    if not await sender.send(to=to, subject=subject, text=text, html=html):
        logger.warning("Email %r to %s was not accepted by the provider", subject, to)


async def _fail_after_commit(db: AsyncSession, error: APIError) -> None:
    """Raise, but keep what the request counted: the rollback of an error would erase it."""
    await db.commit()
    raise error


def _invalid_code() -> APIError:
    return APIError(400, "OTP_INVALID", "The code is wrong or has expired")


async def _find_user(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(User.email_normalized == normalize_email(email)))


async def _send_code(
    db: AsyncSession,
    background: BackgroundTasks,
    sender: EmailSender,
    settings: Settings,
    user: User,
    purpose: OtpPurpose,
    code: str,
    link_token: str | None = None,
) -> None:
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.otp_ttl_minutes)
    link = (
        f"{settings.app_url.rstrip('/')}/auth/verify#token={link_token}"
        if settings.app_url and link_token
        else None
    )
    message = auth_emails.sign_in_code(user.email, code, link, expires_at, settings.otp_ttl_minutes)

    await _commit_then_send(db, background, sender, user.email, message)


def record_profile_update(
    db: AsyncSession, request: Request, *, actor_user_id, user: User, display_name: str
) -> None:
    """Set the display name and write the audit entry, when the name really changes."""
    if user.display_name == display_name:
        return
    before = user.display_name
    user.display_name = display_name
    record_audit(
        db,
        actor_user_id=actor_user_id,
        action="user.profile_updated",
        resource_type="user",
        resource_id=user.id,
        request_id=getattr(request.state, "request_id", None),
        details={"display_name": {"before": before, "after": display_name}},
    )


async def _lock_platform_admin_set(db: AsyncSession) -> None:
    if db.bind and db.bind.dialect.name == "postgresql":
        from sqlalchemy import text

        await db.execute(text("SELECT pg_advisory_xact_lock(1804, 1)"))


async def _active_platform_admin_count(db: AsyncSession) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(UserPlatformRole)
            .join(User, User.id == UserPlatformRole.user_id)
            .where(User.status == UserStatus.ACTIVE)
        )
        or 0
    )


async def _send_account_invite(settings: Settings, sender: EmailSender, user: User) -> bool:
    subject, text, html = account_invite(
        user.email,
        user.display_name or user.email,
        settings.app_url,
    )
    return await sender.send(to=user.email, subject=subject, text=text, html=html)


def _created_item(item: UserAdminItem, user: User, *, invite_email_sent: bool) -> UserCreated:
    return UserCreated(
        **item.model_dump(),
        invite_email_sent=invite_email_sent,
    )


async def _user_item(db: AsyncSession, user: User, prefix: str) -> UserAdminItem:
    role = await db.get(UserPlatformRole, user.id)
    return _admin_item(user, role.role_code if role else None, prefix)


def _admin_item(user: User, role_code: str | None, prefix: str) -> UserAdminItem:
    return UserAdminItem(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        avatar_url=avatar_url(prefix, user.id, user.avatar_storage_key),
        status=user.status,
        platform_role=platform_role_from_code(role_code),
        email_verified=user.email_verified_at is not None,
        last_login_at=user.last_login_at,
        invite_sent_at=user.invite_sent_at,
        created_by_user_id=str(user.created_by_user_id) if user.created_by_user_id else None,
        created_at=user.created_at,
    )
