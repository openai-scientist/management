from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, model_validator

from .base import ContractModel, FindingKind, Severity


class Anchor(ContractModel):
    artifact_id: str | None = None
    locator: str = Field(min_length=1)


class FindingCategory(StrEnum):
    leakage = "leakage"
    baseline = "baseline"
    control = "control"
    statistics = "statistics"
    reproducibility = "reproducibility"
    evidence = "evidence"
    other = "other"


class Finding(ContractModel):
    finding_id: str
    anchor: Anchor
    severity: Severity
    kind: FindingKind = FindingKind.fix
    category: FindingCategory = FindingCategory.other
    issue: str = Field(min_length=1)
    suggestion: str | None = None
    author: Literal["reviewer", "researcher"]
    evidence_refs: list[str] = Field(default_factory=list)


class ReviewFindings(ContractModel):
    round: int = Field(ge=1)
    reviewed_artifact_id: str
    reviewer_model: str
    findings: list[Finding]
    passed: bool

    @model_validator(mode="after")
    def validate_passed(self) -> Self:
        if self.passed != (not any(f.severity == Severity.blocking for f in self.findings)):
            raise ValueError("passed must match absence of blocking findings")
        return self


class RevisionItem(ContractModel):
    finding_id: str
    outcome: Literal["fixed", "rebutted", "not_fixed"]
    note: str | None = None

    @model_validator(mode="after")
    def validate_note(self) -> Self:
        if self.outcome != "fixed" and not (self.note and self.note.strip()):
            raise ValueError("rebutted and not_fixed require a note")
        return self


class RevisionResponse(ContractModel):
    round: int = Field(ge=1)
    artifact_before: str
    artifact_after: str
    items: list[RevisionItem]


class RevisionDiff(ContractModel):
    round: int = Field(ge=1)
    artifact_before: str
    artifact_after: str
    format: Literal["unified"] = "unified"
    patch: str


class DatasetDescriptor(ContractModel):
    dataset_version_id: str
    version_number: int = Field(ge=1)
    original_filename: str
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    row_count: int | None = None
    source_type: str
    download_url: str


class ResearchProfile(ContractModel):
    domain: str | None = None
    research_type: str | None = None

    @property
    def is_base(self) -> bool:
        return self.domain is None and self.research_type is None


class PipelineRecommendation(ContractModel):
    action: Literal["proceed", "refine", "pivot"]
    target_stage_key: str | None = None
    selection: dict[str, list[str]] = Field(default_factory=dict)
    profile: ResearchProfile | None = None
    outcome: Literal["positive", "negative", "inconclusive"] | None = None
    reason: str
    evidence_artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.action != "proceed" and not self.target_stage_key:
            raise ValueError("refine and pivot require target_stage_key")
        return self


class PipelineFeedback(ContractModel):
    source_stage_key: str
    note: str | None = None
    artifact_ids: list[str] = Field(default_factory=list)
