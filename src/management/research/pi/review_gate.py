"""The review-gate rule: the Platform answers an engine `artifact_review` gate by code.

In auto and light a gate with no blocking finding is approved. A gate with blocking findings
the rule has not sent yet is answered with a comment of exactly those findings, at most
`MAX_RULE_REVISIONS` times per stage run. Otherwise the gate is approved and the findings that
still block are kept in the stage run (`open_blocking`) for the stage outcome. The rule runs after
the engine's event batch is committed, because answering a gate calls the engine back.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from management.core.errors import APIError
from management.research.contracts import Finding, GateAnswer, ReviewFindings
from management.research.models import ResearchRun, StageDecision, StageRun
from management.research.orchestrator import _relock, answer_stage_gate
from management.research.pi.authority import review_gate_applies
from management.research.pi.findings import ReviewUnreadable, latest_review
from management.research.pi.service import emit_attention, pi_active
from management.research.reviews import normalize_comment

logger = logging.getLogger(__name__)

MAX_RULE_REVISIONS = 2
POLICY = "review_gate"
DECIDED_BY = "policy:review_gate"
#: A pending answer older than this was lost (a crash between the commit and the engine call).
STALE_PENDING = timedelta(minutes=10)


def blocking_findings(review: ReviewFindings | None) -> list[Finding]:
    return [f for f in review.findings if f.severity == "blocking"] if review else []


def comment_findings(review: ReviewFindings, sent: set[str], comments: int) -> list[Finding] | None:
    """The blocking findings to send back as a comment, or None to approve."""
    blocking = blocking_findings(review)
    ids = {f.finding_id for f in blocking}
    if not blocking or ids <= sent or comments >= MAX_RULE_REVISIONS:
        return None
    return blocking


def _comment(review: ReviewFindings, findings: list[Finding], artifact_id: str | None) -> dict:
    """The comment findings: copies of the blocking ones, anchored in the artifact under review."""
    return {
        "action": "comment",
        "text": None,
        "findings": [
            finding.model_dump(mode="json")
            | {
                "author": "researcher",
                "issue": f"[{finding.finding_id}] {finding.issue}",
                "anchor": {
                    "artifact_id": artifact_id or review.reviewed_artifact_id,
                    "locator": finding.anchor.locator,
                },
            }
            for finding in findings
        ],
    }


async def answer_review_gates(app_state, run_id) -> None:
    """Answer every unanswered review gate of the run that the rule owns."""
    async with app_state.session_factory() as db:
        app_state.notification_hub.bind(db)
        app_state.run_event_hub.bind(db)
        run = await db.get(ResearchRun, run_id)
        if run is None or not pi_active(app_state, run):
            return
        stage_ids = list(
            await db.scalars(
                select(StageRun.id).where(
                    StageRun.research_run_id == run_id, StageRun.status == "RUNNING"
                )
            )
        )
        for stage_id in stage_ids:
            await _answer(db, app_state, run_id, stage_id)


async def answer_review_gates_safely(app_state, run_id) -> None:
    """The background entry point: a failure hands the open gates to the researcher."""
    try:
        await answer_review_gates(app_state, run_id)
    except Exception as exc:  # noqa: BLE001 - a background task must not strand an open gate
        logger.warning("Review gate rule failed for run %s: %s", run_id, exc)
        try:
            await _all_to_researcher(app_state, run_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not hand the review gates of run %s to the researcher", run_id)


async def _all_to_researcher(app_state, run_id) -> None:
    async with app_state.session_factory() as db:
        app_state.notification_hub.bind(db)
        app_state.run_event_hub.bind(db)
        run = await db.get(ResearchRun, run_id)
        if run is None:
            return
        stages = await db.scalars(
            select(StageRun).where(StageRun.research_run_id == run_id, StageRun.status == "RUNNING")
        )
        for stage_run in list(stages):
            gate = stage_run.open_gate
            if (
                gate
                and review_gate_applies(run, gate["kind"])
                and stage_run.config_snapshot.get("review_gate_failed") != gate["gate_id"]
            ):
                await _to_researcher(
                    db, run_id, stage_run.id, gate["gate_id"], "review_gate_failed", "engine_error"
                )


async def _pending_gate(db, run, stage_run) -> str | None:
    gate = stage_run.open_gate
    if not gate or not review_gate_applies(run, gate["kind"]):
        return None
    gate_id = gate["gate_id"]
    if stage_run.config_snapshot.get("review_gate_failed") == gate_id:
        return None
    answered = await db.scalar(
        select(StageDecision.id).where(
            StageDecision.stage_run_id == stage_run.id,
            StageDecision.gate_id == gate_id,
            ~(
                (StageDecision.status == "pending")
                & (StageDecision.decided_at < datetime.now(UTC) - STALE_PENDING)
            ),
        )
    )
    return None if answered else gate_id


async def _answer(db, app_state, run_id, stage_id) -> None:
    run = await _relock(db, ResearchRun, run_id)
    stage_run = await _relock(db, StageRun, stage_id)
    # Before the engine's create call returns, the gate cannot be answered: a later flush does it.
    ready = stage_run.status == "RUNNING" and stage_run.engine_run_id
    gate_id = await _pending_gate(db, run, stage_run) if ready else None
    if gate_id is None:
        await db.rollback()
        return
    try:
        review = await latest_review(db, app_state, stage_run)
    except ReviewUnreadable:
        await _to_researcher(db, run_id, stage_id, gate_id, "review_unreadable", None)
        return
    state = stage_run.config_snapshot.get("review_gate") or {}
    sent, comments = set(state.get("sent", [])), state.get("comments", 0)
    findings = comment_findings(review, sent, comments) if review else None
    # The counters change only after the engine accepted the answer.
    recorded = {}
    if findings is None:
        answer = GateAnswer(action="approve", actor="system")
        ids = sorted(f.finding_id for f in blocking_findings(review))
        if ids:
            recorded["open_blocking"] = ids
    else:
        ids = sorted(f.finding_id for f in findings)
        reviewed = (stage_run.open_gate.get("artifact_ids") or [None])[0]
        payload = await normalize_comment(db, stage_run, _comment(review, findings, reviewed))
        answer = GateAnswer(**payload, actor="system")
        recorded["review_gate"] = {"sent": sorted(sent | set(ids)), "comments": comments + 1}
    try:
        await answer_stage_gate(
            db,
            app_state,
            stage_run,
            answer,
            actor="system",
            user_id=None,
            decided_by=DECIDED_BY,
            gate_id=gate_id,
            event_extra={"policy": POLICY, "finding_ids": ids},
        )
        stage_run.config_snapshot = stage_run.config_snapshot | recorded
        await db.commit()
    except APIError as exc:
        await db.rollback()
        if exc.code != "GATE_NOT_OPEN":  # Someone else answered it: nothing is left to do.
            await _to_researcher(db, run_id, stage_id, gate_id, "review_gate_failed", exc.code)
    except Exception as exc:  # noqa: BLE001 - an engine error must not strand an open gate
        await db.rollback()
        logger.warning("Review gate rule failed: %s", exc)
        await _to_researcher(db, run_id, stage_id, gate_id, "review_gate_failed", "engine_error")


async def _to_researcher(db, run_id, stage_id, gate_id, reason, code) -> None:
    """The rule could not answer: drop what it recorded and tell the researcher."""
    run = await _relock(db, ResearchRun, run_id)
    stage_run = await _relock(db, StageRun, stage_id)
    pending = await db.scalar(
        select(StageDecision).where(
            StageDecision.stage_run_id == stage_run.id,
            StageDecision.gate_id == gate_id,
            StageDecision.status == "pending",
        )
    )
    if pending is not None:
        await db.delete(pending)
    stage_run.config_snapshot = stage_run.config_snapshot | {"review_gate_failed": gate_id}
    await emit_attention(db, run, stage_run, {"reason": reason, "code": code})
    await db.commit()
