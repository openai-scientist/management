from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from management.db.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectIntegration(Base):
    """A chat channel the project posts its events to, through the channel's webhook URL."""

    __tablename__ = "project_integrations"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('slack', 'teams', 'discord')", name="ck_integrations_provider"
        ),
        CheckConstraint("status IN ('active', 'paused', 'error')", name="ck_integrations_status"),
        Index("ix_project_integrations_project_created", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # Event keys from the catalog this integration posts about.
    events: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # Fernet token of {"url": ...}: anyone holding the URL can post to the channel.
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    url_hint: Mapped[str] = mapped_column(String(300), nullable=False)
    # `paused` is the user's choice; `error` is set by delivery when the channel stops taking
    # messages, and cleared when the user turns the integration on again or changes its URL.
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="active")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Raised on every URL change or re-enable, so a delivery sent under the old settings cannot
    # count against the new ones.
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


Index(
    "uq_project_integrations_project_name",
    ProjectIntegration.project_id,
    func.lower(ProjectIntegration.name),
    unique=True,
)


class IntegrationDelivery(Base):
    """One event on its way to one integration: the outbox row a worker sends and retries.

    It holds references only (`ref`), never message text, so a deleted comment is not kept or
    sent from here.
    """

    __tablename__ = "integration_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="ck_deliveries_status"
        ),
        UniqueConstraint("integration_id", "event_id", name="uq_deliveries_integration_event"),
        Index("ix_integration_deliveries_due", "status", "next_attempt_at"),
        Index("ix_integration_deliveries_integration_created", "integration_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    integration_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("project_integrations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Fixed per event, so emitting the same event twice stores it once.
    event_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    event_key: Mapped[str] = mapped_column(String(40), nullable=False)
    ref: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="pending")
    # Failed sends; a 429 or a reclaimed lease does not count.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Times a worker took the row; caps a row that keeps crashing workers.
    claims: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_code: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
