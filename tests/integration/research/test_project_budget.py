from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import func, select

from management.core.errors import APIError
from management.projects.models import Notification, ProjectSetting
from management.projects.settings_service import patch_section
from management.projects.spending_service import read_usage
from management.research.models import (
    CostLedgerStart,
    ResearchRun,
    RunEvent,
    StageRun,
    active_run_filter,
)
from management.research.orchestrator import accept_stage, apply_engine_event
from management.research.project_budget import ensure_within_budget
from tests.helpers.identity import mutation_headers
from tests.helpers.projects import PROJECTS, code
from tests.helpers.research import lifecycle, open_pipeline, open_pipeline_api, publish


@pytest.fixture
async def pipeline(tmp_path):
    async with open_pipeline(tmp_path) as state:
        state[1].add(CostLedgerStart(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC)))
        yield state


async def budget(db, run, **values):
    await patch_section(
        db,
        project_id=run.project_id,
        section="spending",
        version=0,
        values={"capped": True, **values},
        actor_user_id=run.created_by_user_id,
        request_id=None,
    )


async def spend(db, stage, total, seq):
    await apply_engine_event(db, stage, lifecycle("run.cost", payload={"cost_usd": total}, seq=seq))


async def alerts(db, kind):
    return await db.scalar(select(func.count(Notification.id)).where(Notification.kind == kind))


async def test_stop_blocks_new_work_and_alerts_managers_once_a_month(pipeline):
    _, db, run, stage, _ = pipeline
    await budget(db, run, limit="1", warn_at="80", at_limit="stop")
    await ensure_within_budget(db, run.project_id)

    await spend(db, stage, "0.9", 1)
    assert await alerts(db, "budget_warning") == 1
    await spend(db, stage, "1.2", 2)
    await spend(db, stage, "1.4", 3)
    assert await alerts(db, "budget_exceeded") == 1
    assert await alerts(db, "budget_warning") == 1

    with pytest.raises(APIError) as info:
        await ensure_within_budget(db, run.project_id)
    assert (info.value.status_code, info.value.code) == (409, "PROJECT_BUDGET_EXCEEDED")
    usage_budget = (await read_usage(db, run.project_id, None)).budget
    assert usage_budget.enforced is True and usage_budget.status == "exceeded"


async def test_the_run_stops_at_the_next_stage_and_the_callback_still_commits(pipeline):
    harness, db, run, stage, fakes = pipeline
    project_id = run.project_id
    await budget(db, run, limit="1", at_limit="stop")
    await spend(db, stage, "2", 1)
    await publish(db, run, stage)
    sent = sum(len(fake.requests) for fake in fakes.values())

    next_stage = await accept_stage(
        db, harness.app.state, stage, selection={}, note=None, actor="user", user_id=None
    )

    assert run.status == "stopped"
    assert next_stage.status == "STOPPED"
    assert sum(len(fake.requests) for fake in fakes.values()) == sent
    events = await db.scalars(
        select(RunEvent).where(
            RunEvent.stage_run_id == next_stage.id, RunEvent.type == "stage.status_changed"
        )
    )
    assert {"status": "STOPPED", "reason": "project_budget_exceeded"} in [e.payload for e in events]
    # A stopped run does not keep the project busy.
    busy = select(func.count()).select_from(ResearchRun)
    busy = busy.where(ResearchRun.project_id == project_id, active_run_filter())
    assert await db.scalar(busy) == 0
    await db.rollback()
    # The callback's own report was committed before the stage was held.
    assert (await read_usage(db, project_id, None)).totals.spent == "2.000000"


async def test_notify_only_alerts(pipeline):
    _, db, run, stage, _ = pipeline
    await budget(db, run, limit="1", at_limit="notify")
    await spend(db, stage, "3", 1)
    await ensure_within_budget(db, run.project_id)
    assert await alerts(db, "budget_exceeded") == 1
    usage_budget = (await read_usage(db, run.project_id, None)).budget
    assert (usage_budget.enforced, usage_budget.reason_code) == (False, "NOTIFY_ONLY")


async def test_pause_cannot_be_saved_but_a_stored_pause_stops(pipeline):
    _, db, run, stage, _ = pipeline
    with pytest.raises(APIError) as info:
        await budget(db, run, at_limit="pause")
    assert (info.value.status_code, info.value.code) == (422, "SETTING_UNSUPPORTED")

    await budget(db, run, limit="1")
    row = await db.scalar(select(ProjectSetting).where(ProjectSetting.project_id == run.project_id))
    row.sections = {**row.sections, "spending": {**row.sections["spending"], "at_limit": "pause"}}
    await db.flush()
    await spend(db, stage, "2", 1)
    usage_budget = (await read_usage(db, run.project_id, None)).budget
    assert (usage_budget.enforced, usage_budget.reason_code) == (True, "PAUSE_TREATED_AS_STOP")
    with pytest.raises(APIError):
        await ensure_within_budget(db, run.project_id)


async def test_starting_a_run_over_budget_is_refused(tmp_path):
    async with open_pipeline_api(tmp_path) as (harness, client, session, project, run, _):
        headers = mutation_headers(session["csrf_token"])
        saved = await client.patch(
            f"{PROJECTS}/{project['id']}/settings/spending",
            json={"version": 0, "values": {"capped": True, "limit": "1"}},
            headers=headers,
        )
        assert saved.status_code == 200, saved.text
        async with harness.factory() as db:
            db.add(CostLedgerStart(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC)))
            stage = await db.scalar(
                select(StageRun).where(StageRun.research_run_id == UUID(run["id"]))
            )
            await spend(db, stage, "5", 1)
            await db.commit()

        refused = await client.post(
            f"{PROJECTS}/{project['id']}/runs",
            json={"topic": "Another look at retrieval", "domains": ["ml"]},
            headers=headers,
        )
        assert code(refused) == (409, "PROJECT_BUDGET_EXCEEDED")
