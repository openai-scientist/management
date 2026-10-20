"""The project activity feed for the Workspace screen."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import require_project_access
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, paginated
from management.core.sessions import Principal, require_active_principal
from management.core.urls import api_prefix
from management.db.session import get_db
from management.projects.activity import list_activity
from management.projects.schemas.activity import ActivityCategory, ActivityItem

router = APIRouter(prefix="/projects", tags=["project-activity"])


@router.get(
    "/{project_id}/activity",
    response_model=ApiResponse[list[ActivityItem]],
    summary="Project activity",
    description=(
        "Newest first. For the Project Manager and Platform Admins, also on archived projects. "
        "Covers the actions listed in the handoff; events from before an action was recorded "
        "are not shown, so an empty page does not mean nothing ever happened."
    ),
    responses={
        403: {"model": ErrorResponse, "description": "Project Manager role is required"},
        404: {"model": ErrorResponse, "description": "The project does not exist or is not yours"},
        422: {
            "model": ErrorResponse,
            "description": "TIMEZONE_REQUIRED or INVALID_TIME_RANGE for the time filters",
        },
    },
)
async def read_activity(
    project_id: UUID,
    category: ActivityCategory | None = None,
    from_time: datetime | None = Query(
        default=None, alias="from", description="Inclusive lower bound, RFC 3339 with offset"
    ),
    to_time: datetime | None = Query(
        default=None, alias="to", description="Exclusive upper bound, RFC 3339 with offset"
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[list[ActivityItem]]:
    if any(value is not None and value.utcoffset() is None for value in (from_time, to_time)):
        raise APIError(422, "TIMEZONE_REQUIRED", "Activity time filters must include a timezone")
    if from_time is not None and to_time is not None and from_time >= to_time:
        raise APIError(422, "INVALID_TIME_RANGE", "The 'from' filter must be before 'to'")
    project, _ = await require_project_access(db, principal, project_id, manage=True)
    items, total = await list_activity(
        db,
        project_id=project.id,
        project_name=project.name,
        category=category,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
        prefix=prefix,
    )
    return paginated(items, total=total, limit=limit, offset=offset)
