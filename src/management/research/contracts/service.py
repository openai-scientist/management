from datetime import datetime
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from .base import ContractModel, InteractionMode, ModelRole, Phase, parse_schema_id
from .review import ResearchProfile


class StepInfo(ContractModel):
    step_key: str
    label: str
    order: int = Field(ge=1)


class OutputDecl(ContractModel):
    schema_id: str = Field(alias="schema")
    primary: bool = False


class Capabilities(ContractModel):
    engine_key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    engine_version: str
    contract_version: str
    stage_keys: list[str] = Field(min_length=1)
    input_schemas: list[str] = Field(default_factory=list)
    output_schemas: list[OutputDecl]
    steps: list[StepInfo]
    question_gates: dict[InteractionMode, list[str]] = Field(default_factory=dict)
    activity_types: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)
    optional_integrations: list[str] = Field(default_factory=list)
    providers: list[Literal["openai_compatible", "bedrock"]] = Field(
        default_factory=lambda: ["openai_compatible"]
    )
    auto_revision: bool
    feedback_schemas: list[str] = Field(default_factory=list)
    reuses_prior_run: bool = False
    profiles: list[ResearchProfile]

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if not any(profile.is_base for profile in self.profiles):
            raise ValueError("profiles must include base")
        if sum(output.primary for output in self.output_schemas) != 1:
            raise ValueError("output_schemas must contain exactly one primary")
        for schema in [
            *self.input_schemas,
            *self.feedback_schemas,
            *(output.schema_id for output in self.output_schemas),
        ]:
            parse_schema_id(schema)
        if any(not activity.startswith(self.engine_key + ".") for activity in self.activity_types):
            raise ValueError("activity_types must use engine_key prefix")
        if set(self.integrations) & set(self.optional_integrations):
            raise ValueError("integrations and optional_integrations must not overlap")
        return self

    def supports(self, profile: ResearchProfile) -> bool:
        return True

    def exact_support(self, profile: ResearchProfile) -> bool:
        return profile in self.profiles


class Health(ContractModel):
    status: Literal["ok", "degraded", "down"]
    engine_version: str
    contract_version: str
    active_runs: int = Field(ge=0)
    checks: dict[str, bool] | None = None
    last_error: str | None = None


class LlmCall(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    role: ModelRole
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int = Field(ge=0)
    outcome: Literal["ok", "error", "timeout"]


class TelemetryRecord(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    occurred_at: datetime
    level: Literal["DEBUG", "INFO", "WARN", "ERROR"]
    message: str = Field(max_length=500)
    platform_run_id: str
    stage_run_id: str
    engine_run_id: str
    phase: Phase | None = None
    step_key: str | None = None
    llm: LlmCall | None = None


class TelemetryBatch(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    records: list[TelemetryRecord] = Field(min_length=1, max_length=500)


class ArtifactMeta(ContractModel):
    schema_id: str = Field(alias="schema")
    version: int = Field(ge=1)
    primary: bool = False
    filename: str
    media_type: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    review_round: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
