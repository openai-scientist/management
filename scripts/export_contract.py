"""Export the Platform-owned wire contract without importing application services."""

import argparse
import json
from pathlib import Path
from typing import Any

from platform_be import contracts as c
from platform_be.contracts.events import (
    EmptyPayload,
    PhaseChanged,
    StepChanged,
)

DOCS = Path(__file__).resolve().parents[1] / "docs" / "contract"


def _models(parent: type[c.ContractModel]) -> dict[str, type[c.ContractModel]]:
    result = {}
    for model in parent.__subclasses__():
        if model.__module__.startswith("platform_be.contracts."):
            result[model.__name__] = model
        result.update(_models(model))
    return result


WIRE_MODELS = _models(c.ContractModel)


def example_payloads() -> tuple[dict[str, Any], dict[str, Any]]:
    now = "2026-10-14T00:00:00Z"
    model = {"provider": "openai_compatible", "model": "draft-example", "api_key": "example-only"}
    models = {"stage": model, "review": model | {"model": "review-example"}}
    ref = {
        "artifact_id": "a1",
        "schema": "i2h.hypotheses/v1",
        "version": 1,
        "sha256": "0" * 64,
        "download_url": "http://platform.test/internal/artifacts/a1",
        "primary": True,
    }
    anchor = {"artifact_id": "a1", "locator": "hypothesis:H1/falsification_criteria"}
    finding = {
        "finding_id": "F1-1",
        "anchor": anchor,
        "severity": "major",
        "kind": "fix",
        "category": "baseline",
        "issue": "Describe the baseline",
        "author": "researcher",
    }
    gate = {
        "gate_id": "g1",
        "kind": "artifact_review",
        "step_key": "work",
        "prompt": "Review result",
        "artifact_ids": ["a1"],
        "opened_at": now,
    }
    request = {
        "contract_version": c.CONTRACT_VERSION,
        "platform_run_id": "r1",
        "stage_run_id": "s1",
        "stage_key": "hypothesis",
        "interaction_mode": "light",
        "models": models,
        "budget_usd": "10",
        "callback_url": "http://platform.test/api/v1/internal/engines/i2h/runs/s1",
        "inputs": {"params": {"topic": "Compare retrieval methods for research", "domains": []}},
    }
    activity = {
        "source_seq": 1,
        "type": "i2h.screen.scored",
        "class": "activity",
        "phase": "drafting",
        "step_key": "screen",
        "actor": "screener",
        "summary": "Scored one paper",
        "occurred_at": now,
    }
    llm = {
        "role": "review",
        "provider": "openai_compatible",
        "model": "review-example",
        "input_tokens": 10,
        "output_tokens": 2,
        "latency_ms": 1,
        "outcome": "ok",
    }
    telemetry = {
        "occurred_at": now,
        "level": "INFO",
        "message": "Review finished",
        "platform_run_id": "r1",
        "stage_run_id": "s1",
        "engine_run_id": "e1",
        "phase": "reviewing",
        "llm": llm,
    }
    delivered = {"artifact_id": "a1", "schema": "i2h.hypotheses/v1", "version": 1, "primary": True}
    samples = {
        "Anchor": anchor,
        "Finding": finding,
        "ReviewFindings": {
            "round": 1,
            "reviewed_artifact_id": "a1",
            "reviewer_model": "review-example",
            "findings": [finding],
            "passed": True,
        },
        "RevisionItem": {"finding_id": "F1-1", "outcome": "fixed"},
        "RevisionResponse": {
            "round": 1,
            "artifact_before": "a1",
            "artifact_after": "a2",
            "items": [{"finding_id": "F1-1", "outcome": "fixed"}],
        },
        "RevisionDiff": {
            "round": 1,
            "artifact_before": "a1",
            "artifact_after": "a2",
            "patch": "-old\n+new",
        },
        "DatasetDescriptor": {
            "dataset_version_id": "d1",
            "version_number": 1,
            "original_filename": "data.csv",
            "sha256": "0" * 64,
            "source_type": "upload",
            "download_url": "http://platform.test/data/d1",
        },
        "ResearchProfile": {},
        "PipelineRecommendation": {
            "action": "proceed",
            "selection": {"hypothesis_ids": ["H1", "H2"]},
            "reason": "Review passed",
            "outcome": "inconclusive",
        },
        "PipelineFeedback": {"source_stage_key": "experiment_plan", "note": "Revisit baseline"},
        "TokenUsage": {"input_tokens": 10, "output_tokens": 2},
        "RunCost": {"cost_usd": None, "usage": {"stage": {"input_tokens": 10, "output_tokens": 2}}},
        "PhaseChanged": {"from": None},
        "StepChanged": {"from": None},
        "GateClosed": {"gate_id": "g1", "action": "approve", "actor": "user"},
        "ReviewCompleted": {"round": 1, "passed": True, "artifact_id": "review1"},
        "ArtifactDelivered": delivered,
        "RunPublished": {"artifact_id": "a1"},
        "EmptyPayload": {},
        "EventEnvelope": activity,
        "EventBatch": {"events": [activity]},
        "EventPage": {"events": []},
        "ModelConfig": model,
        "Models": models,
        "ArtifactRef": ref,
        "Decisions": {
            "selection": {"hypothesis_ids": ["H1", "H2"]},
            "actor": "user",
            "decided_by": "u1",
        },
        "Inputs": {"artifacts": [ref]},
        "ReviewPolicy": {"max_auto_revisions": 2},
        "RunCreateRequest": request,
        "RunCreateResponse": {"engine_run_id": "e1", "phase": "drafting", "created": True},
        "GateOption": {"option_id": "yes", "label": "Yes"},
        "Gate": gate,
        "GateAnswer": {"action": "approve", "actor": "user", "decided_by": "u1"},
        "FailureInfo": {
            "code": "engine_restarted",
            "message": "Engine restarted before completion",
        },
        "RunState": {
            "engine_run_id": "e1",
            "stage_run_id": "s1",
            "phase": "awaiting_researcher",
            "step_key": "work",
            "waiting_input": True,
            "open_gate": gate,
        },
        "StepInfo": {"step_key": "work", "label": "Work", "order": 1},
        "OutputDecl": {"schema": "i2h.hypotheses/v1", "primary": True},
        "Capabilities": {
            "engine_key": "i2h",
            "engine_version": "example",
            "contract_version": c.CONTRACT_VERSION,
            "stage_keys": ["hypothesis"],
            "output_schemas": [{"schema": "i2h.hypotheses/v1", "primary": True}],
            "steps": [{"step_key": "work", "label": "Work", "order": 1}],
            "activity_types": ["i2h.screen.scored"],
            "auto_revision": False,
            "profiles": [{}],
        },
        "Health": {
            "status": "ok",
            "engine_version": "example",
            "contract_version": c.CONTRACT_VERSION,
            "active_runs": 0,
        },
        "LlmCall": llm,
        "TelemetryRecord": telemetry,
        "TelemetryBatch": {"records": [telemetry]},
        "ArtifactMeta": {
            "schema": "i2h.hypotheses/v1",
            "version": 1,
            "primary": True,
            "filename": "hypotheses.json",
            "media_type": "application/json",
            "sha256": "0" * 64,
        },
    }
    payload_names = {
        "run.phase_changed": "PhaseChanged",
        "run.step_changed": "StepChanged",
        "run.gate_opened": "Gate",
        "run.gate_closed": "GateClosed",
        "review.completed": "ReviewCompleted",
        "artifact.delivered": "ArtifactDelivered",
        "run.published": "RunPublished",
        "run.failed": "FailureInfo",
        "run.cancelled": "EmptyPayload",
        "run.cost": "RunCost",
    }
    events = {activity["type"]: activity}
    for kind, name in payload_names.items():
        phase = {
            "run.published": "published",
            "run.failed": "failed",
            "run.cancelled": "cancelled",
            "run.gate_opened": "awaiting_researcher",
        }.get(kind, "drafting")
        events[kind] = dict(
            source_seq=1,
            type=kind,
            event_class="lifecycle",
            phase=phase,
            step_key="work",
            payload=samples[name],
            occurred_at=now,
        )
    return samples, events


_PAYLOADS = {
    "run.phase_changed": PhaseChanged,
    "run.step_changed": StepChanged,
    "run.gate_opened": c.Gate,
    "run.gate_closed": c.GateClosed,
    "review.completed": c.ReviewCompleted,
    "artifact.delivered": c.ArtifactDelivered,
    "run.published": c.RunPublished,
    "run.failed": c.FailureInfo,
    "run.cancelled": EmptyPayload,
    "run.cost": c.RunCost,
}


def wire_schema(model: type[c.ContractModel]) -> dict[str, Any]:
    schema = model.model_json_schema(by_alias=True)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"urn:research-contract:{c.CONTRACT_VERSION}:{model.__name__}"
    envelope = schema if model is c.EventEnvelope else schema.get("$defs", {}).get("EventEnvelope")
    if envelope is not None:
        definitions = schema.setdefault("$defs", {})
        rules = []
        for kind, payload_model in _PAYLOADS.items():
            payload_schema = payload_model.model_json_schema(by_alias=True)
            definitions.update(payload_schema.pop("$defs", {}))
            definitions[payload_model.__name__] = payload_schema
            then = {
                "properties": {"payload": {"$ref": f"#/$defs/{payload_model.__name__}"}},
                "required": ["payload"],
            }
            if kind in {"run.phase_changed", "run.gate_opened"}:
                then["required"].append("phase")
                then["properties"]["phase"] = {"$ref": "#/$defs/Phase"}
            if kind == "run.step_changed":
                then["required"].append("step_key")
                then["properties"]["step_key"] = {"type": "string", "minLength": 1}
            if kind in {"run.published", "run.failed", "run.cancelled"}:
                then["required"].append("phase")
                then["properties"]["phase"] = {"const": kind.split(".")[1]}
            rules.append(
                {
                    "if": {"properties": {"type": {"const": kind}}, "required": ["type"]},
                    "then": then,
                }
            )
        envelope["allOf"] = rules
    return schema


def render_contract() -> dict[str, str]:
    samples, events = example_payloads()
    if set(samples) != set(WIRE_MODELS):
        raise ValueError(f"Example/model mismatch: {set(samples) ^ set(WIRE_MODELS)}")
    output = {}

    def render(value):
        return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    for name, model in sorted(WIRE_MODELS.items()):
        example = model.model_validate(samples[name]).model_dump(mode="json", by_alias=True)
        output[f"schema/{name}.json"] = render(wire_schema(model))
        output[f"examples/{name}.json"] = render(example)
    for kind, event in sorted(events.items()):
        output[f"examples/event-{kind}.json"] = render(
            c.EventEnvelope.model_validate(event).model_dump(mode="json", by_alias=True)
        )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Check generated files without writing"
    )
    args = parser.parse_args()
    output = render_contract()
    existing = {
        p.relative_to(DOCS).as_posix()
        for directory in ("schema", "examples")
        for p in (DOCS / directory).glob("*.json")
    }
    if args.check:
        stale = existing != set(output) or any(
            not (DOCS / name).exists() or (DOCS / name).read_text(encoding="utf-8") != content
            for name, content in output.items()
        )
        if stale:
            parser.exit(
                1, "Contract export is stale; run: uv run python scripts/export_contract.py\n"
            )
    else:
        for name in existing - set(output):
            (DOCS / name).unlink()
        for name, content in output.items():
            target = DOCS / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
    print(f"Contract {c.CONTRACT_VERSION}: {len(WIRE_MODELS)} models, {len(output)} files")


if __name__ == "__main__":
    main()
