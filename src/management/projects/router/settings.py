"""Project settings: one GET for every section, one PATCH per section."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import ensure_writable_project, require_project_access
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.roles import ProjectRole
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.projects.schemas.settings import (
    AccessContext,
    AccessPermissions,
    InvitationDefaults,
    ProjectSettingsData,
    ProjectSettingsPatch,
)
from management.projects.service import _manage_project, project_access
from management.projects.settings_service import get_snapshot, invite_ttl_hours, patch_section

router = APIRouter(prefix="/projects", tags=["project-settings"])

SETTINGS_ERRORS = {
    403: {"model": ErrorResponse, "description": "Project Manager role is required"},
    404: {
        "model": ErrorResponse,
        "description": "The project does not exist or is not yours (NOT_FOUND), "
        "or the section does not exist (SETTINGS_SECTION_NOT_FOUND)",
    },
}


@router.get(
    "/{project_id}/access-context",
    response_model=ApiResponse[AccessContext],
    summary="What I may do in this project",
    description=(
        "For any member: permissions worked out from the session, the membership and the "
        "project's settings as they are now, plus invitation defaults when you may invite. "
        "These drive the UI only; every API checks again. Refresh after a settings change, a "
        "role change, a restore, or a 403."
    ),
    responses={404: SETTINGS_ERRORS[404]},
)
async def read_access_context(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[AccessContext]:
    rights = await project_access(db, principal, project_id)
    snapshot = await get_snapshot(db, project_id)
    writable = rights.project.archived_at is None
    invitation = None
    if rights.can_invite:
        access = rights.access
        invitation = InvitationDefaults(
            default_role=access["default_role"],
            allowed_roles=([ProjectRole.MANAGER] if rights.can_manage else [])
            + [ProjectRole.RESEARCHER, ProjectRole.REVIEWER],
            allowed_domains=access["domains"],
            configured_expiry_days=access["expiry_days"],
            effective_ttl_hours=invite_ttl_hours(access, request.app.state.settings),
        )
    return ok(
        AccessContext(
            settings_version=snapshot.version,
            permissions=AccessPermissions(
                can_invite=writable and rights.can_invite,
                can_view_invite_candidates=writable and rights.can_invite,
                can_manage_members=writable and rights.can_manage,
                can_manage_settings=writable and rights.can_manage,
                can_read_raw_datasets=rights.can_read_raw_data,
            ),
            invitation=invitation,
        )
    )


@router.get(
    "/{project_id}/settings",
    response_model=ApiResponse[ProjectSettingsData],
    summary="Read project settings",
    description=(
        "Every section with defaults filled in. `capabilities` says which fields the "
        "Platform applies today; the rest are stored for the web app only."
    ),
    responses=SETTINGS_ERRORS,
)
async def read_project_settings(
    project_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectSettingsData]:
    await require_project_access(db, principal, project_id, manage=True)
    return ok(await get_snapshot(db, project_id), "Project settings loaded")


@router.patch(
    "/{project_id}/settings/{section}",
    response_model=ApiResponse[ProjectSettingsData],
    summary="Save one settings section",
    description=(
        "Send the `version` you read and only the fields to change. A stale version answers "
        "409 SETTINGS_VERSION_CONFLICT; an archived project answers 409 PROJECT_ARCHIVED; an "
        "invalid value answers 422 VALIDATION_ERROR naming the field."
    ),
    responses={
        **SETTINGS_ERRORS,
        409: {
            "model": ErrorResponse,
            "description": "SETTINGS_VERSION_CONFLICT or PROJECT_ARCHIVED",
        },
        422: {
            "model": ErrorResponse,
            "description": "VALIDATION_ERROR, EMPTY_UPDATE or SETTING_UNSUPPORTED",
        },
    },
)
async def update_project_settings(
    project_id: UUID,
    section: str,
    body: ProjectSettingsPatch,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectSettingsData]:
    project, _ = await _manage_project(db, principal, project_id)
    ensure_writable_project(project)
    data = await patch_section(
        db,
        project_id=project_id,
        section=section,
        version=body.version,
        values=body.values,
        actor_user_id=principal.user.id,
        request_id=getattr(request.state, "request_id", None),
    )
    return ok(data, "Settings saved")
