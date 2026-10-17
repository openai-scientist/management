from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from management.identity.bootstrap_admin import bootstrap_admin
from management.identity.models import User
from management.projects.models import Project
from management.research.models import ResearchRun
from tests.helpers.harness import Harness
from tests.helpers.identity import login

OVERVIEW = "/api/v1/admin/overview"
ADMIN_EMAIL = "series-admin@example.com"
START = datetime(2026, 10, 1, tzinfo=UTC)
END = datetime(2026, 10, 2, tzinfo=UTC)


async def _seed(harness: Harness, runs: list[dict]) -> None:
    async with harness.factory() as db:
        admin = await db.scalar(select(User).where(User.email == ADMIN_EMAIL))
        assert admin is not None
        project = Project(
            id=uuid4(), name="Series study", owner_user_id=admin.id, status="researching"
        )
        db.add(project)
        for run in runs:
            db.add(
                ResearchRun(
                    id=uuid4(),
                    project_id=project.id,
                    created_by_user_id=admin.id,
                    budget_usd=Decimal("10"),
                    created_at=run.get("started_at") or START,
                    **run,
                )
            )
        await db.commit()


async def _overview(client, section: str, start: datetime = START, end: datetime = END) -> dict:
    response = await client.get(
        OVERVIEW,
        params={"section": section, "from": start.isoformat(), "to": end.isoformat()},
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _series(data: dict, key: str) -> dict:
    return next(item for item in data["series"] if item["key"] == key)


def _unavailable_keys(data: dict) -> set[str]:
    return {item["key"] for item in data["unavailable_fields"]}


@pytest.mark.asyncio
async def test_overview_series_come_from_run_timestamps(harness: Harness) -> None:
    async with harness.client() as client:
        await login(harness, client, uid="series-admin", email=ADMIN_EMAIL)
        await bootstrap_admin(
            ADMIN_EMAIL, settings=harness.settings, session_factory=harness.factory
        )
        hour = timedelta(hours=1)
        await _seed(
            harness,
            [
                # Bucket 02:00 finishes one completed and one failed run: 50%.
                {"status": "completed", "started_at": START, "finished_at": START + 2 * hour},
                {"status": "failed", "started_at": START + hour, "finished_at": START + 2 * hour},
                # Paused run with no finish time stays active until the window end.
                {"status": "paused", "started_at": START + 5 * hour},
                # A run left without a finish time in a finished status is never counted.
                {"status": "failed", "started_at": START},
                # Finishing exactly at the window end belongs to the next window.
                {"status": "completed", "started_at": START, "finished_at": END},
            ],
        )

        data = await _overview(client, "overview")

        reliability = _series(data, "execution_reliability")
        assert reliability["available"] is True
        assert reliability["interval_seconds"] == 300
        points = reliability["points"]
        assert len(points) == 288
        assert datetime.fromisoformat(points[0]["timestamp"]) == START
        by_time = {datetime.fromisoformat(p["timestamp"]): p["value"] for p in points}
        assert by_time[START + 2 * hour - timedelta(minutes=5)] is None
        assert by_time[START + 2 * hour] == 50.0
        assert all(value is None for moment, value in by_time.items() if moment != START + 2 * hour)

        active = {
            datetime.fromisoformat(p["timestamp"]): p["value"]
            for p in _series(data, "active_runs")["points"]
        }
        assert active[START] == 2  # completed run + the window-end run
        assert active[START + hour] == 3
        assert active[START + 2 * hour] == 1  # the two that finished at 02:00 no longer count
        assert active[START + 6 * hour] == 2
        assert active[END - timedelta(minutes=5)] == 2

        unavailable = _unavailable_keys(data)
        assert "series.execution_reliability" not in unavailable
        assert "series.active_runs" not in unavailable
        assert "breakdowns.activity_by_service" in unavailable


@pytest.mark.asyncio
async def test_projects_series_and_unmeasured_sources(harness: Harness) -> None:
    async with harness.client() as client:
        await login(harness, client, uid="series-admin", email=ADMIN_EMAIL)
        await bootstrap_admin(
            ADMIN_EMAIL, settings=harness.settings, session_factory=harness.factory
        )
        await _seed(
            harness,
            [
                {
                    "status": "completed",
                    "started_at": START,
                    "finished_at": START + timedelta(days=3),
                }
            ],
        )

        window_end = START + timedelta(days=28)
        data = await _overview(client, "projects", START, window_end)
        health = _series(data, "project_execution_health")
        assert health["interval_seconds"] == 21_600
        assert len(health["points"]) == 112
        assert sum(p["value"] == 100.0 for p in health["points"]) == 1
        assert data["metrics"]["successful_runs_rate"]["value"] == 100.0
        assert "series.project_execution_health" not in _unavailable_keys(data)
        assert "series.active_runs" not in _unavailable_keys(data)

        governance = await _overview(client, "governance")
        gate = _series(governance, "decision_gate_quality")
        assert gate["available"] is False
        assert gate["points"] == []
        reasons = {item["key"]: item["reason_code"] for item in governance["unavailable_fields"]}
        assert reasons["series.decision_gate_quality"] == "OUTCOME_MAPPING_UNDEFINED"
