from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from management.core.responses import ErrorResponse
from management.research.contracts import InteractionMode

RunStatus = Literal[
    "queued",
    "running",
    "paused",
    "awaiting_review",
    "completed",
    "budget_exceeded",
    "failed",
    "stopped",
]


RUN_ERRORS = {
    403: {"model": ErrorResponse, "description": "Project Manager or Researcher role is required"},
    404: {"model": ErrorResponse, "description": "The project or run was not found"},
    409: {"model": ErrorResponse, "description": "The project is archived or already has a run"},
    502: {"model": ErrorResponse, "description": "Popper could not be reached"},
    503: {"model": ErrorResponse, "description": "Popper is not configured on this server"},
}


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(
        min_length=12,
        max_length=2000,
        description="Core research question or topic",
    )
    domains: list[str] = Field(
        default_factory=list,
        description="List of academic/scientific domains",
    )
    review_mode: Literal["copilot", "auto"] = Field(
        default="copilot",
        description="copilot requires review at gates; auto proceeds autonomously",
    )
    interaction_mode: InteractionMode | None = Field(
        default=None,
        description="Pipeline interaction mode; overrides review_mode when engines are configured",
    )
    budget_usd: Decimal | None = Field(
        default=None,
        max_digits=10,
        decimal_places=2,
        description="Spending cap for this run. Defaults to the server's standard budget.",
    )


class CurrentStage(BaseModel):
    stage_run_id: str
    stage_key: str
    engine_key: str
    status: str
    phase: str | None = None
    step_key: str | None = None
    waiting_input: bool = False
    open_gate: dict[str, Any] | None = None
    cost_usd: Decimal


class RunItem(BaseModel):
    id: str
    project_id: str
    label: str
    topic: str | None = None
    domains: list[str] = Field(default_factory=list)
    review_mode: str | None = None
    pipeline_key: str | None = None
    iteration: int = 1
    interaction_mode: str | None = None
    current_stage: CurrentStage | None = None
    last_seq: int = 0
    last_source_seq: int = 0
    dataset_id: str | None = None
    dataset_version_id: str | None = None
    dataset_version_number: int | None = None
    research_context_id: str | None = None
    research_context_version: int | None = None
    created_by_user_id: str
    status: RunStatus
    auto_review: bool
    budget_usd: Decimal
    cost_usd: Decimal
    failure_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class RunEventItem(BaseModel):
    seq: int
    run_id: str
    ts: datetime
    type: str
    stage_key: str | None = None
    actor: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class GateAnswer(BaseModel):
    option_id: str = Field(min_length=1, max_length=64)
    dropped: list[str] = Field(default_factory=list)
    # Held-back hypotheses the reviewer keeps despite their objection (hypotheses gate only).
    kept: list[str] = Field(default_factory=list)
    note: str | None = Field(default=None, max_length=2000)
