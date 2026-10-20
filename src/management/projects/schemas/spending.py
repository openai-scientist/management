from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Money = str  # A decimal string with 6 places, e.g. "246.800000". Never a float.


class UsageCoverage(BaseModel):
    status: Literal["complete", "partial", "unavailable"] = Field(
        description="complete: every cost from `from` to `as_of` is counted"
    )
    from_: datetime | None = Field(alias="from", serialization_alias="from")
    reason_code: Literal["PARTIAL_USAGE_COVERAGE", "USAGE_NOT_AVAILABLE"] | None

    model_config = {"populate_by_name": True}


class UsageTotals(BaseModel):
    spent: Money | None
    run_count: int | None
    average_cost_per_run: Money | None = Field(description="Null when no run spent anything")
    input_tokens: int | None = Field(
        description="Tokens that were reported; Popper reports none. Null when none were"
    )
    output_tokens: int | None


class UsageBudget(BaseModel):
    capped: bool
    limit: Money | None
    remaining: Money | None
    used_percent: float | None = Field(description="Not clamped at 100")
    warning_percent: int
    status: Literal["within_budget", "warning", "exceeded", "uncapped", "unknown"]
    at_limit: Literal["stop", "pause", "notify"]
    enforced: bool
    reason_code: str | None


class ModelUsage(BaseModel):
    provider: str | None
    model_id: str = Field(description="`unknown` when the engine did not say which model ran")
    display_name: str
    run_count: int
    input_tokens: int | None
    output_tokens: int | None
    cost: Money | None = Field(description="Null when some of its spend has no price")


class SpendingUsage(BaseModel):
    project_id: str
    month: str
    timezone: str
    currency: Literal["USD"] = "USD"
    start_at: datetime
    end_at: datetime
    as_of: datetime
    coverage: UsageCoverage
    totals: UsageTotals
    budget: UsageBudget | None = Field(description="Only for the current month")
    by_model: list[ModelUsage]
