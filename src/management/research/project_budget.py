"""The project's monthly model budget at run time: who may start work, and alerts.

A soft cap. Admission reads what the ledger holds now and reserves nothing, so work that
was already sent to an engine can still spend past the limit.
"""

from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.errors import APIError
from management.core.roles import ProjectRole
from management.notifications.service import notify_project_members
from management.projects.models import SpendingAlert
from management.projects.settings_service import get_snapshot
from management.projects.spending_service import SpendingUsage, read_usage

ALERT_KINDS = {"warning": "budget_warning", "exceeded": "budget_exceeded"}


async def _usage(db: AsyncSession, project_id: UUID) -> SpendingUsage | None:
    """This month's usage, or None when the project has no limit to check against."""
    if not (await get_snapshot(db, project_id)).sections["spending"]["capped"]:
        return None
    return await read_usage(db, project_id, None, by_model=False)


async def over_budget(db: AsyncSession, project_id: UUID) -> bool:
    """The project spent its limit this month and its settings say to stop then."""
    usage = await _usage(db, project_id)
    return usage is not None and usage.budget.enforced and usage.budget.status == "exceeded"


async def ensure_within_budget(db: AsyncSession, project_id: UUID) -> None:
    if await over_budget(db, project_id):
        raise APIError(
            409,
            "PROJECT_BUDGET_EXCEEDED",
            "This project has used its model budget for the month. A Project Manager can "
            "raise the limit in Settings → Spending.",
        )


async def send_budget_alerts(db: AsyncSession, project_id: UUID) -> None:
    """Tell the Project Managers once a month when spend reaches the warning share or limit."""
    usage = await _usage(db, project_id)
    kind = usage and ALERT_KINDS.get(usage.budget.status)
    if not kind:
        return
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    sent = await db.scalar(
        insert(SpendingAlert)
        .values(id=uuid4(), project_id=project_id, period=usage.month, kind=kind)
        .on_conflict_do_nothing(index_elements=["project_id", "period", "kind"])
        .returning(SpendingAlert.id)
    )
    if sent is not None:
        await notify_project_members(db, project_id, kind, roles=(ProjectRole.MANAGER,))
