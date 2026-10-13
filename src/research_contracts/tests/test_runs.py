import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from research_contracts import (
    ArtifactRef,
    EventEnvelope,
    Gate,
    GateAnswer,
    ModelConfig,
    Models,
    RunCreateRequest,
    RunState,
)


def researcher_finding():
    return dict(
        finding_id="F1-1",
        anchor={"locator": "section:methods"},
        severity="major",
        issue="Clarify baseline",
        author="researcher",
    )


def question():
    return dict(
        gate_id="g",
        kind="question",
        step_key="s",
        prompt="?",
        opened_at=datetime.now(UTC),
        options=[{"option_id": "a", "label": "A"}, {"option_id": "b", "label": "B"}],
        default_option_id="a",
    )


def test_review_model_must_differ():
    m = ModelConfig(provider="openai_compatible", model="same", api_key="sk-test")
    with pytest.raises(ValidationError, match="review model must differ"):
        Models(stage=m, review=m)


def test_request_secrets_hidden_and_explicit_wire_unmasks(request_data):
    req = RunCreateRequest(**request_data)
    for marker in ("sk-test", "integration-secret"):
        assert marker not in repr(req)
        assert marker not in json.dumps(req.model_dump(mode="json"))
        assert marker not in str(req.model_dump())
    assert req.to_wire()["models"]["stage"]["api_key"] == "sk-test"
    assert req.to_wire()["integrations"]["search"] == "integration-secret"


def test_request_rejects_incompatible_contract(request_data):
    with pytest.raises(ValidationError, match="incompatible contract_version"):
        RunCreateRequest(**(request_data | {"contract_version": "2.0"}))


def test_artifact_ref_schema_alias_roundtrip():
    data = dict(
        artifact_id="a",
        schema="i2h.hypotheses/v1",
        version=1,
        sha256="0" * 64,
        download_url="http://p/a",
    )
    assert ArtifactRef(**data).model_dump(by_alias=True)["schema"] == "i2h.hypotheses/v1"
    with pytest.raises(ValidationError):
        ArtifactRef(**(data | {"schema": "broken"}))


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "answer"},
        {"action": "approve", "findings": [researcher_finding()]},
        {"action": "approve", "option_id": "a"},
        {"action": "comment"},
        {"action": "comment", "findings": [researcher_finding()], "params": {"a": 1}},
        {"action": "comment", "findings": [researcher_finding()], "option_id": "a"},
        {"action": "comment", "findings": [researcher_finding() | {"author": "reviewer"}]},
        {"action": "answer", "option_id": "a", "findings": [researcher_finding()]},
    ],
)
def test_gate_answer_invalid_combinations(payload):
    with pytest.raises(ValidationError):
        GateAnswer.model_validate(payload | {"actor": "user"})


@pytest.mark.parametrize("action,option_id", [("answer", "a"), ("approve", None)])
def test_gate_answer_and_approve_accept_params(action, option_id):
    answer = GateAnswer(action=action, option_id=option_id, params={"kept": ["p1"]}, actor="user")
    assert answer.params == {"kept": ["p1"]}


def test_question_gate_requires_default_in_options():
    with pytest.raises(ValidationError):
        Gate(**(question() | {"default_option_id": "c"}))


def test_review_gate_requires_artifacts_without_options():
    q = question() | {"kind": "artifact_review", "default_option_id": None, "options": []}
    with pytest.raises(ValidationError):
        Gate(**q)
    assert Gate(**(q | {"artifact_ids": ["a"]})).artifact_ids == ["a"]


@pytest.mark.parametrize(
    "phase,failure", [("failed", None), ("drafting", {"code": "x", "message": "x"})]
)
def test_failed_state_requires_failure_and_other_phases_forbid_it(phase, failure):
    with pytest.raises(ValidationError):
        RunState(
            engine_run_id="e",
            stage_run_id="s",
            phase=phase,
            step_key=None,
            waiting_input=False,
            open_gate=None,
            failure=failure,
        )


@pytest.mark.parametrize(
    "kind,phase,payload",
    [
        ("run.gate_opened", "drafting", question()),
        ("run.failed", "failed", {"code": "engine_restarted", "message": "Process restarted"}),
    ],
)
def test_gate_and_failure_lifecycle_payloads(kind, phase, payload):
    assert (
        EventEnvelope(
            source_seq=1,
            type=kind,
            event_class="lifecycle",
            phase=phase,
            payload=payload,
            occurred_at=datetime.now(UTC),
        ).type
        == kind
    )


@pytest.mark.parametrize("kind,phase", [("run.gate_opened", "drafting"), ("run.failed", "failed")])
def test_gate_and_failure_events_reject_missing_payload(kind, phase):
    with pytest.raises(ValidationError):
        EventEnvelope(
            source_seq=1,
            type=kind,
            event_class="lifecycle",
            phase=phase,
            occurred_at=datetime.now(UTC),
        )
