import hashlib

from sqlalchemy import select

from management.contracts import ArtifactMeta, EventBatch
from management.models import RunArtifact, RunEvent, StageRun
from tests import test_orchestrator_flow as flow

lifecycle = flow.lifecycle
pipeline = flow.pipeline  # reuse the fixture

KEY = {"X-Service-Key": "test-service-key"}


def path(stage, engine=None):
    return f"/api/v1/internal/engines/{engine or stage.engine_key}/runs/{stage.id}"


async def send(client, stage, events, **kwargs):
    return await client.post(
        path(stage) + "/events",
        json=EventBatch(events=events).model_dump(mode="json", by_alias=True),
        headers=kwargs.pop("headers", KEY),
        **kwargs,
    )


async def upload(client, stage, content=b"{}", version=1, sha=None):
    meta = ArtifactMeta(
        schema="i2h.hypotheses/v1",
        version=version,
        primary=True,
        filename="result.json",
        media_type="application/json",
        sha256=sha or hashlib.sha256(content).hexdigest(),
        metadata={"source": "test"},
    )
    return await client.post(
        path(stage) + "/artifacts",
        data={"meta": meta.model_dump_json(by_alias=True)},
        files={"file": ("result.json", content)},
        headers=KEY,
    )


async def test_wrong_key_and_other_engine_cannot_write_stage_run(pipeline):
    harness, _, _, stage, _ = pipeline
    event = lifecycle("run.phase_changed", phase="drafting", payload={"from": None})
    async with harness.client() as client:
        wrong = await send(client, stage, [event], headers={"X-Service-Key": "wrong"})
        assert wrong.status_code == 401 and set(wrong.json()) == {"code", "message"}
        other = await client.post(
            path(stage, "exd") + "/events",
            json=EventBatch(events=[event]).model_dump(mode="json", by_alias=True),
            headers=KEY,
        )
        assert other.status_code == 404


async def test_events_contiguous_gap_duplicates_and_closed_stage(pipeline):
    harness, db, run, stage, _ = pipeline
    event = lifecycle("run.phase_changed", phase="drafting", payload={"from": None})
    async with harness.client() as client:
        first = await send(client, stage, [event])
        assert first.status_code == 200, first.text
        assert first.json() == {"accepted": 1, "skipped": 0}
        assert (await send(client, stage, [event])).json() == {"accepted": 0, "skipped": 1}
        gap = await send(client, stage, [event.model_copy(update={"source_seq": 3})])
        assert gap.status_code == 409 and gap.json()["code"] == "EVENT_GAP"
        assert gap.json()["delivered_seq"] == 1
        await db.refresh(stage)
        stage.status = "STOPPED"
        await db.commit()
        closed = await send(client, stage, [event])
        assert closed.status_code == 409 and closed.json()["code"] == "STAGE_CLOSED"


async def test_seq_continuous_across_two_stage_runs(pipeline):
    harness, db, run, stage, _ = pipeline
    second = StageRun(
        research_run_id=run.id,
        iteration=1,
        stage_key="experiment_plan",
        engine_key="exd",
        status="RUNNING",
    )
    db.add(second)
    await db.commit()
    async with harness.client() as client:
        for row in (stage, second):
            response = await send(
                client,
                row,
                [lifecycle("run.phase_changed", phase="drafting", payload={"from": None})],
            )
            assert response.status_code == 200, response.text
    events = list(
        await db.scalars(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))
    )
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert [e.source_seq for e in events if e.source_seq is not None] == [1, 1]


async def test_artifact_upload_hash_idempotence_and_download_scope(pipeline):
    harness, db, run, stage, _ = pipeline
    async with harness.client() as client:
        bad = await upload(client, stage, sha="0" * 64)
        assert bad.status_code == 422 and bad.json()["code"] == "SHA256_MISMATCH"
        first = await upload(client, stage)
        assert first.status_code == 201, first.text
        artifact_id = first.json()["artifact_id"]
        assert (await upload(client, stage)).json()["artifact_id"] == artifact_id
        assert (await upload(client, stage, content=b"different")).status_code == 409
        assert (await upload(client, stage, content=b"second", version=2)).status_code == 201
        url = f"/api/v1/internal/engines/exd/artifacts/{artifact_id}"
        assert (await client.get(url, headers=KEY)).status_code == 404
        db.add(
            StageRun(
                research_run_id=run.id,
                iteration=1,
                stage_key="experiment_plan",
                engine_key="exd",
                status="PENDING",
            )
        )
        await db.commit()
        downloaded = await client.get(url, headers=KEY)
        assert downloaded.status_code == 200 and downloaded.content == b"{}"
        assert "attachment" in downloaded.headers["content-disposition"]
    artifact = await db.get(RunArtifact, __import__("uuid").UUID(artifact_id))
    assert artifact.artifact_metadata == {"source": "test"}


async def test_engine_artifact_over_1mib_accepted(pipeline):
    harness, _, _, stage, _ = pipeline
    async with harness.client() as client:
        response = await upload(client, stage, content=b"x" * (1_048_576 + 1))
        assert response.status_code == 201, response.text


async def test_engine_route_rejects_missing_key_before_body(pipeline):
    harness, _, _, stage, _ = pipeline

    async def unread_body():
        raise AssertionError("Unauthorized body was read")
        yield b"never"

    async with harness.client() as client:
        response = await client.post(path(stage) + "/artifacts", content=unread_body())
        assert response.status_code == 401
        assert response.json()["code"] == "SERVICE_KEY_INVALID"
