from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from management.projects.removal import delete_project_rows
from management.research.cost_ledger import record_popper_total
from management.research.models import CostEntry, CostLedgerStart
from management.research.orchestrator import apply_engine_event
from tests.helpers.research import lifecycle, open_pipeline


@pytest.fixture
async def pipeline(tmp_path):
    async with open_pipeline(tmp_path) as state:
        yield state


async def entries(db, run):
    rows = await db.scalars(
        select(CostEntry).where(CostEntry.run_id == run.id).order_by(CostEntry.occurred_at)
    )
    return list(rows)


def usage(input_tokens, output_tokens, cost=None):
    return {
        "cost_usd": cost,
        "usage": {"stage": {"input_tokens": input_tokens, "output_tokens": output_tokens}},
    }


async def test_running_totals_become_increases_and_a_replay_adds_nothing(pipeline):
    _, db, run, stage, _ = pipeline
    first = lifecycle("run.cost", payload=usage(1000, 500, "0.25"), seq=1)
    await apply_engine_event(db, stage, first)
    await apply_engine_event(
        db, stage, lifecycle("run.cost", payload=usage(3000, 900, "0.75"), seq=2)
    )
    assert run.cost_usd == Decimal("0.75")
    # The engine sends the first report again after a retry.
    await apply_engine_event(db, stage, first)

    rows = await entries(db, run)
    assert [(r.cost_usd, r.input_tokens, r.output_tokens) for r in rows] == [
        (Decimal("0.25"), 1000, 500),
        (Decimal("0.5"), 2000, 400),
    ]
    assert {r.source for r in rows} == {"stage"}
    assert rows[0].model == stage.config_snapshot["models"]["stage"]["model"]
    assert sum(r.cost_usd for r in rows) == Decimal("0.75")


async def test_an_unpriced_report_is_kept_as_unknown_then_priced_reports_continue(pipeline):
    _, db, run, stage, _ = pipeline
    stage.config_snapshot = stage.config_snapshot | {
        "pricing": {"stage": {"input": None, "output": None}}
    }
    await apply_engine_event(db, stage, lifecycle("run.cost", payload=usage(100, 10), seq=1))
    await apply_engine_event(
        db, stage, lifecycle("run.cost", payload=usage(300, 30, "0.40"), seq=2)
    )

    rows = await entries(db, run)
    assert [(r.cost_usd, r.input_tokens) for r in rows] == [(None, 100), (Decimal("0.4"), 200)]


async def test_popper_totals_count_once_each(pipeline):
    _, db, run, _, _ = pipeline
    for total in ("1.5", "1.5", "2.25"):
        await record_popper_total(db, run, Decimal(total))
    rows = await entries(db, run)
    assert [(r.source, r.cost_usd) for r in rows] == [
        ("popper", Decimal("1.5")),
        ("popper", Decimal("0.75")),
    ]
    assert rows[0].model is None and rows[0].input_tokens is None


async def test_a_stage_older_than_the_ledger_books_its_first_spend_at_the_ledger_start(pipeline):
    _, db, run, stage, _ = pipeline
    started = datetime.now(UTC) - timedelta(days=40)
    stage.created_at = started - timedelta(hours=1)
    db.add(CostLedgerStart(id=1, started_at=started))
    await db.flush()
    await apply_engine_event(db, stage, lifecycle("run.cost", payload=usage(10, 5, "2"), seq=1))
    await apply_engine_event(db, stage, lifecycle("run.cost", payload=usage(20, 9, "3"), seq=2))

    first, second = await entries(db, run)
    assert first.occurred_at.replace(tzinfo=UTC) == started
    assert second.occurred_at.replace(tzinfo=UTC) > started + timedelta(days=39)


async def test_removing_a_project_removes_its_spend(pipeline):
    _, db, run, stage, _ = pipeline
    await apply_engine_event(db, stage, lifecycle("run.cost", payload=usage(10, 5, "1"), seq=1))
    await db.commit()
    await delete_project_rows(db, run.project_id)
    await db.commit()
    assert await db.scalar(select(func.count(CostEntry.id))) == 0
