from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from .base import (
    ContractModel,
    GateKind,
    InteractionMode,
    Phase,
    is_compatible,
    parse_schema_id,
)
from .review import Finding, PipelineFeedback, ResearchProfile


class ModelConfig(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    provider: Literal["openai_compatible", "bedrock"]
    base_url: str | None = None
    model: str = Field(min_length=1)
    api_key: SecretStr | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def identity(self) -> tuple[str, str]:
        return self.provider, self.model


class Models(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    stage: ModelConfig
    review: ModelConfig

    @model_validator(mode="after")
    def validate_models(self) -> Self:
        if self.stage.identity == self.review.identity:
            raise ValueError("review model must differ from stage model")
        return self


class ArtifactRef(ContractModel):
    artifact_id: str
    schema_id: str = Field(alias="schema")
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    download_url: str
    primary: bool = False

    @field_validator("schema_id")
    @classmethod
    def validate_schema(cls, value: str) -> str:
        parse_schema_id(value)
        return value


class Decisions(ContractModel):
    selection: dict[str, list[str]] = Field(default_factory=dict)
    note: str | None = None
    actor: Literal["user", "system"]
    decided_by: str | None = None


class Inputs(ContractModel):
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    decisions: Decisions | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    feedback: list[PipelineFeedback] = Field(default_factory=list)
    prior_engine_run_id: str | None = None
    history: list[ArtifactRef] = Field(default_factory=list)


class ReviewPolicy(ContractModel):
    max_auto_revisions: int = Field(2, ge=0)


class RunCreateRequest(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    contract_version: str
    platform_run_id: str
    stage_run_id: str
    stage_key: str
    iteration: int = Field(1, ge=1)
    interaction_mode: InteractionMode
    profile: ResearchProfile = Field(default_factory=ResearchProfile)
    review_policy: ReviewPolicy = Field(default_factory=ReviewPolicy)
    inputs: Inputs = Field(default_factory=Inputs)
    models: Models
    integrations: dict[str, SecretStr] = Field(default_factory=dict)
    budget_usd: Decimal = Field(gt=0)
    callback_url: str

    @field_validator("contract_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not is_compatible(value):
            raise ValueError("incompatible contract_version")
        return value

    def to_wire(self) -> dict[str, Any]:
        """Reveal credentials only for an outgoing engine HTTP request."""
        data = self.model_dump(mode="json", by_alias=True)
        for role in ("stage", "review"):
            key = getattr(self.models, role).api_key
            data["models"][role]["api_key"] = key.get_secret_value() if key else None
        data["integrations"] = {
            name: key.get_secret_value() for name, key in self.integrations.items()
        }
        return data


class RunCreateResponse(ContractModel):
    engine_run_id: str
    phase: Phase
    created: bool


class GateOption(ContractModel):
    option_id: str
    label: str


class Gate(ContractModel):
    gate_id: str
    kind: GateKind
    step_key: str | None
    prompt: str
    options: list[GateOption] = Field(default_factory=list)
    default_option_id: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
    opened_at: datetime

    @model_validator(mode="after")
    def validate_gate(self) -> Self:
        if self.kind == GateKind.question:
            ids = [option.option_id for option in self.options]
            if not 2 <= len(ids) <= 4 or self.default_option_id not in ids:
                raise ValueError("question requires 2–4 options and a default in those options")
        elif self.options or not self.artifact_ids:
            raise ValueError("artifact_review requires artifact_ids and no options")
        return self


class GateAnswer(ContractModel):
    action: Literal["answer", "approve", "comment"]
    option_id: str | None = None
    text: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    params: dict[str, Any] = Field(default_factory=dict)
    actor: Literal["user", "system"]
    decided_by: str | None = None

    @model_validator(mode="after")
    def validate_answer(self) -> Self:
        if self.action == "answer":
            if not self.option_id or self.findings:
                raise ValueError("answer requires option_id and no findings")
        elif self.action == "approve":
            if self.option_id is not None or self.findings:
                raise ValueError("approve forbids option_id and findings")
        elif (
            self.option_id is not None
            or self.params
            or not self.findings
            or any(f.author != "researcher" for f in self.findings)
        ):
            raise ValueError("comment requires researcher findings and forbids option_id/params")
        return self


class FailureInfo(ContractModel):
    code: str
    message: str


class RunState(ContractModel):
    engine_run_id: str
    stage_run_id: str
    phase: Phase
    step_key: str | None
    waiting_input: bool
    open_gate: Gate | None
    cost_usd: Decimal = Decimal("0")
    last_source_seq: int = Field(0, ge=0)
    failure: FailureInfo | None = None

    @model_validator(mode="after")
    def validate_failure(self) -> Self:
        if (self.phase == Phase.failed) != (self.failure is not None):
            raise ValueError("failure is required exactly when phase is failed")
        return self
