"""Owner-only project actions: hand the project to another member, or delete it."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import ensure_writable_project
from management.audit.service import record_audit
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.roles import ProjectRole
from management.core.sessions import Principal, require_active_csrf
from management.db.session import get_db
from management.identity.models import User, UserStatus
from management.infra.file_store import FileStore, get_file_store
from management.notifications.notification_stream import notifications_changed
from management.projects.models import Project, ProjectMembership
from management.projects.removal import delete_project_rows
from management.projects.schemas.projects import ProjectItem, ProjectOwnershipTransfer
from management.projects.service import _manage_project, _project_item
from management.research.models import ResearchRun, active_run_filter

logger = logging.getLogger("management.projects")

router = APIRouter(prefix="/projects", tags=["projects"])

OWNER_ERRORS = {
    403: {"model": ErrorResponse, "description": "Only the project owner can do this"},
    404: {"model": ErrorResponse, "description": "The project does not exist or is not yours"},
    409: {"model": ErrorResponse, "description": "The project or the chosen member cannot take it"},
}


async def _owned_project(
    db: AsyncSession, principal: Principal, project_id: UUID
) -> tuple[Project, ProjectMembership | None]:
    """Lock the project for a change only its owner may make; a Platform Admin is refused too."""
    project, membership = await _manage_project(db, principal, project_id)
    if project.owner_user_id != principal.user.id:
        raise APIError(403, "OWNER_REQUIRED", "Only the project owner can do this")
    return project, membership


@router.post(
    "/{project_id}/transfer-ownership",
    response_model=ApiResponse[ProjectItem],
    summary="Make another member the project owner",
    description=(
        "The new owner becomes the Project Manager; the current owner stays in the project "
        "as a Researcher. Owner only, and the new owner must be an active member."
    ),
    responses=OWNER_ERRORS,
)
async def transfer_project_ownership(
    project_id: UUID,
    body: ProjectOwnershipTransfer,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _owned_project(db, principal, project_id)
    ensure_writable_project(project)
    if body.new_owner_user_id == principal.user.id:
        raise APIError(409, "ALREADY_OWNER", "You already own this project. Choose another member.")
    target = await db.scalar(
        select(ProjectMembership)
        .where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == body.new_owner_user_id,
            ProjectMembership.status == "active",
        )
        .with_for_update()
    )
    if target is None:
        raise APIError(
            409,
            "NEW_OWNER_NOT_MEMBER",
            "Only an active member of the project can become its owner. Invite them first.",
        )
    new_owner = await db.get(User, target.user_id)
    if new_owner is None or new_owner.status == UserStatus.SUSPENDED:
        raise APIError(409, "USER_SUSPENDED", "A suspended user cannot own a project")

    previous_role = target.role_code
    target.role_code = ProjectRole.MANAGER
    project.owner_user_id = target.user_id
    if membership is not None:
        membership.role_code = ProjectRole.RESEARCHER
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.ownership_transferred",
        resource_type="project",
        resource_id=project.id,
        project_id=project.id,
        request_id=getattr(request.state, "request_id", None),
        details={
            "from_user_id": str(principal.user.id),
            "to_user_id": str(target.user_id),
            "to_previous_role": previous_role,
        },
    )
    await db.flush()
    return ok(_project_item(project, membership), "Ownership transferred")


@router.delete(
    "/{project_id}",
    response_model=ApiResponse[None],
    summary="Delete a project for good",
    description=(
        "Deletes the project with its datasets, files, connections and research runs; "
        "members lose access. It cannot be undone. Owner only, and no run may be active."
    ),
    responses=OWNER_ERRORS,
)
async def delete_project(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    store: FileStore = Depends(get_file_store),
) -> ApiResponse[None]:
    project, _ = await _owned_project(db, principal, project_id)
    active = await db.scalar(
        select(ResearchRun.id)
        .where(ResearchRun.project_id == project_id, active_run_filter())
        .limit(1)
    )
    if active is not None:
        raise APIError(
            409,
            "PROJECT_HAS_ACTIVE_RUNS",
            "A research run is still going. Stop it before deleting the project.",
        )
    member_ids = list(
        await db.scalars(
            select(ProjectMembership.user_id).where(ProjectMembership.project_id == project_id)
        )
    )
    # The audit log has no foreign key to projects, so this record outlives the project.
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.deleted",
        resource_type="project",
        resource_id=project.id,
        project_id=project.id,
        request_id=getattr(request.state, "request_id", None),
        details={"name": project.name},
    )
    await db.flush()
    keys = await delete_project_rows(db, project_id)
    for user_id in member_ids:
        notifications_changed(db, user_id)
    # Commit first: a row pointing at a missing file is worse than a file nothing points at.
    await db.commit()
    for key in keys:
        try:
            await store.delete(key)
        except Exception:
            logger.warning("stored file was not removed", exc_info=True, extra={"key": key})
    return ok(None, "Project deleted")
