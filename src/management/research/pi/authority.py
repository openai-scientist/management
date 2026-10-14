"""Who decides at each decision point (spec section 5). Pure functions, no I/O."""

from typing import Literal

Authority = Literal["pi", "researcher"]

PI_ESCALATIONS_THAT_STOP_AUTO = frozenset({"integrity", "charter_conflict", "cannot_decide"})
KINDS = ("charter", "gate_question", "gate_review", "stage_outcome", "failure", "close")


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
    if kind in {"gate_question", "gate_review"}:
        return "pi" if mode in {"auto", "light"} else "researcher"
    if kind == "stage_outcome":
        if mode == "auto":
            return "pi"
        if mode == "light":
            checkpoint = True if stage_config is None else stage_config.checkpoint
            return "researcher" if checkpoint else "pi"
        return "researcher"
    raise ValueError(f"unknown consultation kind: {kind}")


def engine_mode(run) -> str:
    """P5: the engine opens every gate unless the researcher chose `full` anyway."""
    return "full" if run_mode(run) == "full" else "copilot"


def escalation_stops(mode: str, reason_code: str) -> bool:
    """Whether an escalation hands the decision to the researcher."""
    return mode != "auto" or reason_code in PI_ESCALATIONS_THAT_STOP_AUTO
