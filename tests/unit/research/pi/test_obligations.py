from types import SimpleNamespace

import pytest

from management.research.contracts import CriticReport, Obligation
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
    stage = SimpleNamespace(stage_key="experiment_plan")
    await carry_forward(None, None, run, stage, {}, SimpleNamespace(stage_key="reproduction"))
    state = {o.obligation_id: (o.target_stage_key, o.status, o.note) for o in load(run)}
    assert state == {
        "R1": ("reproduction", "open", "No baseline"),
        "R2": ("experiment_plan", "controlled", None),
        "R3": ("reproduction", "open", None),
        "R4": ("reproduction", "open", None),
    }


async def test_the_last_stage_leaves_obligations_where_they_are():
    run = run_with(item("R1", status="unresolved", note="n"))
    stage = SimpleNamespace(stage_key="experiment_plan")
    await carry_forward(None, None, run, stage, {}, None)
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
