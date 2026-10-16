"""The latest `review.findings/v1` of a stage run, read for the PI rules."""

from pydantic import ValidationError
from sqlalchemy import select

from management.research.contracts import REVIEW_FINDINGS, ReviewFindings
from management.research.models import RunArtifact


class ReviewUnreadable(Exception):
    """The stored review cannot be read or parsed."""


def hypothesis_id(locator: str) -> str | None:
    if not locator.startswith("hypothesis:"):
        return None
    return locator.removeprefix("hypothesis:").split("/", 1)[0]


async def latest_review(db, app_state, stage_run) -> ReviewFindings | None:
    """None when the stage run has no review. Raises `ReviewUnreadable` when it cannot be read."""
    artifact = await db.scalar(
        select(RunArtifact)
        .where(RunArtifact.stage_run_id == stage_run.id, RunArtifact.schema_id == REVIEW_FINDINGS)
        .order_by(RunArtifact.version.desc())
        .limit(1)
    )
    if artifact is None:
        return None
    try:
        body = b"".join([chunk async for chunk in app_state.file_store.open(artifact.storage_key)])
        return ReviewFindings.model_validate_json(body)
    except (OSError, ValueError, ValidationError):
        raise ReviewUnreadable from None
