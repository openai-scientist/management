from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from management.db.base import Base


class DataConnection(Base):
    """A saved connection to an external data source that a project can read from."""

    __tablename__ = "data_connections"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('postgres', 'mysql', 'bigquery', 'google', 'google_sheets', "
            "'google_drive', 'prometheus', 'influxdb')",
            name="ck_data_connections_kind",
        ),
        Index("ix_data_connections_project_created", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # Everything that is not secret: host, port, database, username, ssl mode.
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    # Fernet token of the credentials; never returned by the API.
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(40))
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


Index(
    "uq_data_connections_project_name",
    DataConnection.project_id,
    func.lower(DataConnection.name),
    unique=True,
)


class GoogleConnectionGrant(Base):
    """Access to a Google account, held between the user's consent and the connection.

    Made when the browser leaves for Google and filled in when it comes back. A connection
    is then created from it, by the same user in the same project, before it expires.
    """

    __tablename__ = "google_connection_grants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # SHA-256 of the state sent to Google, never the state: it finds the grant on the way back.
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # Fernet token of the refresh token. Empty until Google's answer is in.
    secret_ciphertext: Mapped[str | None] = mapped_column(Text)
    # The Google account that gave access: its permanent ID, and its address at that time.
    google_subject: Mapped[str | None] = mapped_column(String(255))
    account_email: Mapped[str | None] = mapped_column(String(320))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
