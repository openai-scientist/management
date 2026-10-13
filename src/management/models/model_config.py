from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from management.db.base import Base


class ModelProfile(Base):
    __tablename__ = "model_profiles"
    __table_args__ = (
        CheckConstraint("role IN ('stage','review')", name="ck_model_profiles_role"),
        CheckConstraint("scope = 'global'", name="ck_model_profiles_scope"),
        UniqueConstraint("role", "scope", name="uq_model_profiles_role_scope"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    role: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str | None] = mapped_column(String(500))
    model: Mapped[str] = mapped_column(String(200))
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    scope: Mapped[str] = mapped_column(String(20), default="global")
    input_usd_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    output_usd_per_mtok: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class IntegrationSecret(Base):
    __tablename__ = "integration_secrets"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
