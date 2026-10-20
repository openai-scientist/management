from decimal import Decimal

import pytest
from sqlalchemy import select

from management.audit.models import AuditEvent
from management.core.errors import APIError
from management.research.orchestrator import accept_stage, apply_engine_event
from tests.helpers.research import lifecycle, open_pipeline, publish


@pytest.fixture
async def pipeline(tmp_path):
    async with open_pipeline(tmp_path) as state:
        yield state


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


async def test_budget_enforced_pauses_when_spent(pipeline):
    run, fakes = await accept_first_stage_with_spend(pipeline, True)
    assert run.status == "budget_exceeded"
    assert sum(len(fake.requests) for fake in fakes.values()) == 1


async def test_stage_decisions_and_run_endings_reach_the_audit_log(pipeline):
    harness, db, run, stage, _ = pipeline
    await publish(db, run, stage)
    await accept_stage(
        db, harness.app.state, stage, selection={}, note=None, actor="user", user_id=None
    )
    await db.flush()
    rows = (
        await db.scalars(
            select(AuditEvent)
            .where(AuditEvent.resource_id == str(run.id))
            .order_by(AuditEvent.created_at)
        )
    ).all()
    decided = [row for row in rows if row.action == "run.decision_recorded"]
    assert [row.details["decision"] for row in decided] == ["accept"]
    assert decided[0].project_id == run.project_id
    # Waiting for review is not an ending: only the decision is recorded.
    assert not [row for row in rows if row.action == "run.status_changed"]


async def test_a_failed_stage_records_the_run_ending(pipeline):
    _, db, run, stage, _ = pipeline
    await apply_engine_event(
        db, stage, lifecycle("run.failed", phase="failed", payload={"code": "x", "message": "m"})
    )
    await db.flush()
    ending = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_id == str(run.id), AuditEvent.action == "run.status_changed"
        )
    )
    assert ending.details["after"] == "failed" and ending.actor_user_id is None
