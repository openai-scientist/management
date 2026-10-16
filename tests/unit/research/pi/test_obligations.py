from types import SimpleNamespace

import pytest

from management.research.contracts import (
    Anchor,
    CriticReport,
    Finding,
    Obligation,
    ReviewFindings,
)
from management.research.pi import obligations as obligations_module
from management.research.pi.guidance import build_guidance
from management.research.pi.obligations import apply_checks, carry_forward, guidance_line, load

pytestmark = pytest.mark.unit


def item(obligation_id="R1", target="experiment_plan", status="open", **fields):
    return Obligation(
        obligation_id=obligation_id,
        source_stage_key="hypothesis",
        item_id="H1",
        finding_id="F4-1",
        text="The baseline is not named.",
        target_stage_key=target,
        status=status,
        **fields,
    ).model_dump(mode="json")


class FakeDb:
    """`scalar` answers the stage_review result, then the stage_outcome result."""

    def __init__(self, review_result=None, outcome_result=None):
        self.answers = [review_result, outcome_result]

    async def scalar(self, query):
        return self.answers.pop(0)


@pytest.fixture(autouse=True)
def no_review(monkeypatch):
    async def none(db, app_state, stage_run):
        return None

    monkeypatch.setattr(obligations_module, "latest_review", none)


def with_review(monkeypatch, *findings):
    async def read(db, app_state, stage_run):
        return ReviewFindings(
            round=1,
            reviewed_artifact_id="a1",
            reviewer_model="m",
            findings=[
                Finding(
                    finding_id=fid,
                    anchor=Anchor(locator=locator),
                    severity=severity,
                    kind=kind[0] if kind else "fix",
                    issue=f"Issue {fid}",
                    author="reviewer",
                )
                for fid, locator, severity, *kind in findings
            ],
            passed=not any(f[2] == "blocking" for f in findings),
        )

    monkeypatch.setattr(obligations_module, "latest_review", read)


def critic_result(*severities):
    return {
        "report": {
            "verdict": "serious",
            "summary": "s",
            "points": [
                {
                    "point_id": f"C{n}",
                    "severity": severity,
                    "lens": "integrity",
                    "ref": "integrity:x",
                    "point": f"Point {n}",
                }
                for n, severity in enumerate(severities, 1)
            ],
        }
    }


def run_with(*items):
    return SimpleNamespace(obligations=list(items))


def test_the_guidance_line_names_the_origin_and_asks_for_the_control():
    line = guidance_line(Obligation.model_validate(item()))
    assert line == (
        "Known risk R1 (from hypothesis, H1, finding F4-1): The baseline is not named. "
        "State in your output how it is controlled."
    )


def test_the_guidance_line_skips_what_the_obligation_does_not_have():
    bare = Obligation(
        obligation_id="R2", source_stage_key="hypothesis", text="A risk", target_stage_key="x"
    )
    assert guidance_line(bare).startswith("Known risk R2 (from hypothesis): A risk.")


async def test_uncontrolled_obligations_of_the_accepted_stage_move_on_with_their_note():
    run = run_with(
        item("R1", status="unresolved", note="No baseline"),
        item("R2", status="controlled", locator="plan:b"),
        item("R3", status="open"),
        item("R4", target="reproduction"),
    )
    stage = SimpleNamespace(stage_key="experiment_plan", id=1)
    await carry_forward(FakeDb(), None, run, stage, {}, SimpleNamespace(stage_key="reproduction"))
    state = {o.obligation_id: (o.target_stage_key, o.status, o.note) for o in load(run)}
    assert state == {
        "R1": ("reproduction", "open", "No baseline"),
        "R2": ("experiment_plan", "controlled", None),
        "R3": ("reproduction", "open", None),
        "R4": ("reproduction", "open", None),
    }


async def test_the_last_stage_leaves_obligations_where_they_are():
    run = run_with(item("R1", status="unresolved", note="n"))
    stage = SimpleNamespace(stage_key="experiment_plan", id=1)
    await carry_forward(FakeDb(), None, run, stage, {}, None)
    assert load(run)[0].target_stage_key == "experiment_plan"


def test_the_critic_report_sets_status_locator_note_and_checker():
    run = run_with(item("R1"), item("R2"))
    report = CriticReport.model_validate(
        {
            "verdict": "sound",
            "summary": "ok",
            "obligations": [
                {"obligation_id": "R1", "status": "controlled", "locator": "plan:b"},
                {"obligation_id": "R2", "status": "unresolved", "note": "missing"},
                {"obligation_id": "R9", "status": "controlled", "locator": "x"},
            ],
        }
    )
    apply_checks(run, report, "c-1", "experiment_plan")
    first, second = load(run)
    assert (first.status, first.locator, first.checked_by) == ("controlled", "plan:b", "c-1")
    assert (second.status, second.note, second.checked_by) == ("unresolved", "missing", "c-1")


async def test_obligation_lines_come_after_the_capped_lines():
    run = SimpleNamespace(
        id=1,
        topic="t",
        charter={"question": "q" * 20, "objectives": ["o"] * 10, "success_criteria": ["s"] * 10},
        obligations=[item(f"R{n}") for n in range(1, 6)],
    )

    class Db:
        async def scalars(self, query):
            return iter([])

    lines = await build_guidance(Db(), run, "experiment_plan")
    risks = [line for line in lines if line.startswith("Known risk")]
    assert risks == lines[-5:] and len(risks) == 5


def test_a_check_applies_only_to_an_obligation_targeted_at_the_reviewed_stage():
    run = run_with(item("R1", target="analysis"))
    report = CriticReport.model_validate(
        {
            "verdict": "sound",
            "summary": "ok",
            "obligations": [{"obligation_id": "R1", "status": "controlled", "locator": "x"}],
        }
    )
    apply_checks(run, report, "c-1", "experiment_plan")
    assert load(run)[0].status == "open" and load(run)[0].checked_by is None


def test_controlled_without_a_locator_leaves_the_obligation_unchanged():
    run = run_with(item("R1"))
    report = CriticReport.model_validate(
        {
            "verdict": "sound",
            "summary": "ok",
            "obligations": [{"obligation_id": "R1", "status": "controlled"}],
        }
    )
    apply_checks(run, report, "c-1", "experiment_plan")
    assert load(run)[0].status == "open" and load(run)[0].checked_by is None


async def test_an_unresolved_obligation_stays_in_the_guidance():
    run = SimpleNamespace(
        id=1,
        topic="t",
        charter={"question": "q" * 20, "objectives": [], "success_criteria": []},
        obligations=[
            item("R1", status="unresolved", note="n"),
            item("R2", status="controlled", locator="x"),
        ],
    )

    class Db:
        async def scalars(self, query):
            return iter([])

    lines = await build_guidance(Db(), run, "experiment_plan")
    assert [line[:17] for line in lines if line.startswith("Known risk")] == ["Known risk R1 (fr"]


STAGE = SimpleNamespace(stage_key="hypothesis", id=1)
NEXT = SimpleNamespace(stage_key="experiment_plan")


def summary(run):
    return [(o.finding_id, o.item_id, o.target_stage_key) for o in load(run)]


async def test_open_blocking_findings_become_obligations_for_selected_and_whole_artifact(
    monkeypatch,
):
    with_review(
        monkeypatch,
        ("F1", "hypothesis:H1", "blocking"),
        ("F2", "hypothesis:H2", "blocking"),
        ("F3", "artifact", "blocking"),
        ("F4", "hypothesis:H1", "major"),
    )
    run = run_with()
    await carry_forward(FakeDb(), None, run, STAGE, {"hypothesis_ids": ["H1"]}, NEXT)
    assert summary(run) == [("F1", "H1", "experiment_plan"), ("F3", None, "experiment_plan")]


async def test_major_findings_that_are_not_fixes_become_obligations(monkeypatch):
    with_review(
        monkeypatch,
        ("F1", "hypothesis:H1", "major", "needs_evidence"),
        ("F2", "hypothesis:H1", "major", "fix"),
        ("F3", "hypothesis:H1", "minor", "needs_evidence"),
        ("F4", "artifact", "major", "needs_evidence"),
    )
    run = run_with()
    await carry_forward(FakeDb(), None, run, STAGE, {"hypothesis_ids": ["H1"]}, NEXT)
    assert summary(run) == [("F1", "H1", "experiment_plan"), ("F4", None, "experiment_plan")]


async def test_serious_critic_points_become_obligations_without_duplicates():
    run = run_with()
    db = FakeDb(critic_result("serious", "minor", "serious"))
    await carry_forward(db, None, run, STAGE, {}, NEXT)
    assert summary(run) == [("C1", None, "experiment_plan"), ("C3", None, "experiment_plan")]
    assert load(run)[0].text == "[integrity] Point 1"
    assert len({o.obligation_id for o in load(run)}) == 2


async def test_a_stage_accepted_again_replaces_the_obligations_of_its_earlier_run():
    run = run_with()
    await carry_forward(FakeDb(critic_result("serious")), None, run, STAGE, {}, NEXT)
    other = Obligation.model_validate(item("R9", target="close") | {"source_stage_key": "charter"})
    run.obligations.append(other.model_dump(mode="json"))
    # The run was sent back to the stage: its new run has a different serious C1.
    result = critic_result("serious")
    result["report"]["points"][0]["point"] = "New risk"
    await carry_forward(FakeDb(result), None, run, STAGE, {}, NEXT)
    assert [(o.obligation_id, o.source_stage_key, o.text) for o in load(run)] == [
        ("R9", "charter", "The baseline is not named."),
        ("R10", "hypothesis", "[integrity] New risk"),
    ]


async def test_the_points_the_outcome_rejected_do_not_become_obligations():
    outcome = {
        "critic_response": [
            {"point_id": "C1", "response": "accepted", "reason": "r"},
            {"point_id": "C2", "response": "rejected", "reason": "r"},
        ]
    }
    run = run_with()
    await carry_forward(
        FakeDb(critic_result("serious", "serious", "serious"), outcome), None, run, STAGE, {}, NEXT
    )
    # C3 has no answer: the researcher decided over it, so it is carried.
    assert summary(run) == [("C1", None, "experiment_plan"), ("C3", None, "experiment_plan")]


async def test_the_last_stage_targets_close():
    run = run_with()
    await carry_forward(FakeDb(critic_result("serious")), None, run, STAGE, {}, None)
    assert summary(run) == [("C1", None, "close")]


async def test_a_point_that_is_not_a_fix_keeps_its_class_in_the_text():
    result = critic_result("serious", "serious")
    result["report"]["points"][0]["fix_class"] = "needs_evidence"
    result["report"]["points"][1]["fix_class"] = "later"
    run = run_with()
    await carry_forward(FakeDb(result), None, run, STAGE, {}, NEXT)
    assert [o.text for o in load(run)] == [
        "[needs_evidence] [integrity] Point 1",
        "[later] [integrity] Point 2",
    ]
