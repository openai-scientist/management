"""Take a PI result and act on it: apply it, hand it to the researcher, or drop it as stale."""

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select

from management.access.service import lock_project_scope
from management.core.errors import APIError
from management.research.contracts import (
    AllowedAction,
    Charter,
    ConsultationResult,
    Escalation,
    FindingKind,
)
from management.research.models import (
    PiConsultation,
    ResearchRun,
    StageDecision,
    StageRun,
)
from management.research.orchestrator import (
    _consultation_payload,
    _decision_event,
    _relock,
    accept_stage,
    first_stage_inputs,
    rerun_stage,
    send_back,
    start_stage,
    stop_research_run,
    validate_selection,
)
from management.research.pi.authority import escalation_stops, run_mode
from management.research.pi.findings import ReviewUnreadable, hypothesis_id, latest_review
from management.research.pi.obligations import apply_checks
from management.research.pi.service import (
    add_note,
    critic_unavailable,
    emit_attention,
    fail_consultation,
    mark_overridden,
    request_consultation,
    result_cost,
    run_active,
)
from management.research.stage_runs import append_run_event, recompute_run_cost

FIRST_STAGE_DONE = ("COMPLETED", "ACCEPTED", "SUPERSEDED")
STUCK_AFTER = timedelta(minutes=10)
# The PI is asked about a failed stage or a finished run, so these kinds skip the active check.
FINISHED_OK_KINDS = ("failure", "close")


def _dump(result: ConsultationResult) -> dict:
    return result.model_dump(mode="json")


async def is_current(db, run, consultation, stage_run) -> bool:
    """Whether the situation the PI was asked about still holds."""
    kind = consultation.kind
    if kind not in FINISHED_OK_KINDS and not run_active(run):
        return False
    if kind == "charter":
        return run.charter_status == "pending"
    if stage_run is None:
        return False
    if kind in {"stage_review", "stage_outcome"}:
        return stage_run.status == "COMPLETED"
    if kind == "failure":
        return (
            stage_run.status == "FAILED"
            and stage_run.iteration == run.iteration
            and await db.scalar(
                select(StageRun.id).where(StageRun.prior_stage_run_id == stage_run.id)
            )
            is None
        )
    return kind == "close"  # The engine gate kinds are no longer asked.


async def _blocking_scope(db, app_state, stage_run) -> tuple[str | None, dict[str, set[str]]]:
    """B12: (reason the whole artifact is blocked or None, blocking finding ids per hypothesis id).

    Unreadable review fails closed: the whole artifact is blocked.
    """
    try:
        review = await latest_review(db, app_state, stage_run)
    except ReviewUnreadable:
        return "The review findings cannot be read, so the PI cannot accept", {}
    if review is None:
        return None, {}
    blocked: dict[str, set[str]] = {}
    known_risk = False
    for finding in review.findings:
        if finding.severity != "blocking":
            continue
        item_id = hypothesis_id(finding.anchor.locator)
        if item_id is None:
            # One that needs evidence or a tool travels as a known risk: revising cannot fix it.
            if finding.kind != FindingKind.fix:
                known_risk = True
                continue
            return "The review has a fixable blocking finding on the whole artifact", {}
        blocked.setdefault(item_id, set()).add(finding.finding_id)
    if not review.passed and not blocked and not known_risk:
        return "The review still has blocking findings", {}
    return None, blocked


def _risk_stated(handoff, hypothesis_id, finding_ids) -> bool:
    """B19: a handoff line names the hypothesis as a whole word and one of its blocking findings."""

    def token(value):
        # Ids like "F1-1" are real: a hyphen or word character next to the id is another id.
        return re.compile(rf"(?<![\w-]){re.escape(value)}(?![\w-])")

    hypothesis = token(hypothesis_id)
    findings = [token(f) for f in finding_ids]
    return any(
        hypothesis.search(line) and any(f.search(line) for f in findings) for line in handoff
    )


async def check_decision(db, app_state, consultation, stage_run, result) -> Escalation | None:
    """Guards in code: the action is allowed here, and no accept over blocking findings."""
    decision = result.decision
    allowed = {
        a.action: a
        for a in (AllowedAction.model_validate(x) for x in consultation.request["allowed"])
    }
    rule = allowed.get(decision.action)

    def refuse(message):
        return Escalation(
            reason_code="cannot_decide", message=f"Not a valid decision here: {message}"
        )

    if rule is None:
        return refuse(f"{decision.action} is not allowed")
    if (
        decision.action == "accept"
        and rule.selection_key
        and not decision.selection.get(rule.selection_key)
    ):
        return refuse(f"select at least one {rule.selection_key}")
    if decision.action == "send_back" and decision.target_stage_key not in rule.targets:
        return refuse("target stage is not allowed")
    if decision.action == "accept" and consultation.kind == "stage_outcome":
        whole, blocked = await _blocking_scope(db, app_state, stage_run)
        chosen = (
            set(decision.selection.get(rule.selection_key) or []) if rule.selection_key else set()
        )
        if whole:
            return Escalation(reason_code="integrity", message=whole)
        unaccepted = sorted(
            h for h in chosen & blocked.keys() if not _risk_stated(result.handoff, h, blocked[h])
        )
        if unaccepted:
            return Escalation(
                reason_code="integrity",
                message=(
                    f"Blocking findings remain on: {', '.join(unaccepted)} (name each in a "
                    "handoff line with its finding id to accept it as a known risk)"
                ),
            )
    return None


async def settle_charter(
    db, app_state, run, charter: Charter, *, author, reason=None, consultation_id=None
):
    """Store the charter (a new version) and, the first time, start the first stage."""
    first_key = app_state.settings.pipelines[run.pipeline_key][0].stage_key
    post_hoc = (
        await db.scalar(
            select(StageRun.id)
            .where(
                StageRun.research_run_id == run.id,
                StageRun.stage_key == first_key,
                StageRun.status.in_(FIRST_STAGE_DONE),
            )
            .limit(1)
        )
        is not None
    )
    version = ((run.charter or {}).get("version") or 0) + 1
    starting = run.charter_status == "pending"
    run.charter = charter.model_dump(mode="json") | {
        "version": version,
        "author": author,
        "post_hoc": post_hoc,
        "reason": reason,
    }
    run.charter_status = "settled"
    append_run_event(
        db,
        run,
        None,
        "pi.charter_settled",
        {"version": version, "author": author, "post_hoc": post_hoc}
        | ({"consultation_id": str(consultation_id)} if consultation_id else {}),
        actor="pi" if author == "pi" else "user",
    )
    add_note(
        db,
        run,
        "decision",
        f"Charter v{version} settled by {author}{' (after results)' if post_hoc else ''}: "
        f"{charter.question}" + (f" Reason: {reason}" if reason else ""),
        "pi" if author == "pi" else "user",
        consultation_id=consultation_id,
    )
    if starting:
        await start_stage(
            db,
            app_state,
            run,
            app_state.settings.pipelines[run.pipeline_key][0],
            inputs=first_stage_inputs(run),
        )


async def apply_decision(
    db, app_state, run, consultation, stage_run, result, *, actor, user_id, reason=None
):
    """Run the PI decision through the same functions a researcher uses. Returns the decision id."""
    decision = result.decision
    action = decision.action
    cid = consultation.id
    common = {"actor": actor, "user_id": user_id, "consultation_id": cid, "reason": reason}
    if actor == "pi":
        common["decided_by"] = f"pi:{cid}"
    if action == "charter":
        await settle_charter(
            db, app_state, run, decision.charter, author="pi", consultation_id=cid, reason=reason
        )
        return None
    if action == "accept":
        await validate_selection(db, app_state, stage_run, decision.selection)
        await accept_stage(
            db, app_state, stage_run, selection=decision.selection, note=decision.note, **common
        )
        return await db.scalar(
            select(StageDecision.id)
            .where(StageDecision.stage_run_id == stage_run.id, StageDecision.action == "accept")
            .order_by(StageDecision.decided_at.desc())
            .limit(1)
        )
    if action == "send_back":
        note = "\n".join(filter(None, [decision.note, *result.handoff])) or None
        row = await send_back(
            db,
            app_state,
            stage_run,
            target_stage_key=decision.target_stage_key,
            note=note,
            artifact_ids=[],
            **common,
        )
        return row.id
    if action == "stop":
        if not run_active(run) and stage_run is not None:
            # The run already ended (a failed stage): record the decision, nothing to cancel.
            row = StageDecision(
                stage_run_id=stage_run.id,
                action="stop",
                actor=actor,
                decided_by_user_id=user_id,
                reason=reason,
                payload=({"decided_by": common["decided_by"]} if "decided_by" in common else {})
                | _consultation_payload(cid),
            )
            db.add(row)
            _decision_event(db, run, stage_run, row, actor=actor)
            await db.flush()
            return row.id
        row = await stop_research_run(db, app_state, run, **common)
        return row.id
    if action == "rerun":
        row = await rerun_stage(db, app_state, stage_run, **common)
        return row.id
    raise APIError(409, "CONSULTATION_NOT_ACTIONABLE", "This PI result has no action to apply")


def _hand_over(db, run, consultation, stage_run, result):
    consultation.status = "awaiting_researcher"
    consultation.result = _dump(result)
    escalate = result.escalate
    append_run_event(
        db,
        run,
        stage_run,
        "pi.recommendation",
        {
            "consultation_id": str(consultation.id),
            "kind": consultation.kind,
            "action": result.decision.action if result.decision else None,
            "escalation": escalate.model_dump(mode="json") if escalate else None,
        },
        actor="pi",
    )


async def _failed_to_apply(db, run_id, consultation_id, stage_run_id, code, message) -> None:
    """The decision could not be applied: nothing of it stays, and the researcher decides."""
    await db.rollback()
    run = await _relock(db, ResearchRun, run_id)
    consultation = await _relock(db, PiConsultation, consultation_id)
    if consultation.status != "decided":
        return
    stage_run = (
        await db.get(StageRun, stage_run_id, populate_existing=True) if stage_run_id else None
    )
    result = ConsultationResult.model_validate(consultation.result).model_copy(
        update={
            "escalate": Escalation(
                reason_code="cannot_decide",
                message=f"Applying the PI decision failed: {message}",
            )
        }
    )
    _hand_over(db, run, consultation, stage_run, result)
    await emit_attention(
        db,
        run,
        stage_run,
        {
            "consultation_id": str(consultation_id),
            "kind": consultation.kind,
            "reason": "apply_failed",
            "code": code,
        },
    )


async def _store(db, run, consultation, result):
    """The result and its cost, kept even when the answer comes too late to act on."""
    consultation.result = _dump(result)
    consultation.decided_at = datetime.now(UTC)
    cost, known = await result_cost(db, result, consultation.kind)
    consultation.cost_usd = cost
    if not known:
        run.cost_unknown = True
    await recompute_run_cost(db, run)


async def _record(db, run, consultation, stage_run, result):
    """Notebook and timeline event: written once, when the result is still wanted."""
    stage_key = stage_run.stage_key if stage_run else None
    for draft in result.notebook:
        add_note(
            db,
            run,
            draft.kind,
            draft.text,
            "pi",
            consultation_id=consultation.id,
            stage_key=stage_key,
        )
    if result.brief:
        add_note(
            db,
            run,
            "brief",
            result.brief,
            "pi",
            consultation_id=consultation.id,
            stage_key=stage_key,
        )
    decision = result.decision
    if decision.action != "report":
        target = f" to {decision.target_stage_key}" if decision.target_stage_key else ""
        add_note(
            db,
            run,
            "decision",
            f"{decision.action}{target}: {result.rationale or result.brief or 'no rationale'}",
            "pi",
            consultation_id=consultation.id,
            stage_key=stage_key,
        )
    append_run_event(
        db,
        run,
        stage_run,
        "pi.decision",
        {
            "consultation_id": str(consultation.id),
            "kind": consultation.kind,
            "action": decision.action,
            "confidence": result.confidence,
        },
        actor="pi",
        event_class="activity",
        summary=result.brief or None,
    )


def _overrides_critic(consultation, result) -> bool:
    """The PI left a serious Critic point unanswered or rejected it: the researcher decides."""
    critique = consultation.request.get("critique") or {}
    serious = {p["point_id"] for p in critique.get("points", []) if p["severity"] == "serious"}
    accepted = {r.point_id for r in result.critic_response if r.response == "accepted"}
    return bool(serious - accepted)


async def _apply_critic_report(db, app_state, run, consultation, stage_run, result) -> None:
    """Keep the Critic report, update the obligations, then ask the PI for the outcome."""
    report = result.report
    if report is None:
        reason = "the Critic returned no report"
        consultation.status, consultation.resolved_at = "failed", datetime.now(UTC)
        consultation.failure = {"code": "no_report", "message": reason}
        await critic_unavailable(db, app_state, run, stage_run, reason)
        return
    await _record(db, run, consultation, stage_run, result)
    apply_checks(run, report, consultation.id, stage_run.stage_key)
    add_note(
        db,
        run,
        "critique",
        f"Critic {report.verdict}: {report.summary}",
        "pi",
        consultation_id=consultation.id,
        stage_key=stage_run.stage_key,
    )
    consultation.status, consultation.resolved_at = "applied", datetime.now(UTC)
    await db.flush()  # The outcome request reads the report from the stored row.
    await request_consultation(db, app_state, run, "stage_outcome", stage_run=stage_run)


async def handle_result(db, app_state, consultation_id: UUID, result: ConsultationResult) -> bool:
    """Process a PI callback. Returns False when this result was already processed."""
    consultation = await db.get(PiConsultation, consultation_id)
    if consultation is None:
        raise APIError(404, "NOT_FOUND", "Consultation was not found")
    run_id, stage_run_id = consultation.research_run_id, consultation.stage_run_id
    project_id = (await db.get(ResearchRun, run_id)).project_id
    await lock_project_scope(db, project_id)
    run = await _relock(db, ResearchRun, run_id)
    consultation = await _relock(db, PiConsultation, consultation_id)
    if consultation.result is not None:
        return False
    stage_run = (
        await db.get(StageRun, stage_run_id, populate_existing=True) if stage_run_id else None
    )
    await _store(db, run, consultation, result)
    if result.status == "failed":
        if consultation.status in {"queued", "sent"}:
            await fail_consultation(
                db, consultation, "pi_failed", result.failure.message[:500], app_state=app_state
            )
        return True
    if consultation.status not in {"queued", "sent"}:
        # Overridden, stale or failed while the PI was thinking: kept for the record only.
        return True
    consultation.status = "decided"
    if not await is_current(db, run, consultation, stage_run):
        consultation.status, consultation.resolved_at = "stale", datetime.now(UTC)
        return True
    if consultation.kind == "stage_review":
        await _apply_critic_report(db, app_state, run, consultation, stage_run, result)
        return True
    await _record(db, run, consultation, stage_run, result)
    guard = await check_decision(db, app_state, consultation, stage_run, result)
    if guard is not None:
        result = result.model_copy(update={"escalate": guard})
    stops = result.escalate is not None and escalation_stops(
        run_mode(run), result.escalate.reason_code.value
    )
    if result.escalate is not None and not stops:
        add_note(
            db,
            run,
            "observation",
            f"The PI decided despite {result.escalate.reason_code.value}: "
            f"{result.escalate.message}",
            "pi",
            consultation_id=consultation.id,
            stage_key=stage_run.stage_key if stage_run else None,
        )
    if consultation.kind == "close":
        consultation.status, consultation.resolved_at = "applied", datetime.now(UTC)
        return True
    overrides = _overrides_critic(consultation, result)
    if consultation.authority != "pi" or stops or overrides:
        _hand_over(db, run, consultation, stage_run, result)
        if consultation.authority == "pi":
            await emit_attention(
                db,
                run,
                stage_run,
                {
                    "consultation_id": str(consultation.id),
                    "kind": consultation.kind,
                    "reason": "pi_overrides_critic" if overrides else "escalation",
                    "code": result.escalate.reason_code.value if result.escalate else None,
                },
            )
        return True
    # Keep what is recorded before a function that commits halfway can fail.
    consultation.result = _dump(result)
    await db.commit()
    run = await _relock(db, ResearchRun, run_id)
    consultation = await _relock(db, PiConsultation, consultation_id)
    if stage_run_id:
        stage_run = await _relock(db, StageRun, stage_run_id)
    if consultation.status != "decided":
        return True  # The researcher decided in the window.
    if not await is_current(db, run, consultation, stage_run):
        consultation.status, consultation.resolved_at = "stale", datetime.now(UTC)
        return True
    try:
        decision_id = await apply_decision(
            db,
            app_state,
            run,
            consultation,
            stage_run,
            result,
            actor="pi",
            user_id=None,
            reason=result.rationale or None,
        )
    except APIError as exc:
        await _failed_to_apply(db, run_id, consultation_id, stage_run_id, exc.code, exc.message)
        return True
    except Exception as exc:  # noqa: BLE001 - engine errors must not strand a decided row
        message = f"{type(exc).__name__}: {exc}"[:500]
        await _failed_to_apply(db, run_id, consultation_id, stage_run_id, "apply_failed", message)
        return True
    consultation = await _relock(db, PiConsultation, consultation_id)
    consultation.status, consultation.resolved_at = "applied", datetime.now(UTC)
    consultation.applied_decision_id = decision_id
    return True


async def endorse(db, app_state, consultation_id: UUID, *, user_id, project_id, reason=None):
    consultation = await _consultation(db, consultation_id, project_id)
    run_id, stage_run_id = consultation.research_run_id, consultation.stage_run_id
    run = await _relock(db, ResearchRun, run_id)
    consultation = await _relock(db, PiConsultation, consultation_id)
    if consultation.status != "awaiting_researcher" or not consultation.result:
        raise APIError(409, "CONSULTATION_NOT_AWAITING", "The PI has no recommendation to use")
    result = ConsultationResult.model_validate(consultation.result)
    if result.decision is None or result.decision.action == "report":
        raise APIError(409, "CONSULTATION_NOT_ACTIONABLE", "This PI result has no action to use")
    stage_run = await _relock(db, StageRun, stage_run_id) if stage_run_id else None
    if not await is_current(db, run, consultation, stage_run):
        consultation.status, consultation.resolved_at = "stale", datetime.now(UTC)
        await db.commit()
        raise APIError(409, "CONSULTATION_STALE", "The situation changed after the PI answered")
    # Set before applying: the next stage start reads the hand-off of endorsed consultations.
    consultation.status, consultation.resolved_at = "endorsed", datetime.now(UTC)
    await db.flush()
    try:
        decision_id = await apply_decision(
            db,
            app_state,
            run,
            consultation,
            stage_run,
            result,
            actor="user",
            user_id=user_id,
            reason=reason,
        )
    except Exception:
        consultation.status, consultation.resolved_at = "awaiting_researcher", None
        raise
    consultation = await _relock(db, PiConsultation, consultation_id)
    consultation.applied_decision_id = decision_id
    if reason:
        add_note(
            db,
            run,
            "decision",
            f"The researcher used the PI recommendation. {reason}",
            "user",
            consultation_id=consultation_id,
            stage_key=stage_run.stage_key if stage_run else None,
        )
    return consultation


def _stuck(consultation) -> bool:
    """Sent (or queued) long ago with no result: the PI is not going to answer."""
    since = consultation.sent_at if consultation.status == "sent" else consultation.created_at
    if consultation.status not in {"sent", "queued"} or consultation.result or since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return datetime.now(UTC) - since > STUCK_AFTER


async def retry(db, app_state, consultation_id: UUID, *, project_id):
    """Ask again after a failed or stale consultation: a new consultation with a new id."""
    consultation = await _consultation(db, consultation_id, project_id)
    run = await _relock(db, ResearchRun, consultation.research_run_id)
    consultation = await _relock(db, PiConsultation, consultation_id)
    stuck = _stuck(consultation)
    if consultation.status not in {"failed", "stale"} and not stuck:
        raise APIError(409, "CONSULTATION_NOT_RETRYABLE", "Only a failed or stale consultation")
    stage_run = (
        await _relock(db, StageRun, consultation.stage_run_id)
        if consultation.stage_run_id
        else None
    )
    if not await is_current(db, run, consultation, stage_run):
        raise APIError(409, "CONSULTATION_STALE", "The situation changed; there is nothing to ask")
    if stuck:
        consultation.status, consultation.resolved_at = "failed", datetime.now(UTC)
        consultation.failure = {"code": "pi_timeout", "message": "The PI did not answer in time"}
        await db.flush()
    return await request_consultation(db, app_state, run, consultation.kind, stage_run=stage_run)


async def put_charter(db, app_state, run, charter: Charter, *, reason=None):
    if run.charter_status == "none":
        raise APIError(409, "PI_NOT_ACTIVE", "This run has no PI charter")
    if not run_active(run):
        raise APIError(409, "RUN_FINISHED", "This run has already finished")
    await mark_overridden(
        db, app_state, run, kinds=("charter",), what="wrote the charter", reason=reason
    )
    await settle_charter(db, app_state, run, charter, author="researcher", reason=reason)


async def _consultation(db, consultation_id, project_id) -> PiConsultation:
    consultation = await db.get(PiConsultation, consultation_id)
    run = await db.get(ResearchRun, consultation.research_run_id) if consultation else None
    if run is None or run.project_id != project_id:
        raise APIError(404, "NOT_FOUND", "Consultation was not found")
    await lock_project_scope(db, project_id)
    return consultation
