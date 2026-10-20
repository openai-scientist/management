import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

CONTRACT_VERSION = "1.4.0"


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class Phase(StrEnum):
    drafting = "drafting"
    reviewing = "reviewing"
    revising = "revising"
    awaiting_researcher = "awaiting_researcher"
    published = "published"
    failed = "failed"
    cancelled = "cancelled"
    paused = "paused"


TERMINAL_PHASES = frozenset({Phase.published, Phase.failed, Phase.cancelled})


class InteractionMode(StrEnum):
    auto = "auto"
    light = "light"
    copilot = "copilot"
    full = "full"


class Severity(StrEnum):
    blocking = "blocking"
    major = "major"
    minor = "minor"


class FindingKind(StrEnum):
    fix = "fix"
    needs_evidence = "needs_evidence"


class GateKind(StrEnum):
    question = "question"
    artifact_review = "artifact_review"


class EventClass(StrEnum):
    lifecycle = "lifecycle"
    activity = "activity"


class ModelRole(StrEnum):
    stage = "stage"
    review = "review"


def is_compatible(version: str) -> bool:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*))?", version)
    return bool(match and match[1] == CONTRACT_VERSION.split(".")[0])


def parse_schema_id(schema_id: str) -> tuple[str, int]:
    match = re.fullmatch(r"([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*)/v([1-9][0-9]*)", schema_id)
    if not match:
        raise ValueError("invalid schema id; expected name/v<positive major>")
    return match[1], int(match[2])


PLATFORM_DATASET = "platform.dataset/v1"
EXD_RESOURCES = "exd.resources/v1"
EXD_EXPERIMENT_INDEX = "exd.experiment_index/v1"
REVIEW_FINDINGS = "review.findings/v1"
REVISION_RESPONSE = "revision.response/v1"
REVISION_DIFF = "revision.diff/v1"
PIPELINE_FEEDBACK = "pipeline.feedback/v1"
PIPELINE_RECOMMENDATION = "pipeline.recommendation/v1"
I2H_HYPOTHESES = "i2h.hypotheses/v1"
EXD_EXP_PLAN = "exd.exp_plan/v1"
SELECTION_HYPOTHESIS_IDS = "hypothesis_ids"
FAILURE_ENGINE_RESTARTED = "engine_restarted"
