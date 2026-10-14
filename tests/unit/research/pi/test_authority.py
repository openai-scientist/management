from types import SimpleNamespace

import pytest

from management.core.engine_settings import PipelineStage
from management.research.pi.authority import authority_for, engine_mode, escalation_stops

pytestmark = pytest.mark.unit

FREE = PipelineStage(stage_key="plan", engine="exd", checkpoint=False)
CHECKPOINT = PipelineStage(stage_key="hypothesis", engine="i2h")


def run(mode):
    return SimpleNamespace(interaction_mode=mode)


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("charter", ["pi", "researcher", "researcher", "researcher"]),
        ("gate_question", ["pi", "pi", "researcher", "researcher"]),
        ("gate_review", ["pi", "pi", "researcher", "researcher"]),
        ("stage_outcome", ["pi", "researcher", "researcher", "researcher"]),
        ("failure", ["researcher"] * 4),
        ("close", ["pi"] * 4),
    ],
)
def test_matrix(kind, expected):
    modes = ["auto", "light", "copilot", "full"]
    assert [authority_for(run(mode), kind, CHECKPOINT) for mode in modes] == expected


def test_light_lets_the_pi_decide_stages_that_are_not_checkpoints():
    assert authority_for(run("light"), "stage_outcome", FREE) == "pi"
    assert authority_for(run("copilot"), "stage_outcome", FREE) == "researcher"


def test_engine_mode_opens_every_gate_unless_full():
    assert [engine_mode(run(m)) for m in ("auto", "light", "copilot", "full")] == [
        "copilot",
        "copilot",
        "copilot",
        "full",
    ]


@pytest.mark.parametrize(
    ("mode", "code", "stops"),
    [
        ("auto", "integrity", True),
        ("auto", "charter_conflict", True),
        ("auto", "cannot_decide", True),
        ("auto", "low_confidence", False),
        ("auto", "out_of_scope", False),
        ("light", "low_confidence", True),
        ("copilot", "out_of_scope", True),
    ],
)
def test_escalation(mode, code, stops):
    assert escalation_stops(mode, code) is stops
