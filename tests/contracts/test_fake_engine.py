import json

import httpx
import pytest

from management.contracts import GateAnswer, Phase, RunCreateRequest, RunState
from tests.fakes.fake_engine import FakeEngine


@pytest.fixture
def fake_setup(request_data):
    deliveries = []

    async def callback(request):
        deliveries.append(request)
        if request.url.path.endswith("/artifacts"):
            return httpx.Response(201, json={"artifact_id": f"a{len(deliveries)}"})
        return httpx.Response(200, json={"accepted": 1, "skipped": 0})

    fake = FakeEngine(callback_transport=httpx.MockTransport(callback))
    request = RunCreateRequest(**request_data)
    return fake, request, deliveries


async def create(fake, request):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://engine.test"
    ) as client:
        response = await client.post(
            "/runs", json=request.to_wire(), headers={"X-Service-Key": fake.service_key}
        )
    assert response.status_code == 200, response.text
    return response.json()["engine_run_id"]


async def test_create_run_idempotent_by_stage_run_id(fake_setup):
    fake, request, _ = fake_setup
    first = await create(fake, request)
    assert await create(fake, request) == first
    assert len(fake.requests) == 1


async def test_scripted_flow_reaches_review_gate(fake_setup):
    fake, request, deliveries = fake_setup
    fake.scripted()
    engine_id = await create(fake, request)
    state = fake.states[engine_id]
    assert state.phase == Phase.awaiting_researcher
    assert state.open_gate.kind == "artifact_review"
    events = [
        e
        for d in deliveries
        if d.url.path.endswith("/events")
        for e in json.loads(d.content)["events"]
    ]
    assert [e["phase"] for e in events if e["type"] == "run.phase_changed"] == [
        "drafting",
        "reviewing",
        "awaiting_researcher",
    ]
    assert [e["source_seq"] for e in events] == list(range(1, len(events) + 1))
    assert (Phase.reviewing, "review") in fake.llm_calls


async def test_comment_loops_back_and_answer_idempotent(fake_setup):
    fake, request, _ = fake_setup
    fake.scripted()
    engine_id = await create(fake, request)
    gate = fake.states[engine_id].open_gate
    answer = GateAnswer(
        action="comment",
        actor="user",
        findings=[
            dict(
                finding_id="F1-1",
                anchor={"locator": "section:methods"},
                severity="major",
                issue="Clarify",
                author="researcher",
            )
        ],
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app),
        base_url="http://engine.test",
        headers={"X-Service-Key": fake.service_key},
    ) as client:
        path = f"/runs/{engine_id}/gates/{gate.gate_id}"
        response = await client.post(path, json=answer.model_dump(mode="json"))
        assert RunState(**response.json()).phase == Phase.awaiting_researcher
        count = len(fake.events[engine_id])
        assert (await client.post(path, json=answer.model_dump(mode="json"))).status_code == 200
        assert len(fake.events[engine_id]) == count
        different = await client.post(path, json={"action": "approve", "actor": "user"})
        assert different.status_code == 409
        assert different.json()["code"] == "GATE_NOT_OPEN"
    phases = [e.phase for e in fake.events[engine_id] if e.type == "run.phase_changed"]
    assert phases[-3:] == [Phase.revising, Phase.reviewing, Phase.awaiting_researcher]


async def test_unreachable_returns_503_and_health_details_follow_auth(fake_setup):
    fake, _, _ = fake_setup
    fake.set_health("degraded")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app), base_url="http://engine.test"
    ) as client:
        public = (await client.get("/health")).json()
        assert public["status"] == "degraded" and "checks" not in public
        assert public["engine_version"] == "test"
        private = (await client.get("/health", headers={"X-Service-Key": fake.service_key})).json()
        assert private["checks"]["storage_writable"]
        fake.set_unreachable(True)
        assert (await client.get("/health")).status_code == 503


async def test_pause_resume_and_validation_does_not_echo_secret(fake_setup):
    fake, request, _ = fake_setup
    engine_id = await create(fake, request)
    fake.scripted()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app),
        base_url="http://engine.test",
        headers={"X-Service-Key": fake.service_key},
    ) as client:
        assert (await client.post(f"/runs/{engine_id}/pause")).json()["phase"] == "paused"
        assert (await client.post(f"/runs/{engine_id}/resume")).json()[
            "phase"
        ] == "awaiting_researcher"
        bad = request.to_wire() | {"budget_usd": -1, "api_key": "secret-marker"}
        response = await client.post("/runs", json=bad)
        assert response.status_code == 422
        assert "secret-marker" not in response.text


async def test_event_gap_resends_and_closed_stage_stops_delivery(request_data):
    seen = []
    first = True

    async def callback(request):
        nonlocal first
        seen.append(json.loads(request.content)["events"])
        if first:
            first = False
            return httpx.Response(
                409, json={"code": "EVENT_GAP", "message": "gap", "delivered_seq": 0}
            )
        return httpx.Response(200, json={"accepted": len(seen[-1]), "skipped": 0})

    fake = FakeEngine(callback_transport=httpx.MockTransport(callback))
    engine_id = await create(fake, RunCreateRequest(**request_data))
    assert seen[0] == seen[1]
    assert fake.states[engine_id].phase == Phase.drafting

    async def closed(request):
        return httpx.Response(409, json={"code": "STAGE_CLOSED", "message": "closed"})

    fake.callback_transport = httpx.MockTransport(closed)
    await fake.to_phase(engine_id, Phase.reviewing)
    assert fake.states[engine_id].phase == Phase.cancelled


async def test_cancel_delivers_lifecycle_event(fake_setup):
    fake, request, deliveries = fake_setup
    engine_id = await create(fake, request)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app),
        base_url="http://engine.test",
        headers={"X-Service-Key": fake.service_key},
    ) as client:
        response = await client.post(f"/runs/{engine_id}/cancel")
    assert response.json()["phase"] == "cancelled"
    events = [
        e
        for d in deliveries
        if d.url.path.endswith("/events")
        for e in json.loads(d.content)["events"]
    ]
    assert events[-1]["type"] == "run.cancelled"


async def test_capability_options_and_review_artifact_order(fake_setup):
    fake, request, deliveries = fake_setup
    fake.scripted()
    await create(fake, request)
    uploads = [d.content for d in deliveries if d.url.path.endswith("/artifacts")]
    assert b"review.findings/v1" in uploads[0]
    assert b"fake.output/v1" in uploads[1]
    custom = FakeEngine(
        profiles=[{}, {"domain": "se"}], reuses_prior_run=True, providers=["bedrock"]
    )
    assert custom.capabilities.reuses_prior_run
    assert custom.capabilities.providers == ["bedrock"]
    assert custom.capabilities.profiles[1].domain == "se"
