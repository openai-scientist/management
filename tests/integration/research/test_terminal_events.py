import hashlib
import json

import pytest

from tests.helpers.research import open_pipeline_api


@pytest.mark.parametrize(
    ("kind", "phase", "stage_status", "run_status"),
    [
        ("run.published", "published", "COMPLETED", "awaiting_review"),
        ("run.failed", "failed", "FAILED", "failed"),
        ("run.cancelled", "cancelled", "STOPPED", "stopped"),
    ],
)
async def test_terminal_event_closes_stage_without_phase_changed(
    tmp_path, kind, phase, stage_status, run_status
):
    async with open_pipeline_api(tmp_path) as state:
        _, client, _, project, run, _ = state
        stage_id = run["current_stage"]["stage_run_id"]
        callback = f"/api/v1/internal/engines/i2h/runs/{stage_id}"
        headers = {"X-Service-Key": "test-service-key"}
        payload = {}
        if kind == "run.published":
            body = b'{"hypotheses":[{"id":"H1"}]}'
            uploaded = await client.post(
                callback + "/artifacts",
                headers=headers,
                data={
                    "meta": json.dumps(
                        {
                            "schema": "i2h.hypotheses/v1",
                            "version": 1,
                            "primary": True,
                            "filename": "hypotheses.json",
                            "media_type": "application/json",
                            "sha256": hashlib.sha256(body).hexdigest(),
                        }
                    )
                },
                files={"file": ("hypotheses.json", body, "application/json")},
            )
            assert uploaded.status_code == 201, uploaded.text
            payload = {"artifact_id": uploaded.json()["artifact_id"]}
        elif kind == "run.failed":
            payload = {"code": "internal_error", "message": "Engine failed"}
        response = await client.post(
            callback + "/events",
            headers=headers,
            json={
                "events": [
                    {
                        "source_seq": 2,
                        "type": kind,
                        "class": "lifecycle",
                        "phase": phase,
                        "occurred_at": "2026-10-14T01:00:00Z",
                        "payload": payload,
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        current = (await client.get(f"/api/v1/projects/{project['id']}/runs/{run['id']}")).json()[
            "data"
        ]
        assert current["current_stage"]["status"] == stage_status
        assert current["current_stage"]["phase"] == phase
        assert current["status"] == run_status
