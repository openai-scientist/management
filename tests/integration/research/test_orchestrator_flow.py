from decimal import Decimal
from uuid import uuid4

import pytest

from management.core.errors import APIError
from management.research.models import RunArtifact
from management.research.orchestrator import accept_stage, apply_engine_event
from tests.helpers.research import lifecycle, open_pipeline


@pytest.fixture
async def pipeline(tmp_path):
    async with open_pipeline(tmp_path) as state:
        yield state


async def publish(db, run, stage, schema="i2h.hypotheses/v1"):
    artifact = RunArtifact(
        id=uuid4(),
        run_id=run.id,
        stage_run_id=stage.id,
        schema_id=schema,
        version=1,
        is_primary=True,
        kind="other",
        filename="result.json",
        content_type="application/json",
        storage_key=uuid4().hex,
        size_bytes=2,
        sha256="0" * 64,
    )
    db.add(artifact)
    await db.flush()
    await apply_engine_event(
        db,
        stage,
        lifecycle("run.published", phase="published", payload={"artifact_id": str(artifact.id)}),
    )
    return artifact


async def test_start_creates_first_stage_with_request(pipeline):
    _, _, run, stage, fakes = pipeline
    req = fakes["i2h"].requests[0]
    assert stage.status == "RUNNING" and req.stage_key == "hypothesis"
    assert req.models.stage.identity != req.models.review.identity
    assert req.inputs.params == {"topic": run.topic, "domains": ["ml"]}
    assert req.inputs.prior_engine_run_id is None
    assert "model-test-secret" not in str(stage.config_snapshot)


async def test_schema_mismatch_rejected(pipeline):
    harness, db, run, stage, _ = pipeline
    await publish(db, run, stage, schema="other.output/v1")
    with pytest.raises(APIError) as info:
        await accept_stage(
            db, harness.app.state, stage, selection={}, note=None, actor="user", user_id=None
        )
    assert info.value.code == "SCHEMA_MISMATCH"
    assert stage.status == "COMPLETED"


async def test_failed_event_marks_failed(pipeline):
    _, db, run, stage, _ = pipeline
    await apply_engine_event(
        db,
        stage,
        lifecycle(
            "run.failed",
            phase="failed",
            payload={"code": "engine_restarted", "message": "Restarted"},
        ),
    )
    assert stage.status == "FAILED" and run.status == "failed"
    assert stage.failure["code"] == "engine_restarted"
    assert run.failure_message == "Restarted"


async def test_two_cost_events_do_not_double_count(pipeline):
    _, db, run, stage, _ = pipeline
    for cost in ("0.25", "0.50"):
        await apply_engine_event(db, stage, lifecycle("run.cost", payload={"cost_usd": cost}))
    assert float(stage.cost_usd) == 0.5 and float(run.cost_usd) == 0.5


async def test_cost_priced_from_tokens_and_missing_price_keeps_prior_cost(pipeline):
    _, db, run, stage, _ = pipeline
    event = lifecycle(
        "run.cost",
        payload={
            "cost_usd": None,
            "usage": {
                "stage": {
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                }
            },
        },
    )
    await apply_engine_event(db, stage, event)
    assert stage.cost_usd == Decimal("3") and not run.cost_unknown
    stage.config_snapshot = stage.config_snapshot | {
        "pricing": {"stage": {"input": None, "output": None}}
    }
    await apply_engine_event(db, stage, event)
    assert stage.cost_usd == Decimal("3") and run.cost_unknown


async def accept_first_stage_with_spend(pipeline, enforced):
    harness, db, run, stage, fakes = pipeline
    harness.app.state.settings.run_budget_enforced = enforced
    await publish(db, run, stage)
    run.cost_usd = run.budget_usd
    run.cost_unknown = False
    await accept_stage(
        db, harness.app.state, stage, selection={}, note=None, actor="user", user_id=None
    )
    return run, fakes


async def test_budget_not_enforced_sends_large_cap(pipeline):
    run, fakes = await accept_first_stage_with_spend(pipeline, False)
    requests = [r for fake in fakes.values() for r in fake.requests]
    assert run.status != "budget_exceeded"
    assert len(requests) == 2 and requests[-1].budget_usd == Decimal("2000")


async def test_budget_is_enforced_by_default_once_every_price_is_known(pipeline):
    run, fakes = await accept_first_stage_with_spend(pipeline, None)
    assert run.status == "budget_exceeded"
    assert sum(len(fake.requests) for fake in fakes.values()) == 1


async def test_budget_enforced_pauses_when_spent(pipeline):
    run, fakes = await accept_first_stage_with_spend(pipeline, True)
    assert run.status == "budget_exceeded"
    assert sum(len(fake.requests) for fake in fakes.values()) == 1
