"""A project's model spend for one month, from the cost ledger, against its budget."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.errors import APIError
from management.projects.schemas.spending import (
    ModelUsage,
    SpendingUsage,
    UsageBudget,
    UsageCoverage,
    UsageTotals,
)
from management.projects.settings_service import get_snapshot
from management.research.models import CostEntry, CostLedgerStart

MICRO = Decimal("0.000001")


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(Decimal(value).quantize(MICRO))


def month_bounds(
    month: str | None, zone: ZoneInfo, now: datetime
) -> tuple[str, datetime, datetime]:
    """The month as `YYYY-MM` and its [start, end) in UTC, by the project's timezone."""
    local_now = now.astimezone(zone)
    if month is None:
        year, number = local_now.year, local_now.month
    else:
        year, number = int(month[:4]), int(month[5:])
    if (year, number) > (local_now.year, local_now.month):
        raise APIError(422, "INVALID_TIME_RANGE", "Usage cannot be read for a future month")
    start = datetime(year, number, 1, tzinfo=zone)
    end = datetime(year + number // 12, number % 12 + 1, 1, tzinfo=zone)
    return f"{year:04d}-{number:02d}", start.astimezone(UTC), end.astimezone(UTC)


def budget_status(spending: dict, spent: Decimal | None, *, complete: bool) -> UsageBudget:
    """The budget as it stands for this spend.

    With some spend unpriced the total is a floor: a limit it already passes is certainly
    passed, and anything else is unknown.
    """
    warning = int(spending["warn_at"])
    at_limit = spending["at_limit"]
    common = {
        "capped": spending["capped"],
        "warning_percent": warning,
        "at_limit": at_limit,
        # A stored "pause" predates the rule that only stop and notify can be saved.
        "enforced": spending["capped"] and at_limit in ("stop", "pause"),
        "reason_code": {"pause": "PAUSE_TREATED_AS_STOP", "notify": "NOTIFY_ONLY"}.get(at_limit)
        if spending["capped"]
        else None,
    }
    if not spending["capped"]:
        return UsageBudget(
            limit=None, remaining=None, used_percent=None, status="uncapped", **common
        )
    limit = Decimal(spending["limit"])
    if spent is None:
        status = "unknown"
    elif spent >= limit:
        status = "exceeded"
    elif spent * 100 >= limit * warning:
        status = "warning"
    else:
        status = "within_budget"
    if not complete:
        return UsageBudget(
            limit=_money(limit),
            remaining=None,
            used_percent=None,
            status=status if status == "exceeded" else "unknown",
            **common,
        )
    return UsageBudget(
        limit=_money(limit),
        remaining=_money(max(limit - spent, Decimal(0))),
        used_percent=float((spent * 100 / limit).quantize(Decimal("0.01"))) if limit > 0 else None,
        status=status,
        **common,
    )


async def read_usage(
    db: AsyncSession,
    project_id: UUID,
    month: str | None,
    *,
    now: datetime | None = None,
    by_model: bool = True,
) -> SpendingUsage:
    """The month's usage. ``by_model=False`` skips the per-model rows (budget checks)."""
    now = now or datetime.now(UTC)
    sections = (await get_snapshot(db, project_id)).sections
    timezone = sections["general"]["timezone"]
    label, start, end = month_bounds(month, ZoneInfo(timezone), now)
    as_of = min(now, end)
    current = end > now

    ledger_start = await db.scalar(
        select(CostLedgerStart.started_at).where(CostLedgerStart.id == 1)
    )
    if ledger_start is not None and ledger_start.tzinfo is None:
        ledger_start = ledger_start.replace(tzinfo=UTC)
    in_month = (
        CostEntry.project_id == project_id,
        CostEntry.occurred_at >= start,
        CostEntry.occurred_at < end,
    )

    if ledger_start is None or ledger_start >= as_of:
        coverage = UsageCoverage(
            status="unavailable", from_=None, reason_code="USAGE_NOT_AVAILABLE"
        )
        totals = UsageTotals(
            spent=None,
            run_count=None,
            average_cost_per_run=None,
            input_tokens=None,
            output_tokens=None,
        )
        return SpendingUsage(
            project_id=str(project_id),
            month=label,
            timezone=timezone,
            start_at=start,
            end_at=end,
            as_of=as_of,
            coverage=coverage,
            totals=totals,
            budget=budget_status(sections["spending"], None, complete=False) if current else None,
            by_model=[],
        )

    spent, runs, unpriced, tokened, input_tokens, output_tokens = (
        await db.execute(
            select(
                func.coalesce(func.sum(CostEntry.cost_usd), 0),
                func.count(func.distinct(CostEntry.run_id)),
                func.count(CostEntry.id).filter(CostEntry.cost_usd.is_(None)),
                func.count(CostEntry.input_tokens),
                func.coalesce(func.sum(CostEntry.input_tokens), 0),
                func.coalesce(func.sum(CostEntry.output_tokens), 0),
            ).where(*in_month)
        )
    ).one()
    spent = Decimal(str(spent))
    complete = ledger_start <= start and unpriced == 0
    coverage = UsageCoverage(
        status="complete" if complete else "partial",
        from_=max(start, ledger_start),
        reason_code=None if complete else "PARTIAL_USAGE_COVERAGE",
    )
    totals = UsageTotals(
        spent=_money(spent),
        run_count=runs,
        average_cost_per_run=_money(spent / runs) if runs else None,
        input_tokens=int(input_tokens) if tokened else None,
        output_tokens=int(output_tokens) if tokened else None,
    )

    rows = (
        []
        if not by_model
        else await db.execute(
            select(
                CostEntry.provider,
                CostEntry.model,
                func.count(func.distinct(CostEntry.run_id)),
                func.count(CostEntry.id).filter(CostEntry.cost_usd.is_(None)),
                func.count(CostEntry.input_tokens),
                func.coalesce(func.sum(CostEntry.input_tokens), 0),
                func.coalesce(func.sum(CostEntry.output_tokens), 0),
                func.coalesce(func.sum(CostEntry.cost_usd), 0),
            )
            .where(*in_month)
            .group_by(CostEntry.provider, CostEntry.model)
        )
    )
    models = sorted(
        (
            ModelUsage(
                provider=provider,
                model_id=model or "unknown",
                display_name=model or "Unknown model",
                run_count=count,
                input_tokens=int(model_in) if tokened else None,
                output_tokens=int(model_out) if tokened else None,
                cost=None if no_price else _money(Decimal(str(cost))),
            )
            for (provider, model, count, no_price, tokened, model_in, model_out, cost) in rows
        ),
        key=lambda row: Decimal(row.cost or 0),
        reverse=True,
    )
    return SpendingUsage(
        project_id=str(project_id),
        month=label,
        timezone=timezone,
        start_at=start,
        end_at=end,
        as_of=as_of,
        coverage=coverage,
        totals=totals,
        budget=budget_status(sections["spending"], spent, complete=complete) if current else None,
        by_model=models,
    )
