from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from management.identity.bootstrap_admin import bootstrap_admin
from management.projects.settings_service import patch_section
from management.projects.spending_service import read_usage
from management.research.models import CostEntry, CostLedgerStart
from tests.helpers.harness import Harness
from tests.helpers.identity import login
from tests.helpers.projects import PROJECTS, add_member, code, create_project
from tests.helpers.research import open_pipeline

# 2026-10-15 12:00 in Ho Chi Minh City (UTC+7).
NOW = datetime(2026, 10, 15, 5, tzinfo=UTC)


@pytest.fixture
async def pipeline(tmp_path):
    async with open_pipeline(tmp_path) as state:
        yield state


def entry(run, when, cost, *, model="gpt-x", tokens=(10, 5), source="stage"):
    return CostEntry(
        id=uuid4(),
        project_id=run.project_id,
        run_id=run.id,
        source=source,
        source_id=uuid4(),
        source_key=uuid4().hex,
        role="stage",
        provider="openai" if model else None,
        model=model,
        input_tokens=tokens[0] if tokens else None,
        output_tokens=tokens[1] if tokens else None,
        cost_usd=None if cost is None else Decimal(cost),
        occurred_at=when,
    )


async def save_spending(db, run, version=0, **values):
    await patch_section(
        db,
        project_id=run.project_id,
        section="spending",
        version=version,
        values=values,
        actor_user_id=run.created_by_user_id,
        request_id=None,
    )


async def test_month_follows_the_project_timezone_and_counts_known_cost(pipeline):
    _, db, run, _, _ = pipeline
    db.add(CostLedgerStart(id=1, started_at=datetime(2026, 9, 1, tzinfo=UTC)))
    db.add_all(
        [
            # 30 Sep 18:00 UTC is already 1 Oct in Ho Chi Minh City.
            entry(run, datetime(2026, 9, 30, 18, tzinfo=UTC), "1.25"),
            entry(run, datetime(2026, 10, 10, tzinfo=UTC), "0.75", model="claude-y"),
            # Still September there.
            entry(run, datetime(2026, 9, 30, 16, tzinfo=UTC), "9"),
        ]
    )
    await db.flush()

    usage = await read_usage(db, run.project_id, None, now=NOW)
    assert usage.month == "2026-10"
    assert usage.start_at == datetime(2026, 9, 30, 17, tzinfo=UTC)
    assert usage.end_at == datetime(2026, 10, 31, 17, tzinfo=UTC)
    assert usage.as_of == NOW
    assert usage.coverage.status == "complete"
    assert usage.totals.spent == "2.000000"
    assert usage.totals.run_count == 1
    assert usage.totals.average_cost_per_run == "2.000000"
    assert (usage.totals.input_tokens, usage.totals.output_tokens) == (20, 10)
    assert [(m.model_id, m.cost) for m in usage.by_model] == [
        ("gpt-x", "1.250000"),
        ("claude-y", "0.750000"),
    ]
    assert usage.budget.status == "uncapped" and usage.budget.enforced is False

    september = await read_usage(db, run.project_id, "2026-09", now=NOW)
    assert september.totals.spent == "9.000000" and september.budget is None
    # The ledger began on 1 Sep UTC, after the month began in local time.
    assert september.coverage.status == "partial"
    assert september.coverage.reason_code == "PARTIAL_USAGE_COVERAGE"


async def test_months_before_the_ledger_are_unavailable_and_empty_months_are_zero(pipeline):
    _, db, run, _, _ = pipeline
    db.add(CostLedgerStart(id=1, started_at=datetime(2026, 9, 20, tzinfo=UTC)))
    await db.flush()

    august = await read_usage(db, run.project_id, "2026-08", now=NOW)
    assert august.coverage.status == "unavailable"
    assert august.coverage.reason_code == "USAGE_NOT_AVAILABLE"
    assert august.totals.spent is None and august.by_model == []

    october = await read_usage(db, run.project_id, "2026-10", now=NOW)
    assert october.coverage.status == "complete"
    assert october.totals.spent == "0.000000"
    assert october.totals.run_count == 0 and october.totals.average_cost_per_run is None


async def test_budget_status_and_unknown_cost(pipeline):
    _, db, run, _, _ = pipeline
    db.add(CostLedgerStart(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC)))
    db.add(entry(run, datetime(2026, 10, 2, tzinfo=UTC), "8.5"))
    await db.flush()
    await save_spending(db, run, capped=True, limit="10", warn_at="80")

    budget = (await read_usage(db, run.project_id, None, now=NOW)).budget
    assert (budget.status, budget.remaining, budget.used_percent) == ("warning", "1.500000", 85.0)
    assert budget.enforced is True and budget.reason_code is None

    db.add(entry(run, datetime(2026, 10, 3, tzinfo=UTC), "4"))
    await db.flush()
    budget = (await read_usage(db, run.project_id, None, now=NOW)).budget
    assert (budget.status, budget.remaining, budget.used_percent) == ("exceeded", "0.000000", 125.0)

    db.add(entry(run, datetime(2026, 10, 4, tzinfo=UTC), None, model=None, tokens=None))
    await db.flush()
    usage = await read_usage(db, run.project_id, None, now=NOW)
    assert usage.coverage.status == "partial"
    assert usage.totals.input_tokens == 20
    # What is known already passes the limit; the unpriced spend can only add to it.
    assert (usage.budget.status, usage.budget.remaining, usage.budget.used_percent) == (
        "exceeded",
        None,
        None,
    )
    await save_spending(db, run, version=1, limit="20")
    usage = await read_usage(db, run.project_id, None, now=NOW)
    assert (usage.budget.status, usage.budget.remaining) == ("unknown", None)
    unknown = next(m for m in usage.by_model if m.model_id == "unknown")
    assert unknown.display_name == "Unknown model" and unknown.cost is None


async def test_only_managers_and_admins_read_usage(harness: Harness) -> None:
    async with (
        harness.client() as manager_client,
        harness.client() as researcher_client,
        harness.client() as outsider_client,
        harness.client() as admin_client,
    ):
        manager = await login(harness, manager_client, uid="manager", email="manager@example.com")
        await login(harness, researcher_client, uid="researcher", email="researcher@example.com")
        await login(harness, outsider_client, uid="outsider", email="outsider@example.com")
        await login(harness, admin_client, uid="admin", email="admin@example.com")
        await bootstrap_admin(
            "admin@example.com", settings=harness.settings, session_factory=harness.factory
        )
        project = await create_project(manager_client, manager)
        await add_member(
            manager_client, manager, project["id"], "researcher@example.com", "researcher"
        )
        async with harness.factory() as db:
            db.add(CostLedgerStart(id=1, started_at=datetime(2026, 1, 1, tzinfo=UTC)))
            await db.commit()
        url = f"{PROJECTS}/{project['id']}/spending/usage"

        response = await manager_client.get(url)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["currency"] == "USD" and data["coverage"]["status"] == "complete"
        assert "from" in data["coverage"] and "from_" not in data["coverage"]
        assert data["project_id"] == str(UUID(project["id"]))
        assert (await admin_client.get(url)).status_code == 200

        assert code(await researcher_client.get(url)) == (403, "ROLE_REQUIRED")
        assert (await outsider_client.get(url)).status_code == 404

        assert (await manager_client.get(url, params={"month": "2026-13"})).status_code == 422
        future = await manager_client.get(url, params={"month": "2999-01"})
        assert code(future) == (422, "INVALID_TIME_RANGE")
