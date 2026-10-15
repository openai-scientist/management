"""Obligations: a risk accepted at one stage that a later stage must control.

They live in `research_runs.obligations` (a list of contract `Obligation` dicts). The Platform
creates them when a stage is accepted over blocking findings or serious Critic points (R4). The
Critic sets their status at the stage that owns them, and `build_guidance` tells the engine about
the open ones.
"""

import logging

from sqlalchemy import select

from management.research.contracts import (
    SELECTION_HYPOTHESIS_IDS,
    CriticReport,
    FindingKind,
    Obligation,
)
from management.research.models import PiConsultation
from management.research.pi.findings import ReviewUnreadable, hypothesis_id, latest_review

logger = logging.getLogger(__name__)

TEXT_MAX = 600
#: The target of an obligation that comes from the last stage.
CLOSE_TARGET = "close"


def load(run) -> list[Obligation]:
    return [Obligation.model_validate(item) for item in run.obligations or []]


def store(run, obligations: list[Obligation]) -> None:
    run.obligations = [item.model_dump(mode="json") for item in obligations]


def targeted_at(run, stage_key) -> list[Obligation]:
    return [item for item in load(run) if item.target_stage_key == stage_key]


def _next_number(obligations: list[Obligation]) -> int:
    numbers = [
        int(item.obligation_id[1:])
        for item in obligations
        if item.obligation_id[1:].isdigit() and item.obligation_id[0] == "R"
    ]
    return max(numbers, default=0) + 1


def _point_text(point) -> str:
    fix_class = point.get("fix_class") or "fix"
    prefix = "" if fix_class == "fix" else f"[{fix_class}] "
    return f"{prefix}[{point['lens']}] {point['point']}"


async def _critic_points(db, stage_run) -> list[tuple[str, str]]:
    """(point id, text) of the serious Critic points the stage may be accepted over.

    The latest applied `stage_review` report of the stage run. When a `stage_outcome` answered it,
    only the points that answer accepted; with no outcome (the researcher decided alone), all.
    """
    results = {}
    for kind in ("stage_review", "stage_outcome"):
        query = (
            select(PiConsultation.result)
            .where(
                PiConsultation.stage_run_id == stage_run.id,
                PiConsultation.kind == kind,
                PiConsultation.result.is_not(None),
            )
            .order_by(PiConsultation.created_at.desc())
            .limit(1)
        )
        if kind == "stage_review":
            query = query.where(PiConsultation.status == "applied")
        results[kind] = await db.scalar(query)
    report = (results["stage_review"] or {}).get("report")
    if not report:
        return []
    outcome = results["stage_outcome"]
    answered = (
        {
            r["point_id"]
            for r in outcome.get("critic_response") or []
            if r.get("response") == "accepted"
        }
        if outcome
        else None
    )
    return [
        (p["point_id"], _point_text(p))
        for p in report.get("points", [])
        if p["severity"] == "serious" and (answered is None or p["point_id"] in answered)
    ]


async def carry_forward(db, app_state, run, stage_run, selection, next_config) -> None:
    """On accept: move uncontrolled obligations on, and add one per risk the stage is accepted over.

    The risks are the open blocking findings of the latest review (anchored to a selected item, or
    to the whole artifact) and the serious Critic points the PI accepted. The last stage targets
    `close`.
    """
    obligations = load(run)
    if next_config is not None:
        for item in obligations:
            if item.target_stage_key == stage_run.stage_key and item.status != "controlled":
                item.target_stage_key, item.status = next_config.stage_key, "open"
    target = next_config.stage_key if next_config is not None else CLOSE_TARGET
    chosen = set(selection.get(SELECTION_HYPOTHESIS_IDS) or [])
    try:
        review = await latest_review(db, app_state, stage_run)
    except ReviewUnreadable:
        logger.warning("Obligations skipped: review of stage run %s is unreadable", stage_run.id)
        review = None
    known = {
        (item.source_stage_key, item.item_id, item.finding_id)
        for item in obligations
        if item.status != "controlled"
    }
    number = _next_number(obligations)

    def add(item_id, finding_id, text):
        nonlocal number
        key = (stage_run.stage_key, item_id, finding_id)
        if key in known:
            return
        known.add(key)
        obligations.append(
            Obligation(
                obligation_id=f"R{number}",
                source_stage_key=stage_run.stage_key,
                item_id=item_id,
                finding_id=finding_id,
                text=text[:TEXT_MAX],
                target_stage_key=target,
            )
        )
        number += 1

    for finding in review.findings if review else []:
        item_id = hypothesis_id(finding.anchor.locator)
        carried = finding.severity == "blocking" or (
            finding.severity == "major" and finding.kind != FindingKind.fix
        )
        if carried and (item_id is None or item_id in chosen):
            add(item_id, finding.finding_id, finding.issue)
    for point_id, text in await _critic_points(db, stage_run):
        add(None, point_id, text)
    store(run, obligations)


def apply_checks(run, report: CriticReport, consultation_id, stage_key) -> None:
    """The Critic's verdict on each obligation targeted at the stage it reviewed."""
    obligations = load(run)
    by_id = {item.obligation_id: item for item in obligations}
    for check in report.obligations:
        item = by_id.get(check.obligation_id)
        if item is None or item.target_stage_key != stage_key:
            logger.warning("Critic check on %s ignored: not targeted at %s", check, stage_key)
            continue
        if check.status == "controlled" and not check.locator:
            logger.warning(
                "Critic check on %s ignored: controlled without a locator", item.obligation_id
            )
            continue
        item.status, item.locator, item.note = check.status, check.locator, check.note
        item.checked_by = str(consultation_id)
    store(run, obligations)


def guidance_line(item: Obligation) -> str:
    origin = ", ".join(
        part
        for part in (
            item.source_stage_key,
            item.item_id,
            f"finding {item.finding_id}" if item.finding_id else None,
        )
        if part
    )
    return (
        f"Known risk {item.obligation_id} (from {origin}): {item.text.rstrip().rstrip('.')}. "
        "State in your output how it is controlled."
    )
