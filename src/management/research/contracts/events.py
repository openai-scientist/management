import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator

from .base import ContractModel, EventClass, ModelRole, Phase, parse_schema_id

LIFECYCLE_TYPES = frozenset(
    {
        "run.phase_changed",
        "run.step_changed",
        "run.gate_opened",
        "run.gate_closed",
        "review.completed",
        "artifact.delivered",
        "run.published",
        "run.failed",
        "run.cancelled",
        "run.cost",
    }
)


class TokenUsage(ContractModel):
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)


class RunCost(ContractModel):
    cost_usd: Decimal | None = None
    usage: dict[ModelRole, TokenUsage] = Field(default_factory=dict)


class PhaseChanged(ContractModel):
    from_phase: Phase | None = Field(alias="from")


class StepChanged(ContractModel):
    from_step: str | None = Field(alias="from")


class GateClosed(ContractModel):
    gate_id: str
    action: Literal["answer", "approve", "comment"]
    actor: Literal["user", "system"]


class ReviewCompleted(ContractModel):
    round: int = Field(ge=1)
    passed: bool
    artifact_id: str | None
    error: str | None = None


class ArtifactDelivered(ContractModel):
    artifact_id: str
    schema_id: str = Field(alias="schema")
    version: int = Field(ge=1)
    primary: bool

    @field_validator("schema_id")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        parse_schema_id(value)
        return value


class RunPublished(ContractModel):
    artifact_id: str


class EmptyPayload(ContractModel):
    pass


_PAYLOAD_MODELS = {
    "run.phase_changed": PhaseChanged,
    "run.step_changed": StepChanged,
    "run.gate_closed": GateClosed,
    "review.completed": ReviewCompleted,
    "artifact.delivered": ArtifactDelivered,
    "run.published": RunPublished,
    "run.cancelled": EmptyPayload,
    "run.cost": RunCost,
}


class EventEnvelope(ContractModel):
    source_seq: int = Field(ge=1)
    type: str
    event_class: EventClass = Field(alias="class")
    phase: Phase | None = None
    step_key: str | None = None
    actor: str | None = None
    summary: str | None = None
    refs: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.event_class == EventClass.activity:
            if self.type in LIFECYCLE_TYPES or not re.fullmatch(
                r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*", self.type
            ):
                raise ValueError("activity type requires an engine prefix")
            if not all(v and v.strip() for v in (self.step_key, self.actor, self.summary)):
                raise ValueError("activity requires step_key, actor and summary")
            return self
        if self.type not in LIFECYCLE_TYPES:
            raise ValueError("unknown lifecycle type")
        if self.type in {"run.phase_changed", "run.gate_opened"} and self.phase is None:
            raise ValueError("phase is required")
        if self.type == "run.step_changed" and not self.step_key:
            raise ValueError("step_key is required")
        terminal = {
            "run.published": Phase.published,
            "run.failed": Phase.failed,
            "run.cancelled": Phase.cancelled,
        }
        if self.type in terminal and self.phase != terminal[self.type]:
            raise ValueError("terminal event phase does not match type")
        payload_model = _PAYLOAD_MODELS.get(self.type)
        if self.type in {"run.gate_opened", "run.failed"}:
            from .runs import FailureInfo, Gate

            payload_model = Gate if self.type == "run.gate_opened" else FailureInfo
        if payload_model:
            payload_model.model_validate(self.payload)
        return self

    def engine_prefix(self) -> str | None:
        return self.type.split(".", 1)[0] if self.event_class == EventClass.activity else None


class EventBatch(ContractModel):
    events: list[EventEnvelope] = Field(min_length=1)


class EventPage(ContractModel):
    events: list[EventEnvelope]
