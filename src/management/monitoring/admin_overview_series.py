"""Time series for the Admin Overview charts, built from research run timestamps.

Buckets start at the window start and have a fixed length, the same way the
operations request-rate series does, so every chart on one screen lines up.
"""

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, and_, case, column, func, or_, select, values
from sqlalchemy.ext.asyncio import AsyncSession

from management.research.models import ACTIVE_RUN_STATUSES, FINISHED_RUN_STATUSES, ResearchRun

SERIES_INTERVALS = (60, 300, 900, 3600, 21_600, 86_400)
MAX_SERIES_POINTS = 500


def series_interval(start: datetime, end: datetime) -> int:
    """Pick the shortest interval that keeps the window within the point limit."""
    seconds = (end - start).total_seconds()
    return next(
        (i for i in SERIES_INTERVALS if math.ceil(seconds / i) <= MAX_SERIES_POINTS),
        SERIES_INTERVALS[-1],
    )


def bucket_starts(start: datetime, end: datetime, interval_seconds: int) -> list[datetime]:
    count = max(0, math.ceil((end - start).total_seconds() / interval_seconds))
    return [start + timedelta(seconds=index * interval_seconds) for index in range(count)]


def _buckets_cte(starts: list[datetime], end: datetime, interval_seconds: int) -> Any:
    rows = [
        (index, bucket, min(bucket + timedelta(seconds=interval_seconds), end))
        for index, bucket in enumerate(starts)
    ]
    return (
        values(
            column("idx", Integer),
            column("bucket_start", DateTime(timezone=True)),
            column("bucket_end", DateTime(timezone=True)),
            name="buckets",
        )
        .data(rows)
        .cte("buckets")
    )


def _series(key: str, unit: str, interval: int, starts: list[datetime], values_: list[Any]):
    return {
        "key": key,
        "unit": unit,
        "interval_seconds": interval,
        "points": [
            {"timestamp": bucket, "value": value, "target": None}
            for bucket, value in zip(starts, values_, strict=True)
        ],
        "available": True,
    }


async def run_success_series(
    db: AsyncSession, *, key: str, start: datetime, end: datetime
) -> dict[str, Any]:
    """Percent of runs finished in each bucket that completed; null where none finished.

    Uses the same finished statuses and `finished_at` window as the
    `successful_runs_rate` metric, so the series and the KPI agree.
    """
    interval = series_interval(start, end)
    starts = bucket_starts(start, end, interval)
    if not starts:
        return _series(key, "percent", interval, starts, [])
    buckets = _buckets_cte(starts, end, interval)
    rows = await db.execute(
        select(
            buckets.c.idx,
            func.count(ResearchRun.id),
            func.sum(case((ResearchRun.status == "completed", 1), else_=0)),
        )
        .select_from(buckets)
        .join(
            ResearchRun,
            and_(
                ResearchRun.status.in_(FINISHED_RUN_STATUSES),
                # Window bounds on the run side let the planner prune before bucketing.
                ResearchRun.finished_at >= start,
                ResearchRun.finished_at < end,
                ResearchRun.finished_at >= buckets.c.bucket_start,
                ResearchRun.finished_at < buckets.c.bucket_end,
            ),
        )
        .group_by(buckets.c.idx)
    )
    rates: list[float | None] = [None] * len(starts)
    for idx, total, completed in rows:
        if total:
            rates[int(idx)] = int(completed or 0) / int(total) * 100
    return _series(key, "percent", interval, starts, rates)


async def active_runs_series(db: AsyncSession, *, start: datetime, end: datetime) -> dict[str, Any]:
    """Runs that were in progress at any point of each bucket.

    A run counts from `started_at` until `finished_at`; paused and awaiting-review
    time counts as active. A run with no `finished_at` counts only while its status
    is still active, so a run left without a finish time never inflates the chart.
    """
    interval = series_interval(start, end)
    starts = bucket_starts(start, end, interval)
    if not starts:
        return _series("active_runs", "count", interval, starts, [])
    buckets = _buckets_cte(starts, end, interval)
    rows = await db.execute(
        select(buckets.c.idx, func.count(ResearchRun.id))
        .select_from(buckets)
        .join(
            ResearchRun,
            and_(
                ResearchRun.started_at < end,
                ResearchRun.started_at < buckets.c.bucket_end,
                or_(
                    ResearchRun.finished_at > buckets.c.bucket_start,
                    and_(
                        ResearchRun.finished_at.is_(None),
                        ResearchRun.status.in_(ACTIVE_RUN_STATUSES),
                    ),
                ),
            ),
        )
        .group_by(buckets.c.idx)
    )
    counts = [0] * len(starts)
    for idx, count in rows:
        counts[int(idx)] = int(count)
    return _series("active_runs", "count", interval, starts, counts)
