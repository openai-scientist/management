"""The signed-in member's notice choices in one project."""

from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.responses import ApiResponse, ErrorResponse, ok
from management.core.sessions import Principal, require_active_csrf, require_active_principal
from management.db.session import get_db
from management.notifications.preferences import (
    NotificationPreferences,
    PreferencesPatch,
    patch_preferences,
    read_preferences,
    require_member,
)

router = APIRouter(prefix="/projects", tags=["notification-preferences"])
NOT_MEMBER = {404: {"model": ErrorResponse, "description": "You are not a member of the project"}}


@router.get(
    "/{project_id}/notification-preferences/me",
    response_model=ApiResponse[NotificationPreferences],
    summary="Your notice choices in this project",
    description=(
        "Defaults until you save, at version 0; reading never saves. Members choose in-app "
        "notices only. Email is one switch for the whole project, set by a Project Manager in "
        "project settings; `capabilities.project_email` says whether it is on. Notices about "
        "your own invitations and removal always arrive."
    ),
    responses=NOT_MEMBER,
)
async def read_my_preferences(
    project_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[NotificationPreferences]:
    await require_member(db, project_id, principal.user.id)
    return ok(await read_preferences(db, project_id, principal.user.id))


@router.patch(
    "/{project_id}/notification-preferences/me",
    response_model=ApiResponse[NotificationPreferences],
    summary="Change your notice choices in this project",
    description=(
        "Send `version` and only the fields to change. Turning off in-app stops new notices "
        "of that group; earlier ones stay. Turning on email (set per project), a digest or "
        "quiet hours answers 422 NOTIFICATION_PREFERENCE_UNSUPPORTED."
    ),
    responses={
        **NOT_MEMBER,
        409: {"model": ErrorResponse, "description": "NOTIFICATION_PREFERENCES_VERSION_CONFLICT"},
        422: {
            "model": ErrorResponse,
            "description": "EMPTY_UPDATE, NOTIFICATION_PREFERENCE_UNSUPPORTED or VALIDATION_ERROR",
        },
    },
)
async def change_my_preferences(
    project_id: UUID,
    body: PreferencesPatch,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[NotificationPreferences]:
    await require_member(db, project_id, principal.user.id)
    saved = await patch_preferences(db, project_id, principal.user.id, body)
    return ok(saved, "Preferences saved")
