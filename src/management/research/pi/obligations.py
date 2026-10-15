"""Obligations: a risk accepted at one stage that a later stage must control.

They live in `research_runs.obligations` (a list of contract `Obligation` dicts). The Platform
creates them when a stage is accepted over blocking findings, the Critic sets their status at the
stage that owns them, and `build_guidance` tells the engine about the open ones.
"""

import logging

from management.research.contracts import SELECTION_HYPOTHESIS_IDS, CriticReport, Obligation
from management.research.pi.findings import ReviewUnreadable, hypothesis_id, latest_review

logger = logging.getLogger(__name__)

TEXT_MAX = 600


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


async def carry_forward(db, app_state, run, stage_run, selection, next_config) -> None:
    """On accept: move uncontrolled obligations on, and add one per blocking finding accepted."""
    if next_config is None:
        return
    obligations = load(run)
    for item in obligations:
        if item.target_stage_key == stage_run.stage_key and item.status != "controlled":
            item.target_stage_key, item.status = next_config.stage_key, "open"
    chosen = set(selection.get(SELECTION_HYPOTHESIS_IDS) or [])
    if chosen:
        try:
            review = await latest_review(db, app_state, stage_run)
        except ReviewUnreadable:
            logger.warning(
                "Obligations skipped: review of stage run %s is unreadable", stage_run.id
            )
            review = None
        known = {
            (item.source_stage_key, item.item_id, item.finding_id)
            for item in obligations
            if item.status != "controlled"
        }
        number = _next_number(obligations)
        for finding in review.findings if review else []:
            item_id = hypothesis_id(finding.anchor.locator)
            key = (stage_run.stage_key, item_id, finding.finding_id)
            if finding.severity != "blocking" or item_id not in chosen or key in known:
                continue
            obligations.append(
                Obligation(
                    obligation_id=f"R{number}",
                    source_stage_key=stage_run.stage_key,
                    item_id=item_id,
                    finding_id=finding.finding_id,
                    text=finding.issue[:TEXT_MAX],
                    target_stage_key=next_config.stage_key,
                )
            )
            number += 1
    store(run, obligations)


def apply_checks(run, report: CriticReport, consultation_id) -> None:
    """The Critic's verdict on each obligation of the stage it reviewed."""
    obligations = load(run)
    by_id = {item.obligation_id: item for item in obligations}
    for check in report.obligations:
        item = by_id.get(check.obligation_id)
        if item is not None:
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
