from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from platform_be.contracts import (
    DatasetDescriptor,
    EventEnvelope,
    EventPage,
    Finding,
    PipelineRecommendation,
    ResearchProfile,
    ReviewFindings,
    RevisionDiff,
    RevisionItem,
)


def finding(**kwargs):
    return Finding.model_validate(
        dict(
            finding_id="F1-1",
            anchor={"locator": "hypothesis:H2"},
            severity="major",
            issue="Missing baseline",
            author="reviewer",
        )
        | kwargs
    )


def event(event_type="i2h.screen.scored", **kwargs):
    return EventEnvelope.model_validate(
        dict(
            source_seq=1,
            type=event_type,
            event_class="activity",
            step_key="screen",
            actor="agent",
            summary="Scored one paper",
            occurred_at=datetime.now(UTC),
        )
        | kwargs
    )


@pytest.mark.parametrize("blocking,passed", [(True, True), (False, False)])
def test_passed_must_match_blocking_findings(blocking, passed):
    with pytest.raises(ValidationError):
        ReviewFindings(
            round=1,
            reviewed_artifact_id="a",
            reviewer_model="review",
            findings=[finding(severity="blocking" if blocking else "major")],
            passed=passed,
        )


def test_finding_category_default_other():
    assert finding().category == "other"


@pytest.mark.parametrize("outcome", ["rebutted", "not_fixed"])
@pytest.mark.parametrize("note", [None, "", "   "])
def test_unfixed_requires_note(outcome, note):
    with pytest.raises(ValidationError):
        RevisionItem(finding_id="f", outcome=outcome, note=note)


def test_pivot_requires_target():
    with pytest.raises(ValidationError):
        PipelineRecommendation(action="pivot", reason="r")


@pytest.mark.parametrize("field", ["summary", "actor", "step_key"])
def test_activity_requires_summary_actor_step(field):
    with pytest.raises(ValidationError):
        event(**{field: None})


@pytest.mark.parametrize("event_type", ["scored", ".scored", "i2h."])
def test_activity_type_needs_engine_prefix(event_type):
    with pytest.raises(ValidationError):
        event(event_type)


def test_lifecycle_type_must_be_known():
    with pytest.raises(ValidationError):
        event("run.exploded", event_class="lifecycle")


def test_class_alias_roundtrip():
    e = event()
    assert e.model_dump(mode="json", by_alias=True)["class"] == "activity"
    assert e.engine_prefix() == "i2h"


def test_revision_diff_roundtrip():
    d = RevisionDiff(round=1, artifact_before="a", artifact_after="b", patch="-old\n+new")
    assert RevisionDiff.model_validate_json(d.model_dump_json()) == d
    with pytest.raises(ValidationError):
        RevisionDiff(
            round=1, artifact_before="a", artifact_after="b", patch="", format="json_patch"
        )


def test_dataset_roundtrip_and_hash_validation():
    data = dict(
        dataset_version_id="d",
        version_number=1,
        original_filename="data.csv",
        sha256="0" * 64,
        source_type="upload",
        download_url="http://platform/data",
    )
    descriptor = DatasetDescriptor(**data)
    assert DatasetDescriptor.model_validate_json(descriptor.model_dump_json()) == descriptor
    with pytest.raises(ValidationError):
        DatasetDescriptor(**(data | {"sha256": "short"}))


def test_profile_is_base():
    assert ResearchProfile().is_base
    assert not ResearchProfile(domain="se").is_base


def test_empty_event_page_is_valid():
    assert EventPage(events=[]).events == []


@pytest.mark.parametrize(
    "kind,phase,step,payload",
    [
        ("run.phase_changed", "drafting", None, {"from": None}),
        ("run.step_changed", None, "screen", {"from": None}),
        ("run.gate_closed", None, None, {"gate_id": "g", "action": "approve", "actor": "user"}),
        ("review.completed", None, None, {"round": 1, "passed": True, "artifact_id": "a"}),
        (
            "artifact.delivered",
            None,
            None,
            {"artifact_id": "a", "schema": "i2h.hypotheses/v1", "version": 1, "primary": True},
        ),
        ("run.published", "published", None, {"artifact_id": "a"}),
        ("run.cancelled", "cancelled", None, {}),
        (
            "run.cost",
            None,
            None,
            {"cost_usd": None, "usage": {"stage": {"input_tokens": 10, "output_tokens": 2}}},
        ),
    ],
)
def test_lifecycle_payloads(kind, phase, step, payload):
    e = event(kind, event_class="lifecycle", phase=phase, step_key=step, payload=payload)
    assert e.engine_prefix() is None


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("run.published", {"phase": "published", "payload": {}}),
        ("run.phase_changed", {"payload": {"from": None}}),
        ("run.step_changed", {"step_key": None, "payload": {"from": None}}),
        ("run.cancelled", {"phase": "drafting"}),
        ("run.cost", {"payload": {"cost_usd": "x"}}),
        ("run.cost", {"payload": {"usage": {"stage": {"input_tokens": -1}}}}),
    ],
)
def test_invalid_lifecycle_payloads(kind, kwargs):
    with pytest.raises(ValidationError):
        event(kind, event_class="lifecycle", **kwargs)
