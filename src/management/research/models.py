from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from management.db.base import Base

ACTIVE_RUN_STATUSES = ("queued", "running", "paused", "awaiting_review")
FINISHED_RUN_STATUSES = ("completed", "budget_exceeded", "failed")
_ACTIVE_RUN = text(
    "status IN ('queued', 'running', 'paused', 'awaiting_review') "
    "OR (status = 'budget_exceeded' AND pipeline_key IS NOT NULL)"
)
_PENDING_REVIEW = text("submitted_at IS NULL")


class ResearchContext(Base):
    """Archived research context retained for historical runs; no write API is exposed."""

    __tablename__ = "research_contexts"
    __table_args__ = (
        UniqueConstraint("project_id", "version_number", name="uq_research_contexts_number"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    front_matter: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class ResearchRun(Base):
    """One execution of Popper on a fixed dataset version or research topic."""

    __tablename__ = "research_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'paused', 'awaiting_review', 'completed', "
            "'budget_exceeded', 'failed', 'stopped')",
            name="ck_research_runs_status",
        ),
        CheckConstraint(
            "interaction_mode IS NULL OR interaction_mode IN ('auto','light','copilot','full')",
            name="ck_research_runs_interaction_mode",
        ),
        CheckConstraint(
            "charter_status IN ('none','pending','settled')", name="ck_research_runs_charter_status"
        ),
        # A project works on one run at a time.
        Index(
            "uq_research_runs_active_project",
            "project_id",
            unique=True,
            postgresql_where=_ACTIVE_RUN,
            sqlite_where=_ACTIVE_RUN,
        ),
        Index("ix_research_runs_project_created", "project_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), nullable=False
    )
    dataset_version_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("dataset_versions.id", ondelete="RESTRICT"), nullable=True
    )
    research_context_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_contexts.id", ondelete="RESTRICT"), nullable=True
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Topic-to-hypothesis additions
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    domains: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    review_mode: Mapped[str | None] = mapped_column(String(20), nullable=True, default="copilot")
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_source_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pipeline_key: Mapped[str | None] = mapped_column(String(100))
    current_stage_key: Mapped[str | None] = mapped_column(String(100))
    interaction_mode: Mapped[str | None] = mapped_column(String(20))
    iteration: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    cost_unknown: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # The AI PI's research charter (current version) and whether it still awaits a decision.
    charter: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    charter_status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="none", server_default="none"
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    auto_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False, default=Decimal("0"))
    popper_run_id: Mapped[str | None] = mapped_column(String(200), unique=True)
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class RunEvent(Base):
    """One immutable event in the stream of a research run."""

    __tablename__ = "run_events"
    __table_args__ = (
        CheckConstraint("seq >= 1", name="ck_run_events_seq"),
        Index("ix_run_events_run_id_seq", "run_id", "seq"),
        Index(
            "run_events_source_seq",
            "stage_run_id",
            "source_seq",
            unique=True,
            postgresql_where=text("source_seq IS NOT NULL"),
            sqlite_where=text("source_seq IS NOT NULL"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id")
    )
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    stage_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    step_key: Mapped[str | None] = mapped_column(String(100))
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    phase: Mapped[str | None] = mapped_column(String(32))
    event_class: Mapped[str] = mapped_column(
        "class", String(20), nullable=False, default="lifecycle"
    )
    summary: Mapped[str | None] = mapped_column(Text)
    refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    actor: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class RunGate(Base):
    """A human-in-the-loop review gate opened during a research run."""

    __tablename__ = "run_gates"
    __table_args__ = (
        Index(
            "uq_run_gates_open",
            "stage_run_id",
            unique=True,
            postgresql_where=text("answer IS NULL"),
            sqlite_where=text("answer IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    gate_key: Mapped[str] = mapped_column(String(32), nullable=False)
    stage_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    opened_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    answered_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )


class FrameReview(Base):
    """A request from Popper to review the research frame, and the decision a person gave.

    The request and the decision are files; this row records who decided and when.
    """

    __tablename__ = "frame_reviews"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_frame_reviews_sequence"),
        Index(
            "uq_frame_reviews_pending_run",
            "run_id",
            unique=True,
            postgresql_where=_PENDING_REVIEW,
            sqlite_where=_PENDING_REVIEW,
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    request_storage_key: Mapped[str] = mapped_column(String(400), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    decision_storage_key: Mapped[str | None] = mapped_column(String(400))
    submitted_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT")
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunArtifact(Base):
    """A result file Popper produced for a run. Never changed once received."""

    __tablename__ = "run_artifacts"
    __table_args__ = (
        UniqueConstraint(
            "stage_run_id", "schema", "version", name="uq_run_artifacts_stage_schema_version"
        ),
        CheckConstraint(
            "kind IN ('paper_pdf', 'paper_tex', 'figure', 'results', 'other')",
            name="ck_run_artifacts_kind",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    stage_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id")
    )
    schema_id: Mapped[str | None] = mapped_column("schema", String(160))
    version: Mapped[int | None] = mapped_column(Integer)
    is_primary: Mapped[bool] = mapped_column("primary", Boolean, default=False, nullable=False)
    review_round: Mapped[int | None] = mapped_column(Integer)
    artifact_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, default=dict, nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(400), unique=True, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','ACCEPTED',"
            "'FAILED','STOPPED','SUPERSEDED')",
            name="ck_stage_runs_status",
        ),
        Index("ix_stage_runs_run_iteration_stage", "research_run_id", "iteration", "stage_key"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    research_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="RESTRICT")
    )
    iteration: Mapped[int] = mapped_column(Integer, default=1)
    stage_key: Mapped[str] = mapped_column(String(100))
    engine_key: Mapped[str] = mapped_column(String(100))
    engine_run_id: Mapped[str | None] = mapped_column(String(200))
    prior_stage_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id")
    )
    origin_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("stage_decisions.id", use_alter=True, name="fk_stage_runs_origin_decision"),
    )
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    phase: Mapped[str | None] = mapped_column(String(32))
    step_key: Mapped[str | None] = mapped_column(String(100))
    waiting_input: Mapped[bool] = mapped_column(Boolean, default=False)
    open_gate: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    input_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal("0"))
    last_source_seq: Mapped[int] = mapped_column(Integer, default=0)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    engine_unreachable_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StageDecision(Base):
    __tablename__ = "stage_decisions"
    __table_args__ = (
        CheckConstraint(
            "action IN ('answer_gate','approve','comment','accept','send_back','rerun','stop')",
            name="ck_stage_decisions_action",
        ),
        CheckConstraint("actor IN ('user','system','pi')", name="ck_stage_decisions_actor"),
        CheckConstraint("status IN ('pending','applied')", name="ck_stage_decisions_status"),
        UniqueConstraint("stage_run_id", "gate_id", name="uq_stage_decisions_gate"),
    )
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    stage_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id", ondelete="RESTRICT")
    )
    gate_id: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="applied")
    decided_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


PI_OPEN_STATUSES = ("queued", "sent", "decided", "awaiting_researcher")


class PiConsultation(Base):
    """One question the Platform put to the AI PI, and what came of the answer."""

    __tablename__ = "pi_consultations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('charter','gate_question','gate_review','stage_outcome','failure','close')",
            name="ck_pi_consultations_kind",
        ),
        CheckConstraint("authority IN ('pi','researcher')", name="ck_pi_consultations_authority"),
        CheckConstraint(
            "status IN ('queued','sent','decided','applied','awaiting_researcher',"
            "'endorsed','overridden','stale','failed')",
            name="ck_pi_consultations_status",
        ),
        Index("ix_pi_consultations_run_status", "research_run_id", "status"),
        Index("ix_pi_consultations_stage_kind", "stage_run_id", "kind"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    research_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="RESTRICT"), nullable=False
    )
    stage_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_runs.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    gate_id: Mapped[str | None] = mapped_column(String(200))
    authority: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    # The request without credentials: the model keys are added when it is sent.
    request: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    applied_decision_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("stage_decisions.id", ondelete="SET NULL")
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PiNote(Base):
    """An entry in the research lab notebook the PI reads at every consultation."""

    __tablename__ = "pi_notes"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('decision','lesson','risk','observation','question','directive',"
            "'override','brief')",
            name="ck_pi_notes_kind",
        ),
        CheckConstraint("actor IN ('pi','user')", name="ck_pi_notes_actor"),
        Index("ix_pi_notes_run_created", "research_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    research_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("research_runs.id", ondelete="CASCADE"), nullable=False
    )
    consultation_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("pi_consultations.id", ondelete="SET NULL")
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    stage_key: Mapped[str | None] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(8), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


def active_run_filter():
    return ResearchRun.status.in_(ACTIVE_RUN_STATUSES) | (
        (ResearchRun.status == "budget_exceeded") & ResearchRun.pipeline_key.is_not(None)
    )


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
