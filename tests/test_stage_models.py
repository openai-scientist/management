from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from management.research.models import ResearchRun, RunArtifact, RunEvent, RunGate, StageRun
from management.research.stage_runs import sync_run_status
from tests.conftest import login, mutation_headers
from tests.test_projects_api import create_project


@pytest.fixture
async def stage_data(harness):
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
    async with harness.factory() as db:
        run = ResearchRun(
            project_id=UUID(project["id"]),
            created_by_user_id=UUID(session["user"]["id"]),
            topic="Compare two retrieval methods",
            status="running",
            budget_usd=Decimal("5"),
            pipeline_key="default",
            current_stage_key="hypothesis",
            interaction_mode="light",
        )
        db.add(run)
        await db.flush()
        stage = StageRun(
            research_run_id=run.id,
            iteration=1,
            stage_key="hypothesis",
            engine_key="i2h",
            status="RUNNING",
        )
        db.add(stage)
        await db.commit()
        yield db, run, stage


@pytest.mark.parametrize(
    "status,phase,gate,expected",
    [
        ("PENDING", None, None, "running"),
        ("RUNNING", "drafting", None, "running"),
        ("RUNNING", "drafting", {"gate_id": "g"}, "awaiting_review"),
        ("COMPLETED", "published", None, "awaiting_review"),
        ("RUNNING", "paused", None, "paused"),
        ("FAILED", "failed", None, "failed"),
        ("STOPPED", "cancelled", None, "stopped"),
        ("ACCEPTED", "published", None, "completed"),
    ],
)
async def test_sync_run_status(stage_data, status, phase, gate, expected):
    db, run, stage = stage_data
    stage.status, stage.phase, stage.open_gate = status, phase, gate
    await sync_run_status(db, run, stage=stage)
    assert run.status == expected


async def test_budget_pause_keeps_project_slot(stage_data):
    db, run, stage = stage_data
    stage.phase = "paused"
    run.status = "budget_exceeded"
    await sync_run_status(db, run, stage=stage)
    assert run.status == "budget_exceeded"
    db.add(
        ResearchRun(
            project_id=run.project_id,
            created_by_user_id=run.created_by_user_id,
            status="running",
            budget_usd=Decimal("5"),
        )
    )
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()


async def test_budget_pipeline_prevents_project_completion(stage_data, harness):
    db, run, stage = stage_data
    stage.phase = "paused"
    run.status = "budget_exceeded"
    await db.commit()
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        response = await client.post(
            f"/api/v1/projects/{run.project_id}/complete",
            headers=mutation_headers(session["csrf_token"]),
        )
    assert response.status_code == 409


async def test_two_stages_each_open_a_gate_and_source_sequences_reset(stage_data):
    db, run, stage = stage_data
    second = StageRun(
        research_run_id=run.id,
        iteration=1,
        stage_key="experiment_plan",
        engine_key="exd",
        status="RUNNING",
    )
    db.add(second)
    await db.flush()
    for seq, row in enumerate((stage, second), 1):
        db.add(
            RunGate(
                run_id=run.id,
                stage_run_id=row.id,
                gate_key=f"g{seq}",
                kind="artifact_review",
                spec={},
                opened_seq=seq,
            )
        )
        db.add(
            RunEvent(
                run_id=run.id,
                stage_run_id=row.id,
                seq=seq,
                source_seq=1,
                type="run.phase_changed",
                payload={},
            )
        )
    await db.flush()


async def test_failed_stage_closes_open_gate(stage_data):
    db, run, stage = stage_data
    gate = RunGate(
        run_id=run.id,
        stage_run_id=stage.id,
        gate_key="g",
        kind="artifact_review",
        spec={},
        opened_seq=1,
    )
    db.add(gate)
    await db.flush()
    stage.status = "FAILED"
    await sync_run_status(db, run, stage=stage)
    await db.refresh(gate)
    assert gate.answer == "closed"


async def test_artifact_versions_allow_same_filename_but_not_duplicate_version(stage_data):
    db, run, stage = stage_data
    fields = dict(
        run_id=run.id,
        stage_run_id=stage.id,
        schema_id="i2h.hypotheses/v1",
        kind="other",
        filename="result.json",
        content_type="application/json",
        size_bytes=2,
        sha256="0" * 64,
    )
    db.add_all(
        [
            RunArtifact(**fields, version=1, storage_key="one"),
            RunArtifact(**fields, version=2, storage_key="two"),
        ]
    )
    await db.flush()
    db.add(RunArtifact(**fields, version=1, storage_key="three"))
    with pytest.raises(IntegrityError):
        await db.flush()
    await db.rollback()
