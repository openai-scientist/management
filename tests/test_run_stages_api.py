from uuid import UUID

import pytest
from sqlalchemy import select

from management.models import StageDecision
from tests.conftest import login, mutation_headers, open_harness
from tests.engine_fakes import engine_overrides, install_fake_engines, seed_models
from tests.test_projects_api import PROJECTS, add_member, create_project
from tests.test_runs_api import start_run


@pytest.fixture
async def api(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
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


async def begin(client, session, project, **more):
    response = await client.post(
        f"{PROJECTS}/{project['id']}/runs",
        json={
            "topic": "Compare two retrieval methods",
            "domains": ["ml"],
            "interaction_mode": "light",
            **more,
        },
        headers=csrf(session),
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def stage_url(project, run, stage="", tail=""):
    return f"{PROJECTS}/{project['id']}/runs/{run['id']}/stages" + (
        f"/{stage}{tail}" if stage else ""
    )


async def approve(client, session, project, run, stage):
    gate = stage["open_gate"]["gate_id"]
    return await client.post(
        stage_url(project, run, stage["stage_run_id"], f"/gates/{gate}"),
        json={"action": "approve"},
        headers=csrf(session),
    )


async def get_run(client, project, run):
    return (await client.get(f"{PROJECTS}/{project['id']}/runs/{run['id']}")).json()["data"]


async def test_s1_flow_light_mode(api):
    harness, client, session, project, fakes = api
    run = await begin(client, session, project)
    assert run["pipeline_key"] == "default" and run["interaction_mode"] == "light"
    stage = run["current_stage"]
    assert stage["stage_key"] == "hypothesis" and stage["open_gate"]["kind"] == "artifact_review"
    assert run["status"] == "awaiting_review"

    assert (await approve(client, session, project, run, stage)).status_code == 200
    run = await get_run(client, project, run)
    assert run["current_stage"]["status"] == "COMPLETED"
    accepted = await client.post(
        stage_url(project, run, stage["stage_run_id"], "/accept"),
        json={"selection": {"hypothesis_ids": ["H1", "H2"]}, "note": "both"},
        headers=csrf(session),
    )
    assert accepted.status_code == 200, accepted.text
    request = fakes["exd"].requests[0]
    assert request.inputs.artifacts[0].schema_id == "i2h.hypotheses/v1"
    assert request.inputs.decisions.selection == {"hypothesis_ids": ["H1", "H2"]}

    run = await get_run(client, project, run)
    second = run["current_stage"]
    assert second["stage_key"] == "experiment_plan" and second["open_gate"]
    assert (await approve(client, session, project, run, second)).status_code == 200
    done = await client.post(
        stage_url(project, run, second["stage_run_id"], "/accept"),
        json={},
        headers=csrf(session),
    )
    assert done.status_code == 200 and done.json()["data"]["next_stage_run_id"] is None
    assert (await get_run(client, project, run))["status"] == "completed"

    stages = (await client.get(stage_url(project, run))).json()["data"]
    assert [s["status"] for s in stages] == ["ACCEPTED", "ACCEPTED"]
    assert all("config_snapshot" not in s for s in stages)
    only = await client.get(stage_url(project, run) + "?iteration=2")
    assert only.json()["data"] == []
    first = stages[0]["stage_run_id"]
    base = f"{PROJECTS}/{project['id']}/runs/{run['id']}/artifacts"
    filtered = (
        await client.get(f"{base}?stage_run_id={first}&schema=i2h.hypotheses/v1&version=1")
    ).json()["data"]
    assert filtered and all(a["stage_run_id"] == first for a in filtered)
    assert (await client.get(f"{base}?schema=other/v1")).json()["data"] == []
    async with harness.factory() as db:
        actions = [d.action for d in await db.scalars(select(StageDecision))]
    assert sorted(actions) == ["accept", "accept", "approve", "approve"]


async def test_legacy_path_when_no_engines(harness):
    async with harness.client() as client:
        session = await login(harness, client, uid="owner", email="owner@example.com")
        project = await create_project(client, session)
        response = await start_run(client, session, project["id"], "")
    assert harness.app.state.settings.engines == {}
    assert response.status_code in {201, 202, 503}
    if response.status_code != 503:
        assert response.json()["data"]["pipeline_key"] is None


async def test_gate_not_open_409(api):
    _, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = run["current_stage"]
    wrong = await client.post(
        stage_url(project, run, stage["stage_run_id"], "/gates/nope"),
        json={"action": "approve"},
        headers=csrf(session),
    )
    assert wrong.status_code == 409 and wrong.json()["error"]["code"] == "GATE_NOT_OPEN"
    assert (await approve(client, session, project, run, stage)).status_code == 200
    assert (await approve(client, session, project, run, stage)).status_code == 200


async def test_accept_while_running_and_invalid_selection(api):
    _, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = run["current_stage"]
    url = stage_url(project, run, stage["stage_run_id"], "/accept")
    early = await client.post(url, json={}, headers=csrf(session))
    assert early.status_code == 409
    assert (await approve(client, session, project, run, stage)).status_code == 200
    for selection in ({}, {"hypothesis_ids": []}, {"hypothesis_ids": ["H9"]}):
        bad = await client.post(url, json={"selection": selection}, headers=csrf(session))
        assert bad.status_code == 422, bad.text
        assert bad.json()["error"]["code"] == "INVALID_SELECTION"
    assert (await get_run(client, project, run))["current_stage"]["status"] == "COMPLETED"


async def test_viewer_cannot_accept(api):
    harness, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = run["current_stage"]
    async with harness.client() as viewer_client:
        viewer = await login(harness, viewer_client, uid="viewer", email="viewer@example.com")
        await add_member(client, session, project["id"], "viewer@example.com", "reviewer")
        assert (await viewer_client.get(stage_url(project, run))).status_code == 200
        assert (await approve(viewer_client, viewer, project, run, stage)).status_code == 403
        accept = await viewer_client.post(
            stage_url(project, run, stage["stage_run_id"], "/accept"),
            json={},
            headers=csrf(viewer),
        )
        assert accept.status_code == 403


async def test_client_cannot_set_actor_system(api):
    harness, client, session, project, _ = api
    run = await begin(client, session, project)
    stage = run["current_stage"]
    gate = stage["open_gate"]["gate_id"]
    response = await client.post(
        stage_url(project, run, stage["stage_run_id"], f"/gates/{gate}"),
        json={"action": "approve", "actor": "system", "decided_by": "someone"},
        headers=csrf(session),
    )
    assert response.status_code == 422
    async with harness.factory() as db:
        assert (await db.scalars(select(StageDecision))).all() == []
    assert (await approve(client, session, project, run, stage)).status_code == 200
    async with harness.factory() as db:
        decision = (await db.scalars(select(StageDecision))).one()
    assert decision.actor == "user" and decision.decided_by_user_id == UUID(session["user"]["id"])
