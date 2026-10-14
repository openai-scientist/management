from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from management.identity.models import User
from management.projects.models import Project
from management.research.contracts import EventEnvelope
from management.research.models import StageRun
from management.research.orchestrator import start_research_run
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.helpers.datasets import upload_dataset
from tests.helpers.harness import CALLBACK_KEY, open_harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, create_project

INTERNAL = "/api/v1/internal/popper/runs"
SERVICE = {"X-Service-Key": CALLBACK_KEY}
REVIEW = {
    "frame": "Exam performance",
    "items": {
        "variables.exam_score.role": {"value": "outcome", "status": "proposed"},
        "variables.school.role": {"value": "cluster", "status": "proposed"},
        "questions.q1": {"value": {"text": "Does study time matter?"}, "status": "proposed"},
    },
}


async def ready_project(client: AsyncClient, session: dict) -> tuple[dict, dict]:
    """A project with one dataset version, ready for a topic run."""
    project = await create_project(client, session)
    dataset = (await upload_dataset(client, session, project["id"])).json()["data"]
    return project, dataset["latest_version"]


async def start_run(client: AsyncClient, session: dict, project_id: str, _version_id: str, **more):
    body = {
        "topic": "Does study time affect exam performance?",
        "domains": ["education"],
        "review_mode": "copilot",
        **more,
    }
    return await client.post(
        f"{PROJECTS}/{project_id}/runs",
        json=body,
        headers=mutation_headers(session["csrf_token"]),
    )


async def report(client: AsyncClient, run_id: str, **body: object):
    return await client.post(f"{INTERNAL}/{run_id}/status", json=body, headers=SERVICE)


def lifecycle(kind, *, phase=None, payload=None, seq=1):
    return EventEnvelope(
        source_seq=seq,
        type=kind,
        event_class="lifecycle",
        phase=phase,
        payload=payload or {},
        occurred_at=datetime.now(UTC),
    )


@asynccontextmanager
async def open_pipeline(tmp_path):
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


@asynccontextmanager
async def open_pipeline_api(tmp_path, *, scripted=False):
    """Public API fixture shared by timeline and researcher-interaction checks."""
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        fakes = install_fake_engines(harness)
        if scripted:
            for fake in fakes.values():
                fake.scripted()
        async with harness.client() as client:
            session = await login(harness, client, uid="owner", email="owner@example.com")
            project = await create_project(client, session)
            await seed_models(harness, session["user"]["id"])
            response = await client.post(
                f"{PROJECTS}/{project['id']}/runs",
                json={
                    "topic": "Compare two retrieval methods",
                    "domains": ["ml"],
                    "interaction_mode": "copilot",
                },
                headers=mutation_headers(session["csrf_token"]),
            )
            assert response.status_code == 201, response.text
            run = response.json()["data"]
            yield harness, client, session, project, run, fakes
