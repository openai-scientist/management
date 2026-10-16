from uuid import UUID

import pytest
from sqlalchemy import select

from management.research.contracts import GateAnswer
from management.research.models import ResearchRun, RunEvent, StageDecision, StageRun
from management.research.orchestrator import (
    accept_stage,
    answer_stage_gate,
    send_back,
    stop_research_run,
)
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.helpers.harness import open_harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, create_project


@pytest.fixture
async def api(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        async for state in _open(harness):
            yield state


@pytest.fixture
async def api_one_iteration(tmp_path):
    async with open_harness(tmp_path, **engine_overrides(), max_iterations=1) as harness:
        async for state in _open(harness):
            yield state


async def _open(harness):
    fakes = install_fake_engines(harness)
    for fake in fakes.values():
        fake.scripted()
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        await seed_models(harness, session["user"]["id"])
        yield harness, client, session, project, fakes


def csrf(session):
    return mutation_headers(session["csrf_token"])


def run_url(project, run, tail=""):
    return f"{PROJECTS}/{project['id']}/runs/{run['id']}{tail}"


async def current(client, project, run):
    return (await client.get(run_url(project, run))).json()["data"]["current_stage"]


async def approve_and_accept(client, session, project, run):
    stage = await current(client, project, run)
    gate = stage["open_gate"]["gate_id"]
    base = run_url(project, run, f"/stages/{stage['stage_run_id']}")
    approved = await client.post(
        f"{base}/gates/{gate}", json={"action": "approve"}, headers=csrf(session)
    )
    assert approved.status_code == 200, approved.text
    body = {"selection": {"hypothesis_ids": ["H1"]}} if stage["stage_key"] == "hypothesis" else {}
    accepted = await client.post(f"{base}/accept", json=body, headers=csrf(session))
    assert accepted.status_code == 200, accepted.text
    return stage


async def begin(client, session, project):
    response = await client.post(
        f"{PROJECTS}/{project['id']}/runs",
        json={
            "topic": "Compare two retrieval methods",
            "domains": ["ml"],
            "interaction_mode": "light",
        },
        headers=csrf(session),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def exd_completed(client, session, project):
    """Hypothesis accepted, experiment plan approved and left COMPLETED."""
    run = await begin(client, session, project)
    await approve_and_accept(client, session, project, run)
    stage = await current(client, project, run)
    assert stage["stage_key"] == "experiment_plan"
    gate = stage["open_gate"]["gate_id"]
    base = run_url(project, run, f"/stages/{stage['stage_run_id']}")
    approved = await client.post(
        f"{base}/gates/{gate}", json={"action": "approve"}, headers=csrf(session)
    )
    assert approved.status_code == 200, approved.text
    return run, stage, base


async def statuses(client, project, run):
    rows = (await client.get(run_url(project, run, "/stages"))).json()["data"]
    return [(r["stage_key"], r["iteration"], r["status"]) for r in rows]


async def test_send_back_creates_iteration_2(api):
    harness, client, session, project, fakes = api
    run, exd, base = await exd_completed(client, session, project)

    response = await client.post(
        f"{base}/send-back",
        json={"target_stage_key": "hypothesis", "note": "Too broad", "artifact_ids": []},
        headers=csrf(session),
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "SUPERSEDED"
    assert await statuses(client, project, run) == [
        ("hypothesis", 1, "SUPERSEDED"),
        ("experiment_plan", 1, "SUPERSEDED"),
        ("hypothesis", 2, "RUNNING"),
    ]
    assert (await client.get(run_url(project, run))).json()["data"]["status"] == "awaiting_review"

    request = fakes["i2h"].requests[1]
    assert request.iteration == 2
    assert [(f.source_stage_key, f.note) for f in request.inputs.feedback] == [
        ("experiment_plan", "Too broad")
    ]
    assert len(request.inputs.history) == 1 and request.inputs.history[0].primary
    assert request.inputs.params["topic"] == "Compare two retrieval methods"
    assert request.inputs.prior_engine_run_id is None

    async with harness.factory() as db:
        decision = await db.scalar(select(StageDecision).where(StageDecision.action == "send_back"))
        assert decision.actor == "user" and decision.payload["target_stage_key"] == "hypothesis"
        new = await db.scalar(select(StageRun).where(StageRun.iteration == 2))
        assert new.origin_decision_id == decision.id
        assert (await db.scalar(select(ResearchRun))).iteration == 2
        types = (await db.scalars(select(RunEvent.type))).all()
        assert "iteration.started" in types

    await approve_and_accept(client, session, project, run)
    assert len(fakes["exd"].requests) == 2 and fakes["exd"].requests[1].iteration == 2
    assert (await current(client, project, run))["stage_key"] == "experiment_plan"


async def test_send_back_reuses_prior_run_when_engine_declares_it(api):
    _, client, session, project, fakes = api
    fakes["i2h"].capabilities.reuses_prior_run = True
    run, _, base = await exd_completed(client, session, project)
    first_engine_run = next(iter(fakes["i2h"].states))
    sent = await client.post(
        f"{base}/send-back",
        json={"target_stage_key": "hypothesis", "note": "again"},
        headers=csrf(session),
    )
    assert sent.status_code == 200, sent.text
    assert fakes["i2h"].requests[1].inputs.prior_engine_run_id == first_engine_run


async def test_refine_same_stage(api):
    harness, client, session, project, fakes = api
    run, exd, base = await exd_completed(client, session, project)
    response = await client.post(
        f"{base}/send-back",
        json={"target_stage_key": "experiment_plan", "note": "Add a control"},
        headers=csrf(session),
    )
    assert response.status_code == 200, response.text
    assert await statuses(client, project, run) == [
        ("hypothesis", 1, "ACCEPTED"),
        ("experiment_plan", 1, "SUPERSEDED"),
        ("experiment_plan", 2, "RUNNING"),
    ]
    first, second = fakes["exd"].requests
    assert second.inputs.artifacts == first.inputs.artifacts
    assert second.inputs.decisions.selection == {"hypothesis_ids": ["H1"]}
    assert second.inputs.feedback[0].note == "Add a control"
    assert len(second.inputs.history) == 1


async def test_undeclared_edge_rejected(api):
    _, client, session, project, _ = api
    run, _, base = await exd_completed(client, session, project)
    response = await client.post(
        f"{base}/send-back",
        json={"target_stage_key": "nowhere", "note": "x"},
        headers=csrf(session),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "LOOP_BACK_NOT_DECLARED"
    assert (await current(client, project, run))["status"] == "COMPLETED"


async def test_send_back_needs_completed_stage(api):
    _, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = await current(client, project, run)
    response = await client.post(
        run_url(project, run, f"/stages/{stage['stage_run_id']}/send-back"),
        json={"target_stage_key": "hypothesis", "note": "x"},
        headers=csrf(session),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STAGE_STATE_CONFLICT"


async def test_max_iterations_rejected(api_one_iteration):
    _, client, session, project, _ = api_one_iteration
    run, _, base = await exd_completed(client, session, project)
    response = await client.post(
        f"{base}/send-back",
        json={"target_stage_key": "hypothesis", "note": "x"},
        headers=csrf(session),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MAX_ITERATIONS_REACHED"


async def test_rerun_after_failed(api):
    harness, client, session, project, fakes = api
    run = await begin(client, session, project)
    stage = await current(client, project, run)
    async with harness.factory() as db:
        row = await db.get(StageRun, UUID(stage["stage_run_id"]))
        engine_run_id = row.engine_run_id
    await fakes["i2h"].fail(engine_run_id, "internal_error", "Failed")
    assert (await current(client, project, run))["status"] == "FAILED"

    response = await client.post(
        run_url(project, run, f"/stages/{stage['stage_run_id']}/rerun"),
        json={"reason": "transient"},
        headers=csrf(session),
    )
    assert response.status_code == 200, response.text
    assert await statuses(client, project, run) == [
        ("hypothesis", 1, "FAILED"),
        ("hypothesis", 1, "RUNNING"),
    ]
    first, second = fakes["i2h"].requests
    assert second.inputs == first.inputs and second.iteration == first.iteration
    data = (await client.get(run_url(project, run))).json()["data"]
    assert data["status"] == "awaiting_review"
    async with harness.factory() as db:
        decision = await db.scalar(select(StageDecision).where(StageDecision.action == "rerun"))
        assert decision.reason == "transient" and decision.actor == "user"

    again = await client.post(
        run_url(project, run, f"/stages/{stage['stage_run_id']}/rerun"),
        json={},
        headers=csrf(session),
    )
    assert again.status_code == 409


async def test_rerun_when_not_failed_409(api):
    _, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = await current(client, project, run)
    response = await client.post(
        run_url(project, run, f"/stages/{stage['stage_run_id']}/rerun"),
        json={},
        headers=csrf(session),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "STAGE_STATE_CONFLICT"


async def test_stop_cancels_engine(api):
    harness, client, session, project, fakes = api
    run = await begin(client, session, project)
    response = await client.post(
        run_url(project, run, "/stop"), json={"reason": "enough"}, headers=csrf(session)
    )
    assert response.status_code == 200, response.text
    assert (await current(client, project, run))["status"] == "STOPPED"
    assert (await client.get(run_url(project, run))).json()["data"]["status"] == "stopped"
    assert all(state.phase == "cancelled" for state in fakes["i2h"].states.values())
    async with harness.factory() as db:
        decision = await db.scalar(select(StageDecision).where(StageDecision.action == "stop"))
        assert decision.reason == "enough"
    again = await client.post(run_url(project, run, "/stop"), json={}, headers=csrf(session))
    assert again.status_code == 409


async def test_pi_actor_recorded(api):
    harness, client, session, project, fakes = api
    run = await begin(client, session, project)
    stage = await current(client, project, run)
    state = harness.app.state

    async with harness.factory() as db:
        row = await db.get(StageRun, UUID(stage["stage_run_id"]))
        decision = await answer_stage_gate(
            db,
            state,
            row,
            GateAnswer(action="approve", actor="user"),
            actor="pi",
            user_id=None,
            reason="Findings are minor",
            decided_by="pi:c-1",
        )
        await db.commit()
        assert decision.actor == "pi" and decision.decided_by_user_id is None
        assert decision.reason == "Findings are minor" and decision.status == "applied"
        sent = next(iter(fakes["i2h"]._answers.values()))
        assert sent.actor == "system" and sent.decided_by == "pi:c-1"

    async with harness.factory() as db:
        row = await db.get(StageRun, UUID(stage["stage_run_id"]))
        assert row.status == "COMPLETED"
        await accept_stage(
            db,
            state,
            row,
            selection={"hypothesis_ids": ["H1"]},
            note="handoff",
            actor="pi",
            user_id=None,
            reason="Best H1",
            decided_by="pi:c-2",
        )
        await db.commit()
    request = fakes["exd"].requests[0]
    assert request.inputs.decisions.actor == "system"
    assert request.inputs.decisions.decided_by == "pi:c-2"
    assert request.inputs.decisions.note == "handoff"

    async with harness.factory() as db:
        exd = await db.scalar(select(StageRun).where(StageRun.stage_key == "experiment_plan"))
        gate = exd.open_gate["gate_id"]
        await answer_stage_gate(
            db,
            state,
            exd,
            GateAnswer(action="approve", actor="user"),
            actor="pi",
            user_id=None,
        )
        await db.commit()
        exd = await db.get(StageRun, exd.id, populate_existing=True)
        assert exd.status == "COMPLETED"
        sent = fakes["exd"]._answers[(exd.engine_run_id, gate)]
        assert sent.actor == "system" and sent.decided_by == "pi"

        sent_back = await send_back(
            db,
            state,
            exd,
            target_stage_key="experiment_plan",
            note="refine",
            artifact_ids=[],
            actor="pi",
            user_id=None,
            reason="Needs control",
            decided_by="pi:c-3",
        )
        await db.commit()
        assert sent_back.actor == "pi" and sent_back.decided_by_user_id is None
        assert sent_back.action == "send_back" and sent_back.reason == "Needs control"

        newest = await db.scalar(select(StageRun).where(StageRun.iteration == 2))
        assert newest.stage_key == "experiment_plan"
        await stop_research_run(
            db,
            state,
            await db.get(ResearchRun, newest.research_run_id),
            actor="pi",
            user_id=None,
            reason="done",
        )
        await db.commit()
        decisions = (await db.scalars(select(StageDecision))).all()
        assert {d.actor for d in decisions if d.action in {"send_back", "stop"}} == {"pi"}
        assert (await db.get(StageRun, newest.id, populate_existing=True)).status == "STOPPED"
        assert (
            await db.get(ResearchRun, newest.research_run_id, populate_existing=True)
        ).status == ("stopped")
