"""Write model spend to the cost ledger, in the transaction that learns about it.

Engines and Popper report running totals. Each entry stores only the increase since the
entries already written for the same source, so summing a month's entries gives what was
spent in it. Every entry has a `source_key` that names the report it came from: a replayed
report is skipped by the unique key instead of being counted twice.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from management.research.models import CostEntry, CostLedgerStart, ResearchRun, StageRun
from management.research.project_budget import send_budget_alerts

MICRO = Decimal("0.000001")


@dataclass(frozen=True)
class Part:
    """What one model role used in a report, as running totals for the source."""

    role: str
    provider: str | None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None


async def _write(db: AsyncSession, rows: list[dict]) -> None:
    if not rows:
        return
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(CostEntry).values(rows).on_conflict_do_nothing(index_elements=["source_key"])
    )
    if any(row["cost_usd"] for row in rows):
        await send_budget_alerts(db, rows[0]["project_id"])


async def _spent(db: AsyncSession, source_id: UUID) -> Decimal:
    total = await db.scalar(
        select(func.coalesce(func.sum(CostEntry.cost_usd), 0)).where(
            CostEntry.source_id == source_id
        )
    )
    return Decimal(str(total))


async def _tokens(db: AsyncSession, source_id: UUID) -> dict[str, tuple[int, int]]:
    rows = await db.execute(
        select(
            CostEntry.role,
            func.coalesce(func.sum(CostEntry.input_tokens), 0),
            func.coalesce(func.sum(CostEntry.output_tokens), 0),
        )
        .where(CostEntry.source_id == source_id)
        .group_by(CostEntry.role)
    )
    return {role: (int(i), int(o)) for role, i, o in rows.all()}


async def _when(db: AsyncSession, source_id: UUID, created_at: datetime | None) -> datetime:
    """When to book a source's spend. A source older than the ledger books its first
    entry at the ledger start: that spend began before the ledger, in a month that the
    usage report already calls partial, so a later month is never charged with it."""
    now = datetime.now(UTC)
    started = await db.scalar(select(CostLedgerStart.started_at).where(CostLedgerStart.id == 1))
    if started is None or created_at is None:
        return now
    started, created_at = (t if t.tzinfo else t.replace(tzinfo=UTC) for t in (started, created_at))
    if created_at >= started:
        return now
    seen = await db.scalar(select(CostEntry.id).where(CostEntry.source_id == source_id).limit(1))
    return now if seen else started


def _split(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Share an amount by weight, to the micro-dollar; the last share takes the rounding."""
    amount = amount.quantize(MICRO)
    total = sum(weights)
    if total <= 0:
        weights, total = [Decimal(1)] + [Decimal(0)] * (len(weights) - 1), Decimal(1)
    shares = [(amount * w / total).quantize(MICRO) for w in weights[:-1]]
    return [*shares, amount - sum(shares)]


async def record_stage_cost(
    db: AsyncSession,
    run: ResearchRun,
    stage_run: StageRun,
    *,
    source_seq: int,
    total_usd: Decimal | None,
    usage: dict[str, tuple[int, int]],
) -> None:
    """Record a stage run's `run.cost` report.

    ``total_usd`` is the stage's running cost, or None when it could not be priced;
    ``usage`` holds running token totals by model role. The increase in cost is shared
    between the roles in proportion to what their new tokens cost, or to their tokens.
    """
    models = stage_run.config_snapshot.get("models", {})
    prices = stage_run.config_snapshot.get("pricing", {})
    before = await _tokens(db, stage_run.id)
    parts, deltas = [], []
    for role, (input_tokens, output_tokens) in sorted(usage.items()):
        done_in, done_out = before.get(role, (0, 0))
        delta = (max(input_tokens - done_in, 0), max(output_tokens - done_out, 0))
        model = models.get(role, {})
        parts.append(Part(role, model.get("provider"), model.get("model") or "unknown", *delta))
        deltas.append(delta)

    increase = None
    if total_usd is not None:
        increase = Decimal(total_usd) - await _spent(db, stage_run.id)
        # A total lower than what is recorded is a correction the ledger does not undo.
        increase = max(increase, Decimal(0))
    if not parts:
        if not increase:
            return
        model = models.get("stage", {})
        parts = [Part("stage", model.get("provider"), model.get("model"), None, None)]
        deltas = [(0, 0)]

    if increase is None:
        costs: list[Decimal | None] = [None] * len(parts)
    else:
        weights = []
        for part, (new_in, new_out) in zip(parts, deltas, strict=True):
            price = prices.get(part.role, {})
            if price.get("input") is not None and price.get("output") is not None:
                weights.append(
                    Decimal(price["input"]) * new_in + Decimal(price["output"]) * new_out
                )
            else:
                weights.append(Decimal(new_in + new_out))
        costs = _split(increase, weights)

    when = await _when(db, stage_run.id, stage_run.created_at)
    await _write(
        db,
        [
            _row(
                run,
                "stage",
                stage_run.id,
                f"stage:{stage_run.id}:{source_seq}:{part.role}",
                part,
                cost,
                when,
            )
            for part, cost in zip(parts, costs, strict=True)
            if (cost is not None and cost > 0) or part.input_tokens or part.output_tokens
        ],
    )


async def record_consultation_cost(
    db: AsyncSession,
    run: ResearchRun,
    consultation_id: UUID,
    parts: list[tuple[Part, Decimal | None]],
) -> None:
    """Record what one AI PI consultation cost, by model role. Written once per role."""
    await _write(
        db,
        [
            _row(
                run,
                "consultation",
                consultation_id,
                f"consultation:{consultation_id}:{p.role}",
                p,
                c,
                datetime.now(UTC),
            )
            for p, c in parts
        ],
    )


async def record_popper_total(db: AsyncSession, run: ResearchRun, total_usd: Decimal) -> None:
    """Record a Popper run's reported running cost. Popper reports no models or tokens.

    The total only goes up, so it names its own report: the same total twice adds nothing.
    """
    increase = Decimal(total_usd) - await _spent(db, run.id)
    if increase <= 0:
        return
    part = Part("popper", None, None, None, None)
    when = await _when(db, run.id, run.created_at)
    await _write(
        db,
        [
            _row(
                run,
                "popper",
                run.id,
                f"popper:{run.id}:{Decimal(total_usd).normalize()}",
                part,
                increase,
                when,
            )
        ],
    )


def _row(
    run: ResearchRun,
    source: str,
    source_id: UUID,
    key: str,
    part: Part,
    cost: Decimal | None,
    occurred_at: datetime,
) -> dict:
    return {
        "id": uuid4(),
        "project_id": run.project_id,
        "run_id": run.id,
        "source": source,
        "source_id": source_id,
        "source_key": key,
        "role": part.role,
        "provider": part.provider,
        "model": part.model,
        "input_tokens": part.input_tokens,
        "output_tokens": part.output_tokens,
        "cost_usd": cost,
        "occurred_at": occurred_at,
    }
