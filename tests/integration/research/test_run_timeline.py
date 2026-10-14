import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.research.models import ResearchRun, RunEvent, StageRun
from tests.helpers.notifications import open_stream
from tests.helpers.research import open_pipeline_api


@pytest.fixture
async def timeline(tmp_path):
    async with open_pipeline_api(tmp_path) as state:
        yield state


async def emit_activity(state):
    harness, client, _, _, run, _ = state
    async with harness.factory() as db:
        stage = await db.get(StageRun, UUID(run["current_stage"]["stage_run_id"]))
    response = await client.post(
        f"/api/v1/internal/engines/i2h/runs/{stage.id}/events",
        headers={"X-Service-Key": "test-service-key"},
        json={
            "events": [
                {
                    "source_seq": stage.last_source_seq + 1,
                    "type": "i2h.work",
                    "class": "activity",
                    "summary": "Read two sources",
                    "actor": "reader",
                    "step_key": "search",
                    "phase": "drafting",
                    "refs": ["paper-1", "paper-2"],
                    "occurred_at": "2026-10-14T01:02:03Z",
                }
            ]
        },
    )
    assert response.status_code == 200, response.text


def events_url(state):
    return f"/api/v1/projects/{state[3]['id']}/runs/{state[4]['id']}/events"


async def test_event_fields_roundtrip(timeline):
    await emit_activity(timeline)
    harness, client, _, _, run, _ = timeline
    response = await client.get(events_url(timeline))
    assert response.status_code == 200
    items = response.json()["data"]
    activity = next(item for item in items if item["type"] == "i2h.work")
    assert activity["class"] == "activity"
    assert activity["summary"] == "Read two sources"
    assert activity["stage_key"] == "hypothesis"
    assert activity["step_key"] == "search"
    assert activity["iteration"] == 1
    assert activity["actor"] == "reader"
    assert activity["phase"] == "drafting"
    assert activity["stage_run_id"] == run["current_stage"]["stage_run_id"]
    assert activity["refs"] == ["paper-1", "paper-2"]
    assert activity["ts"].startswith("2026-10-14T01:02:03")
    async with harness.factory() as db:
        stored = await db.scalar(select(RunEvent).where(RunEvent.type == "i2h.work"))
        assert stored.event_class == "activity" and stored.summary == "Read two sources"
    assert [item["seq"] for item in items] == list(range(1, len(items) + 1))
    assert next(item for item in items if item["type"] == "stage.status_changed")["class"] == (
        "platform"
    )


async def test_filter_by_stage_run(timeline):
    await emit_activity(timeline)
    _, client, _, _, run, _ = timeline
    stage_id = run["current_stage"]["stage_run_id"]
    response = await client.get(events_url(timeline), params={"stage_run_id": stage_id, "limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]) == 2
    assert all(item["stage_run_id"] == stage_id for item in body["data"])
    other = await client.get(events_url(timeline), params={"stage_run_id": str(uuid4())})
    assert other.json()["data"] == [] and other.json()["meta"]["pagination"]["total"] == 0


async def test_activity_event_summary_in_stream(timeline):
    await emit_activity(timeline)
    harness, client, _, _, run, _ = timeline
    async with harness.factory() as db:
        stored = await db.get(ResearchRun, UUID(run["id"]))
        stored.status = "completed"
        await db.commit()
    response = await client.get(
        events_url(timeline) + "/stream",
        params={"stage_run_id": run["current_stage"]["stage_run_id"]},
        headers={"Last-Event-ID": "1"},
    )
    items = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"seq"' in line
    ]
    assert all(item["seq"] > 1 for item in items)
    activity = next(item for item in items if item["type"] == "i2h.work")
    assert activity["class"] == "activity" and activity["summary"] == "Read two sources"
    other = await client.get(
        events_url(timeline) + "/stream", params={"stage_run_id": str(uuid4())}
    )
    assert "event: run-event" not in other.text and "event: run-ended" in other.text


async def test_legacy_popper_events_still_listed(timeline):
    harness, client, _, _, run, _ = timeline
    async with harness.factory() as db:
        stored = await db.get(ResearchRun, UUID(run["id"]))
        stored.last_seq += 1
        db.add(RunEvent(run_id=stored.id, seq=stored.last_seq, type="stage.started", payload={}))
        await db.commit()
    items = (await client.get(events_url(timeline))).json()["data"]
    legacy = next(item for item in items if item["type"] == "stage.started")
    assert legacy["stage_run_id"] is None and legacy["payload"] == {}


async def test_live_stream_filters_stage_and_keeps_global_cursor(timeline):
    harness, client, _, _, run, _ = timeline
    async with harness.factory() as db:
        stored = await db.get(ResearchRun, UUID(run["id"]))
        cursor = stored.last_seq
    path = (
        events_url(timeline) + f"/stream?after={cursor}"
        f"&stage_run_id={run['current_stage']['stage_run_id']}"
    )
    async with open_stream(harness, client, path) as (started, stream):
        assert started["status"] == 200
        assert (await stream.frame()).startswith("retry:")
        assert (await stream.frame()).startswith(": keep-alive")
        async with harness.factory() as db:
            stored = await db.get(ResearchRun, UUID(run["id"]))
            stored.last_seq += 1
            db.add(RunEvent(run_id=stored.id, seq=stored.last_seq, type="legacy.other", payload={}))
            await db.commit()
        await emit_activity(timeline)
        frame = await stream.frame()
        while frame.startswith(":"):
            frame = await stream.frame()
        assert frame.startswith("event: run-event")
        item = json.loads(frame.split("data: ", 1)[1])
        assert item["type"] == "i2h.work" and item["seq"] == cursor + 2
        assert item["class"] == "activity" and item["refs"] == ["paper-1", "paper-2"]


@pytest.mark.parametrize("query_number", [1, 2], ids=["history", "live"])
async def test_stream_drains_terminal_events_before_ending(timeline, monkeypatch, query_number):
    harness, client, _, _, run, fakes = timeline
    async with harness.factory() as db:
        stage = await db.get(StageRun, UUID(run["current_stage"]["stage_run_id"]))
    original = AsyncSession.execute
    count = 0

    async def terminal_after_event_query(db, statement, *args, **kwargs):
        nonlocal count
        result = await original(db, statement, *args, **kwargs)
        descriptions = getattr(statement, "column_descriptions", [])
        if descriptions and descriptions[0].get("entity") is RunEvent:
            count += 1
            if count == query_number:
                await fakes["i2h"].fail(stage.engine_run_id, "internal_error", "Failed")
        return result

    monkeypatch.setattr(AsyncSession, "execute", terminal_after_event_query)
    response = await client.get(events_url(timeline) + "/stream")
    items = [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"seq"' in line
    ]
    assert any(item["type"] == "run.failed" for item in items)
    assert any(
        item["type"] == "stage.status_changed" and item["payload"]["status"] == "FAILED"
        for item in items
    )
    assert "event: run-ended" in response.text
