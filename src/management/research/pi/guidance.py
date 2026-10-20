"""`inputs.params.guidance`: the research lead brief for an engine, rebuilt at each stage start."""

from sqlalchemy import select

from management.research.models import PiConsultation, PiNote
from management.research.pi.obligations import guidance_line, targeted_at

MAX_LINES = 20
MAX_CHARS = 500
HANDOFF_STATUSES = ("decided", "applied", "endorsed")


def _clip(prefix: str, text: str) -> str:
    return f"{prefix}{text}".strip()[:MAX_CHARS]


async def build_guidance(db, run, stage_key) -> list[str]:
    charter = run.charter or {}
    lines: list[str] = []
    # Most important first: the lines past MAX_LINES are dropped from the end.
    if charter.get("question"):
        lines.append(_clip("Research question: ", charter["question"]))
    if run.charter and run.topic:
        lines.append(_clip("Topic given by the researcher: ", run.topic))
    lines += [
        _clip("Success criterion: ", item) for item in charter.get("success_criteria", [])[:5]
    ]
    directives = list(
        await db.scalars(
            select(PiNote.text)
            .where(PiNote.research_run_id == run.id, PiNote.kind == "directive")
            .order_by(PiNote.created_at.desc())
            .limit(5)
        )
    )
    lines += [_clip("Directive: ", text) for text in reversed(directives)]
    # Newest first: an older hand-off is not guidance any more.
    results = await db.scalars(
        select(PiConsultation.result)
        .where(
            PiConsultation.research_run_id == run.id,
            PiConsultation.status.in_(HANDOFF_STATUSES),
            PiConsultation.result.is_not(None),
        )
        .order_by(PiConsultation.decided_at.desc())
    )
    for result in results:
        handoff = result.get("handoff") or []
        if handoff:
            lines += [_clip("Handoff: ", text) for text in handoff[:5]]
            break
    lines += [_clip("Objective: ", item) for item in charter.get("objectives", [])[:4]]
    # An obligation is never dropped: its lines come after the capped ones.
    risks = [
        guidance_line(item) for item in targeted_at(run, stage_key) if item.status != "controlled"
    ]
    return lines[:MAX_LINES] + risks


def with_guidance(inputs, lines: list[str]):
    """Replace, never append: a send-back copies the earlier stage's params."""
    params = {key: value for key, value in inputs.params.items() if key != "guidance"}
    if lines:
        params["guidance"] = lines
    return inputs.model_copy(update={"params": params})
