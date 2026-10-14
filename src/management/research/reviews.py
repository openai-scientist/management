"""Researcher input validation and an index of stored review artifacts."""

from uuid import UUID

from sqlalchemy import select

from management.core.errors import APIError
from management.research.models import RunArtifact
from management.research.schemas.reviews import ReviewArtifactItem, ReviewRoundItem

REVIEW_SCHEMAS = {"review.findings/v1", "revision.response/v1", "revision.diff/v1"}


async def normalize_comment(db, stage, payload):
    if payload["action"] != "comment":
        return payload
    findings = [finding | {"author": "researcher"} for finding in payload["findings"]]
    try:
        ids = {UUID(finding["anchor"]["artifact_id"]) for finding in findings}
    except (ValueError, TypeError, AttributeError):
        raise APIError(
            422, "VALIDATION_ERROR", "Comment must refer to this stage's artifacts"
        ) from None
    owned = set(
        await db.scalars(
            select(RunArtifact.id).where(
                RunArtifact.stage_run_id == stage.id, RunArtifact.id.in_(ids)
            )
        )
    )
    if ids != owned:
        raise APIError(422, "VALIDATION_ERROR", "Comment must refer to this stage's artifacts")
    return payload | {"findings": findings}


def validate_gate_action(gate, answer):
    if gate["kind"] == "question":
        if answer.action != "answer":
            raise APIError(422, "GATE_ACTION_INVALID", "Question gate requires an answer")
        if answer.option_id not in {option["option_id"] for option in gate["options"]}:
            raise APIError(422, "GATE_OPTION_INVALID", "Choose one of the gate's options")
    elif answer.action == "answer":
        raise APIError(422, "GATE_ACTION_INVALID", "Review gate requires approve or comment")


async def review_history(db, stage):
    rows = await db.scalars(
        select(RunArtifact)
        .where(
            RunArtifact.stage_run_id == stage.id,
            RunArtifact.schema_id.in_(REVIEW_SCHEMAS),
            RunArtifact.review_round.is_not(None),
        )
        .order_by(RunArtifact.review_round, RunArtifact.schema_id, RunArtifact.version)
    )
    rounds = {}
    for artifact in rows:
        rounds.setdefault(artifact.review_round, []).append(
            ReviewArtifactItem(
                artifact_id=str(artifact.id), schema_id=artifact.schema_id, version=artifact.version
            )
        )
    return [
        ReviewRoundItem(review_round=number, artifacts=items) for number, items in rounds.items()
    ]
