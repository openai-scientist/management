from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    invite_expired,
    is_platform_admin,
    lock_project_scope,
    lock_user,
    require_project_access,
)
from management.core.config import Settings
from management.core.errors import APIError
from management.core.responses import ApiResponse, paginated
from management.core.roles import ProjectRole
from management.core.search import matches
from management.core.sessions import (
    Principal,
)
from management.core.urls import avatar_url
from management.identity.models import User, UserPlatformRole, UserStatus
from management.infra.email_sender import EmailSender
from management.infra.email_templates import project_invite
from management.notifications.service import (
    notify_user,
)
from management.projects.models import Project, ProjectMembership
from management.projects.schemas.projects import (
    ProjectInviteCandidate,
    ProjectItem,
    ProjectMemberItem,
)
from management.projects.settings_service import effective_section, email_domain_allowed
from management.research.models import ResearchRun


async def get_project_run(db: AsyncSession, project_id: UUID, run_id: UUID) -> ResearchRun:
    """The run of this project, or a 404. Comments need only the run, not the research service."""
    run = await db.scalar(
        select(ResearchRun).where(ResearchRun.id == run_id, ResearchRun.project_id == project_id)
    )
    if run is None:
        raise APIError(404, "NOT_FOUND", "Run was not found")
    return run


def _project_item(project: Project, membership: ProjectMembership | None) -> ProjectItem:
    return ProjectItem(
        id=str(project.id),
        name=project.name,
        description=project.description,
        domain=project.domain,
        objective=project.objective,
        tags=project.tags,
        status="archived" if project.archived_at is not None else project.status,
        owner_user_id=str(project.owner_user_id),
        my_role=membership.role_code if membership else None,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def _member_item(
    membership: ProjectMembership,
    user: User,
    prefix: str,
    *,
    invite_email_sent: bool | None = None,
) -> ProjectMemberItem:
    return ProjectMemberItem(
        id=str(membership.id),
        user_id=str(user.id),
        email=user.email,
        display_name=user.display_name,
        avatar_url=avatar_url(prefix, user.id, user.avatar_storage_key),
        role=membership.role_code,
        status=membership.status,
        invite_sent_at=membership.invite_sent_at,
        invite_expires_at=membership.invite_expires_at,
        invite_expired=invite_expired(membership),
        invite_email_sent=invite_email_sent,
        created_at=membership.created_at,
    )


OPEN_STATUSES = ("invited", "active")


def _send_invitation(
    db: AsyncSession, membership: ProjectMembership, actor_user_id: UUID, *, ttl_hours: int
) -> None:
    """Start the invitation's validity and tell the user in the app, replacing older notices."""
    now = datetime.now(UTC)
    membership.invite_sent_at = now
    membership.invite_expires_at = now + timedelta(hours=ttl_hours)
    if membership.user_id != actor_user_id:
        notify_user(
            db,
            membership.user_id,
            "project_invited",
            project_id=membership.project_id,
            actor_user_id=actor_user_id,
        )


async def _email_invitation(
    settings: Settings,
    sender: EmailSender,
    *,
    project: Project,
    membership: ProjectMembership,
    user: User,
    inviter: User,
) -> bool:
    subject, text, html = project_invite(
        recipient_name=user.display_name or user.email,
        project_name=project.name,
        role=membership.role_code,
        inviter_name=inviter.display_name or inviter.email,
        expires_at=membership.invite_expires_at,
        # The length this invitation was given, even if the project's setting changed since.
        expires_hours=round(
            (membership.invite_expires_at - membership.invite_sent_at).total_seconds() / 3600
        ),
        app_url=settings.app_url,
    )
    return await sender.send(to=user.email, subject=subject, text=text, html=html)


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """What the caller may do in one project under its settings as they are now."""

    project: Project
    membership: ProjectMembership | None
    is_admin: bool
    access: dict[str, Any]

    @property
    def role(self) -> str | None:
        return self.membership.role_code if self.membership else None

    @property
    def can_manage(self) -> bool:
        return self.is_admin or self.role == ProjectRole.MANAGER

    @property
    def can_invite(self) -> bool:
        return self.can_manage or (
            self.role == ProjectRole.RESEARCHER and self.access["inviters"] == "researchers"
        )

    @property
    def can_read_raw_data(self) -> bool:
        return (
            self.is_admin or self.role != ProjectRole.REVIEWER or self.access["reviewers_see_data"]
        )


async def project_access(
    db: AsyncSession, principal: Principal, project_id: UUID, *, lock: bool = False
) -> ProjectAccess:
    """The project and the caller's rights in it; 404 for anyone outside it."""
    project, membership = await require_project_access(db, principal, project_id, lock=lock)
    return ProjectAccess(
        project=project,
        membership=membership,
        is_admin=await is_platform_admin(db, principal.user.id),
        access=await effective_section(db, project_id, "access"),
    )


async def require_invite_access(
    db: AsyncSession, principal: Principal, project_id: UUID, *, lock: bool = False
) -> ProjectAccess:
    """Access for someone the project's `inviters` setting lets invite; 403 for other members."""
    rights = await project_access(db, principal, project_id, lock=lock)
    if not rights.can_invite:
        raise APIError(403, "INVITE_NOT_ALLOWED", "You can't invite people to this project")
    return rights


def ensure_invitable_role(rights: ProjectAccess, role: str) -> None:
    """A Researcher who may invite gives out Researcher or Reviewer, never Project Manager."""
    if role == ProjectRole.MANAGER and not rights.can_manage:
        raise APIError(
            403, "INVITE_NOT_ALLOWED", "Only a Project Manager can invite a Project Manager"
        )


def ensure_invitable_email(rights: ProjectAccess, email: str) -> None:
    if not email_domain_allowed(email, rights.access["domains"]):
        raise APIError(
            403,
            "INVITE_DOMAIN_NOT_ALLOWED",
            "This project only accepts people with an email at "
            + ", ".join(rights.access["domains"]),
        )


async def _manage_project(
    db: AsyncSession, principal: Principal, project_id: UUID
) -> tuple[Project, ProjectMembership | None]:
    """Serialize and authorize a change to a project by its manager or a Platform Admin."""
    await lock_project_scope(db, project_id)
    return await require_project_access(db, principal, project_id, manage=True, lock=True)


async def _locked_member(db: AsyncSession, project_id: UUID, membership_id: UUID):
    membership_filter = (
        ProjectMembership.id == membership_id,
        ProjectMembership.project_id == project_id,
        ProjectMembership.status.in_(OPEN_STATUSES),
    )
    target_user_id = await db.scalar(select(ProjectMembership.user_id).where(*membership_filter))
    if target_user_id is None:
        raise APIError(404, "NOT_FOUND", "Project member was not found")
    user = await lock_user(db, target_user_id)
    membership = await db.scalar(
        select(ProjectMembership)
        .where(*membership_filter)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if membership is None:
        raise APIError(404, "NOT_FOUND", "Project member was not found")
    return membership, user


async def _invite_candidates(
    db: AsyncSession,
    project_id: UUID,
    q: str | None,
    limit: int,
    offset: int,
    prefix: str,
    domains: list[str],
) -> ApiResponse[list[ProjectInviteCandidate]]:
    existing_membership = (
        select(ProjectMembership.id)
        .where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == User.id,
            ProjectMembership.status.in_(OPEN_STATUSES),
        )
        .exists()
    )
    filters = [
        User.status == UserStatus.ACTIVE,
        User.email_verified_at.is_not(None),
        User.id.not_in(select(UserPlatformRole.user_id)),
        ~existing_membership,
    ]
    if domains:
        # Exact domain after the @, the same rule an invitation is checked against.
        filters.append(
            or_(*(User.email_normalized.endswith(f"@{d}", autoescape=True) for d in domains))
        )
    if q:
        filters.append(matches(q, User.email, User.display_name))
    total = int(await db.scalar(select(func.count()).select_from(User).where(*filters)) or 0)
    users = (
        await db.scalars(
            select(User)
            .where(*filters)
            .order_by(User.email_normalized, User.id)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return paginated(
        [
            ProjectInviteCandidate(
                id=str(user.id),
                email=user.email,
                display_name=user.display_name,
                avatar_url=avatar_url(prefix, user.id, user.avatar_storage_key),
            )
            for user in users
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
