"""A project's model spend for the Workspace screen."""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import require_project_access
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import Principal, require_active_principal
from management.db.session import get_db
from management.projects.schemas.spending import SpendingUsage
from management.projects.spending_service import read_usage

router = APIRouter(prefix="/projects", tags=["project-spending"])


@router.get(
    "/{project_id}/spending/usage",
    response_model=ApiResponse[SpendingUsage],
    response_model_by_alias=True,
    summary="Model spend for one month",
    description=(
        "For the Project Manager and Platform Admins, also on archived projects. The month "
        "follows the project's timezone. Money is a decimal string in USD. `coverage` says "
        "whether every cost of the month is counted: months before the ledger began are "
        "`unavailable`, and a month with spend of unknown price is `partial`; then the budget "
        "status is `unknown` rather than a guess. `budget` is given for the current month only."
    ),
    responses={
        403: {"model": ErrorResponse, "description": "Project Manager role is required"},
        404: {"model": ErrorResponse, "description": "The project does not exist or is not yours"},
        422: {"model": ErrorResponse, "description": "A future month (INVALID_TIME_RANGE)"},
    },
)
async def read_spending_usage(
    project_id: UUID,
    month: str | None = Query(
        default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM; default now"
    ),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[SpendingUsage]:
    project, _ = await require_project_access(db, principal, project_id, manage=True)
    return ok(await read_usage(db, project.id, month))
