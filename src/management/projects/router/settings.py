"""Project settings: one GET for every section, one PATCH per section."""

from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import ensure_writable_project, require_project_access
from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.projects.schemas.settings import ProjectSettingsData, ProjectSettingsPatch
from management.projects.service import _manage_project
from management.projects.settings_service import get_snapshot, patch_section

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
