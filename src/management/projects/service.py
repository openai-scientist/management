from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.errors import APIError
from management.research.models import ResearchRun


async def get_project_run(db: AsyncSession, project_id: UUID, run_id: UUID) -> ResearchRun:
    """The run of this project, or a 404. Comments need only the run, not the research service."""
    run = await db.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.project_id == project_id)
    )
    if run is None:
        raise APIError(404, "NOT_FOUND", "Run was not found")
    return run
