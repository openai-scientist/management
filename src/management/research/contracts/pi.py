"""Wire models between the Platform and the PI service (the AI Principal Investigator)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from .base import ContractModel, InteractionMode, is_compatible
from .events import TokenUsage
from .review import Finding, ResearchProfile
from .runs import ArtifactRef, FailureInfo, Gate, ModelConfig

PI_CHARTER = "pi.charter/v1"


class ConsultationKind(StrEnum):
    charter = "charter"
    gate_question = "gate_question"
    gate_review = "gate_review"
    stage_outcome = "stage_outcome"
    failure = "failure"
    close = "close"
    stage_review = "stage_review"


class Authority(StrEnum):
    """`decide`: the Platform applies the PI decision. `advise`: a researcher decides."""

    decide = "decide"
    advise = "advise"


class EscalationReason(StrEnum):
    integrity = "integrity"
    charter_conflict = "charter_conflict"
    low_confidence = "low_confidence"
    out_of_scope = "out_of_scope"
    cannot_decide = "cannot_decide"


class PIAction(StrEnum):
    charter = "charter"
    answer = "answer"
    approve = "approve"
    comment = "comment"
    accept = "accept"
    send_back = "send_back"
    stop = "stop"
    rerun = "rerun"
    report = "report"


class Charter(ContractModel):
    """`pi.charter/v1`: fixed before results exist, in the spirit of a pre-registration."""

    question: str = Field(min_length=1)
    motivation: str = ""
    objectives: list[str] = Field(min_length=1)
    scope_in: list[str] = Field(default_factory=list)
    scope_out: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(min_length=1)
    kill_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risk_posture: Literal["exploratory", "confirmatory"] = "exploratory"
    profile: ResearchProfile = Field(default_factory=ResearchProfile)


class PipelineStageView(ContractModel):
    stage_key: str
    engine: str
    status: str | None = None
    checkpoint: bool = False
    loop_back_to: list[str] = Field(default_factory=list)


class BudgetView(ContractModel):
    limit_usd: Decimal
    spent_usd: Decimal = Decimal("0")
    cost_unknown: bool = False


class Situation(ContractModel):
    stage_key: str | None = None
    stage_run_id: str | None = None
    iteration: int = Field(1, ge=1)
    max_iterations: int = Field(ge=1)
    pipeline: list[PipelineStageView] = Field(min_length=1)
    budget: BudgetView
    #: For kind `failure`: why the stage run failed.
    failure: FailureInfo | None = None


class AllowedAction(ContractModel):
    """One action the PI may take. Constraints apply only to the action they name."""

    action: PIAction
    option_ids: list[str] = Field(default_factory=list)
    selection_key: str | None = None
    targets: list[str] = Field(default_factory=list)


class ConsultArtifact(ArtifactRef):
    role: Literal["primary", "review", "recommendation", "input", "history", "other"]
    stage_key: str | None = None
    #: The stage run that produced it; compare with ``Situation.stage_run_id`` to tell the
    #: current attempt from earlier ones.
    stage_run_id: str | None = None
    size_bytes: int | None = Field(None, ge=0)


class NotebookEntry(ContractModel):
    kind: Literal[
        "decision",
        "lesson",
        "risk",
        "observation",
        "question",
        "directive",
        "override",
        "brief",
        "critique",
    ]
    text: str = Field(min_length=1)
    actor: Literal["pi", "user"]
    stage_key: str | None = None
    at: datetime
    #: ``pi_notes.id``; the Critic cites a directive by it.
    entry_id: str | None = None


class Obligation(ContractModel):
    """A known risk carried to the stage that must control it."""

    obligation_id: str
    source_stage_key: str
    item_id: str | None = None
    finding_id: str | None = None
    text: str = Field(max_length=600)
    target_stage_key: str
    status: Literal["open", "controlled", "unresolved"] = "open"
    #: Set with ``controlled``.
    locator: str | None = None
    #: Set with ``unresolved``.
    note: str | None = None
    #: The consultation id of the Critic that set the status.
    checked_by: str | None = None


class CriticPoint(ContractModel):
    point_id: str
    severity: Literal["serious", "minor"]
    lens: Literal["charter", "risk", "directive", "integrity"]
    #: ``charter:<field>[i]`` | ``risk:<obligation_id>`` | ``directive:<entry_id>`` |
    #: ``integrity:<tag>``
    ref: str
    locator: str | None = None
    point: str
    suggestion: str | None = None


class ObligationCheck(ContractModel):
    obligation_id: str
    status: Literal["controlled", "unresolved"]
    locator: str | None = None
    note: str | None = None


class CriticReport(ContractModel):
    """The independent stage-end review. The PI answers its serious points."""

    verdict: Literal["sound", "concerns", "serious"]
    summary: str
    points: list[CriticPoint] = Field(default_factory=list)
    obligations: list[ObligationCheck] = Field(default_factory=list)
    #: ``provider/model`` the Critic ran with.
    model: str | None = None
    model_shared_with: Literal["lead"] | None = None


class CriticResponse(ContractModel):
    point_id: str
    response: Literal["accepted", "rejected"]
    reason: str


class DirectiveApplied(ContractModel):
    entry_id: str
    how: str


class PIModels(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    lead: ModelConfig
    critic: ModelConfig


class ConsultationRequest(ContractModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    contract_version: str
    consultation_id: str
    platform_run_id: str
    kind: ConsultationKind
    interaction_mode: InteractionMode
    authority: Authority
    topic: str
    charter: Charter | None = None
    profile: ResearchProfile = Field(default_factory=ResearchProfile)
    situation: Situation
    gate: Gate | None = None
    allowed: list[AllowedAction] = Field(default_factory=list)
    artifacts: list[ConsultArtifact] = Field(default_factory=list)
    notebook: list[NotebookEntry] = Field(default_factory=list)
    obligations: list[Obligation] = Field(default_factory=list)
    #: For kind `stage_outcome`: the Critic report, or none when it was unavailable.
    critique: CriticReport | None = None
    models: PIModels
    callback_url: str

    @field_validator("contract_version")
    @classmethod
    def validate_version(cls, value: str) -> str:
        if not is_compatible(value):
            raise ValueError("incompatible contract_version")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.kind in {ConsultationKind.gate_question, ConsultationKind.gate_review}:
            if self.gate is None:
                raise ValueError("gate kinds require gate")
        if self.kind != ConsultationKind.charter and self.charter is None:
            raise ValueError("every kind except charter requires the charter")
        return self

    def to_wire(self) -> dict[str, Any]:
        """Reveal credentials only for an outgoing PI HTTP request."""
        data = self.model_dump(mode="json", by_alias=True)
        for role in ("lead", "critic"):
            key = getattr(self.models, role).api_key
            data["models"][role]["api_key"] = key.get_secret_value() if key else None
        return data


class ConsultationAccepted(ContractModel):
    consultation_id: str
    created: bool


class PIDecision(ContractModel):
    action: PIAction
    option_id: str | None = None
    note: str | None = None
    findings: list[Finding] = Field(default_factory=list)
    selection: dict[str, list[str]] = Field(default_factory=dict)
    target_stage_key: str | None = None
    charter: Charter | None = None
    #: Gate answer parameters, sent as ``GateAnswer.params`` (for example ``dropped`` ids).
    params: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.action == PIAction.answer and not self.option_id:
            raise ValueError("answer requires option_id")
        if self.action == PIAction.comment and not self.findings:
            raise ValueError("comment requires findings")
        if self.action == PIAction.send_back and not self.target_stage_key:
            raise ValueError("send_back requires target_stage_key")
        if self.action == PIAction.charter and self.charter is None:
            raise ValueError("charter requires charter")
        return self


class ConsideredOption(ContractModel):
    action: str
    summary: str
    why_not: str | None = None


class EvidencePoint(ContractModel):
    ref: str
    point: str


class Critique(ContractModel):
    verdict: Literal["upheld", "revised"]
    points: list[str] = Field(default_factory=list)


class Escalation(ContractModel):
    reason_code: EscalationReason
    message: str = Field(min_length=1)


class NoteDraft(ContractModel):
    kind: Literal["lesson", "risk", "observation", "question"]
    text: str = Field(min_length=1)


class TraceStep(ContractModel):
    """One step of the PI agent loop: what it looked at or did, never full content."""

    actor: Literal["pi", "critic"]
    tool: str
    summary: str


class ConsultationResult(ContractModel):
    consultation_id: str
    status: Literal["decided", "failed"]
    failure: FailureInfo | None = None
    decision: PIDecision | None = None
    rationale: str = ""
    confidence: float | None = Field(None, ge=0, le=1)
    options_considered: list[ConsideredOption] = Field(default_factory=list)
    evidence: list[EvidencePoint] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    would_change_my_mind: list[str] = Field(default_factory=list)
    critique: Critique | None = None
    escalate: Escalation | None = None
    handoff: list[str] = Field(default_factory=list)
    notebook: list[NoteDraft] = Field(default_factory=list)
    brief: str = ""
    #: For kind `stage_review`.
    report: CriticReport | None = None
    critic_response: list[CriticResponse] = Field(default_factory=list)
    directives_applied: list[DirectiveApplied] = Field(default_factory=list)
    usage: dict[Literal["lead", "critic"], TokenUsage] = Field(default_factory=dict)
    trace: list[TraceStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.status == "decided") != (self.decision is not None):
            raise ValueError("decision is required exactly when status is decided")
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("failure is required exactly when status is failed")
        return self


class ConsultationState(ContractModel):
    consultation_id: str
    status: Literal["running", "decided", "failed"]
    result: ConsultationResult | None = None


class PIHealth(ContractModel):
    status: Literal["ok", "degraded"]
    service_version: str
    contract_version: str
    active_consultations: int = Field(0, ge=0)


class PICapabilities(ContractModel):
    service_key: Literal["pi"] = "pi"
    service_version: str
    contract_version: str
    kinds: list[ConsultationKind] = Field(min_length=1)
    providers: list[Literal["openai_compatible", "bedrock"]] = Field(
        default_factory=lambda: ["openai_compatible"]
    )
