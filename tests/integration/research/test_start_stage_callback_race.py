from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from management.research.models import ResearchRun, RunEvent, StageRun
from tests.fakes.engines import engine_overrides, install_fake_engines, seed_models
from tests.helpers.harness import open_harness
from tests.helpers.identity import login, mutation_headers
from tests.helpers.projects import PROJECTS, create_project

EVENTS = 12


def reads_run(statement) -> bool:
    return any(
        description["entity"] is ResearchRun
        for description in getattr(statement, "column_descriptions", [])
    )


def racing_session(state):
    """A session that lets pending engine callbacks commit right after an unlocked run read.

    SQLite ignores FOR UPDATE, so the test models the lock: a locked read of the run row keeps
    the callbacks out until the request ends, as Postgres does.
    """

    class RacingSession(AsyncSession):
        async def _after_read(self, *, locked, instance=False):
            if state["armed"] is None:
                return
            if locked:
                state["deferred"], state["armed"] = state["armed"], None
            elif instance:
                land, state["armed"] = state["armed"], None
                await land()

        async def refresh(self, instance, *args, **kwargs):
            await super().refresh(instance, *args, **kwargs)
            await self._after_read(locked=False, instance=isinstance(instance, ResearchRun))

        async def scalar(self, statement, *args, **kwargs):
            result = await super().scalar(statement, *args, **kwargs)
            if reads_run(statement):
                locked = getattr(statement, "_for_update_arg", None) is not None
                await self._after_read(locked=locked, instance=not locked)
            return result

    return RacingSession


async def test_start_run_survives_callbacks_committing_after_the_create_response(tmp_path):
    async with open_harness(tmp_path, **engine_overrides()) as harness:
        fakes = install_fake_engines(harness)
        fake = fakes["i2h"]
        # Callbacks are held back; they land after the Platform has read the run state.
        fake.callback_transport = None
        state = {"armed": None, "deferred": None}

        async def land():
            fake.callback_transport = harness.transport
            for engine_id in fake.states:
                await fake._deliver(engine_id)

        client_for_engine = harness.engines.get("i2h")
        create_run = client_for_engine.create_run

        async def create_run_with_events(request):
            response = await create_run(request)
            for index in range(EVENTS):
                await fake.emit_activity(response.engine_run_id, "i2h.work", f"Step {index}")
            state["armed"] = land
            return response

        client_for_engine.create_run = create_run_with_events
        harness.app.state.session_factory = async_sessionmaker(
            harness.factory.kw["bind"],
            class_=racing_session(state),
            expire_on_commit=False,
            autoflush=False,
        )

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
            if state["deferred"]:
                await state["deferred"]()
            run_id = UUID(response.json()["data"]["id"])

        async with harness.factory() as db:
            stage = await db.scalar(select(StageRun).where(StageRun.research_run_id == run_id))
            run = await db.get(ResearchRun, run_id)
            events = list(
                await db.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            )
        assert stage.engine_run_id is not None
        assert stage.status == "RUNNING" and stage.last_source_seq == EVENTS + 1
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert run.last_seq == len(events)
        assert sorted(e.source_seq for e in events if e.source_seq) == list(range(1, EVENTS + 2))
