from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from platform_be.contracts import EventEnvelope
from platform_be.core.errors import APIError
from platform_be.models import Project, RunArtifact, StageRun, User
from platform_be.services.orchestrator import accept_stage, apply_engine_event, start_research_run
from tests.conftest import login, open_harness
from tests.engine_fakes import engine_overrides, install_fake_engines, seed_models
from tests.test_projects_api import create_project


def lifecycle(kind, *, phase=None, payload=None, seq=1):
    return EventEnvelope(
        source_seq=seq,
        type=kind,
        event_class="lifecycle",
        phase=phase,
        payload=payload or {},
        occurred_at=datetime.now(UTC),
    )


@pytest.fixture
async def pipeline(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        fakes = install_fake_engines(harness)
        # Core state-machine checks precede the HTTP ingress task; replay events directly.
        for fake in fakes.values():
            fake.callback_transport = None
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            project_data = await create_project(client, session)
        await seed_models(harness, session["user"]["id"])
        async with harness.factory() as db:
            project = await db.get(Project, UUID(project_data["id"]))
            user = await db.get(User, UUID(session["user"]["id"]))
            run = await start_research_run(
                db,
                harness.app.state,
                project=project,
                principal=SimpleNamespace(user=user),
                topic="Compare two retrieval methods",
                interaction_mode="light",
                budget_usd=5,
                pipeline_key="default",
                domains=["ml"],
            )
            await db.commit()
            stage = await db.scalar(select(StageRun).where(StageRun.research_run_id == run.id))
            yield harness, db, run, stage, fakes


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


async def test_published_marks_completed_and_accept_starts_next_with_selection(pipeline):
    harness, db, run, stage, fakes = pipeline
    artifact = await publish(db, run, stage)
    assert stage.status == "COMPLETED" and run.status == "awaiting_review"
    next_stage = await accept_stage(
        db,
        harness.app.state,
        stage,
        selection={"hypothesis_ids": ["H1", "H2"]},
        note="Use both",
        actor="user",
        user_id=run.created_by_user_id,
    )
    assert stage.status == "ACCEPTED" and next_stage.stage_key == "experiment_plan"
    req = fakes["exd"].requests[0]
    assert req.inputs.artifacts[0].artifact_id == str(artifact.id)
    assert req.inputs.artifacts[0].schema_id == "i2h.hypotheses/v1"
    assert req.inputs.decisions.selection == {"hypothesis_ids": ["H1", "H2"]}
    assert "/exd/artifacts/" in req.inputs.artifacts[0].download_url
    await publish(db, run, next_stage, schema="exd.exp_plan/v1")
    assert (
        await accept_stage(
            db,
            harness.app.state,
            next_stage,
            selection={},
            note=None,
            actor="user",
            user_id=run.created_by_user_id,
        )
        is None
    )
    assert run.status == "completed"


async def test_accept_while_running_conflict(pipeline):
    harness, db, _, stage, _ = pipeline
    with pytest.raises(APIError) as info:
        await accept_stage(
            db, harness.app.state, stage, selection={}, note=None, actor="user", user_id=None
        )
    assert info.value.status_code == 409


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
