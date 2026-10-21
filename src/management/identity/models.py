from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from management.core.roles import PlatformRole
from management.db.base import Base


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("status IN ('active', 'suspended')", name="ck_users_status"),)

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_normalized: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=UserStatus.ACTIVE)
    # When the user first chose a name, or the name came from Google or an admin. Empty: the
    # app asks for a name before anything else.
    onboarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the user proved the email with a one-time code. Empty: the account cannot sign in.
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Google's ID of the account that may sign in as this user, set at the first Google
    # sign-in. Signing in by email never touches it.
    google_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The admin who created the account; empty for a self-registered one.
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_users_created_by_user"),
    )
    avatar_storage_key: Mapped[str | None] = mapped_column(String(400))
    avatar_content_type: Mapped[str | None] = mapped_column(String(32))
    # When the sign-in details were last emailed to an admin-created user.
    invite_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailOtp(Base):
    """The one-time code of one user for one purpose, with the counters that limit its use.

    The row stays after the code is used, so the limits keep applying to the next code.
    """

    __tablename__ = "email_otps"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('sign_in')",
            name="ck_email_otps_purpose",
        ),
        UniqueConstraint("user_id", "purpose", name="uq_email_otps_user_purpose"),
        UniqueConstraint("link_token_digest", name="uq_email_otps_link_token_digest"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    # A keyed digest, never the code. Empty once the code is used or withdrawn.
    code_digest: Mapped[str | None] = mapped_column(String(64))
    # The sign-in link sent with the code; it expires with the code. Empty once used.
    link_token_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Checks made against the current code.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Wrong checks since the last success or lock; a new code does not reset it.
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Codes issued inside the current one-hour window.
    send_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    send_window_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class UserPlatformRole(Base):
    __tablename__ = "user_platform_roles"
    __table_args__ = (
        CheckConstraint("role_code = 'platform_admin'", name="ck_user_platform_roles_code"),
    )

    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_code: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PlatformRole.PLATFORM_ADMIN
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
