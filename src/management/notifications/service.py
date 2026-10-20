from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.roles import ProjectRole
from management.identity.models import User, UserStatus
from management.notifications.notification_stream import notifications_changed
from management.notifications.preferences import (
    MANDATORY_KINDS,
    muted_users,
    project_email_enabled,
)
from management.projects.models import (
    Notification,
    NotificationEmail,
    NotificationPreference,
    ProjectMembership,
)


def notify_user(
    db: AsyncSession,
    recipient_user_id: UUID,
    kind: str,
    *,
    project_id: UUID,
    run_id: UUID | None = None,
    actor_user_id: UUID | None = None,
) -> None:
    """Send a notice about the user's own access, which preferences cannot turn off.

    Every other kind goes through ``notify_project_members``, which honours them.
    """
    if kind not in MANDATORY_KINDS:
        raise ValueError(f"{kind} notices follow preferences; use notify_project_members")
    _add(
        db,
        recipient_user_id,
        kind,
        project_id=project_id,
        run_id=run_id,
        actor_user_id=actor_user_id,
    )


def _add(
    db: AsyncSession,
    recipient_user_id: UUID,
    kind: str,
    *,
    project_id: UUID,
    run_id: UUID | None,
    actor_user_id: UUID | None,
) -> None:
    db.add(
        Notification(
            recipient_user_id=recipient_user_id,
            kind=kind,
            project_id=project_id,
            run_id=run_id,
            actor_user_id=actor_user_id,
        )
    )
    notifications_changed(db, recipient_user_id)


async def drop_invitation_notices(
    db: AsyncSession, user_id: UUID, project_id: UUID, *, joined: bool = False
) -> None:
    """Remove the user's invitation notices for a project.

    Called when the invitation is answered, cancelled or sent again, so an old notice
    never comes back with a later invitation to the same project. Someone who joins
    again also loses the notice that they were once removed.
    """
    kinds = ["project_invited", "removed_from_project"] if joined else ["project_invited"]
    await db.execute(
        delete(Notification).where(
            Notification.recipient_user_id == user_id,
            Notification.project_id == project_id,
            Notification.kind.in_(kinds),
        )
    )
    notifications_changed(db, user_id)


async def notify_project_members(
    db: AsyncSession,
    project_id: UUID,
    kind: str,
    *,
    run_id: UUID | None = None,
    actor_user_id: UUID | None = None,
    roles: Iterable[ProjectRole] | None = None,
    exclude_user_id: UUID | None = None,
    only_user_id: UUID | None = None,
) -> None:
    """Notify the project's active members, in the caller's transaction.

    People removed from the project and suspended users get nothing, also when
    ``only_user_id`` names them. Members who turned this kind of notice off get no in-app
    notice. While the project's email switch is on, every recipient also gets it by email,
    queued here and sent after commit.
    """
    query = (
        select(ProjectMembership.user_id)
        .join(User, User.id == ProjectMembership.user_id)
        .where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.status == "active",
            User.status == UserStatus.ACTIVE,
        )
    )
    if roles is not None:
        query = query.where(ProjectMembership.role_code.in_(list(roles)))
    if exclude_user_id is not None:
        query = query.where(ProjectMembership.user_id != exclude_user_id)
    if only_user_id is not None:
        query = query.where(ProjectMembership.user_id == only_user_id)
    recipients = list((await db.scalars(query)).all())
    if not recipients:
        return
    muted = await muted_users(db, project_id, kind, recipients)
    email = await project_email_enabled(db, project_id)
    for user_id in recipients:
        if user_id not in muted:
            _add(
                db, user_id, kind, project_id=project_id, run_id=run_id, actor_user_id=actor_user_id
            )
        if email:
            db.add(
                NotificationEmail(
                    recipient_user_id=user_id,
                    kind=kind,
                    project_id=project_id,
                    run_id=run_id,
                    actor_user_id=actor_user_id,
                )
            )


async def drop_preferences(db: AsyncSession, project_id: UUID, user_id: UUID) -> None:
    """Forget a member's notice choices when they leave; joining again starts from defaults."""
    await db.execute(
        delete(NotificationPreference).where(
            NotificationPreference.project_id == project_id,
            NotificationPreference.user_id == user_id,
        )
    )
