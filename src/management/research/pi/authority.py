"""Who decides at each decision point (spec section 5). Pure functions, no I/O."""

from typing import Literal

Authority = Literal["pi", "researcher"]

PI_ESCALATIONS_THAT_STOP_AUTO = frozenset({"integrity", "charter_conflict", "cannot_decide"})
#: The Platform no longer consults the PI at engine gates (`gate_*` stay on the wire only).
KINDS = ("charter", "stage_review", "stage_outcome", "failure", "close")


def run_mode(run) -> str:
    return run.interaction_mode or "copilot"


def authority_for(run, kind: str, stage_config=None) -> Authority:
    """`pi`: the PI decides and the Platform applies it. `researcher`: the PI only advises."""
    mode = run_mode(run)
    if kind == "close":
        return "pi"
    if kind == "failure":
        return "researcher"
    if kind == "charter":
        return "pi" if mode == "auto" else "researcher"
    if kind == "stage_review":
        # A report: nothing is applied.
        return "pi"
    if kind == "stage_outcome":
        if mode == "auto":
            return "pi"
        if mode == "light":
            checkpoint = True if stage_config is None else stage_config.checkpoint
            return "researcher" if checkpoint else "pi"
        return "researcher"
    raise ValueError(f"unknown consultation kind: {kind}")


def engine_mode(run) -> str:
    """The engine runs in the mode the researcher chose; the Platform answers the gates it owns."""
    return run_mode(run)


def review_gate_applies(run, gate_kind: str) -> bool:
    """The deterministic review-gate rule answers `artifact_review` gates in auto and light."""
    return gate_kind == "artifact_review" and run_mode(run) in {"auto", "light"}


def escalation_stops(mode: str, reason_code: str) -> bool:
    """Whether an escalation hands the decision to the researcher."""
    return mode != "auto" or reason_code in PI_ESCALATIONS_THAT_STOP_AUTO
