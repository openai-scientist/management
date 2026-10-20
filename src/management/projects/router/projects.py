import asyncio
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from management.access.service import (
    active_manager_count,
    ensure_writable_project,
    invite_expired,
    is_platform_admin,
    lock_project_scope,
    require_project_access,
)
from management.audit.service import record_audit
from management.core.config import Settings
from management.core.errors import APIError
from management.core.responses import ApiResponse, ErrorResponse, ok, paginated
from management.core.roles import ProjectRole
from management.core.search import SearchTerm, matches
from management.core.sessions import (
    Principal,
    get_principal,
    normalize_email,
    require_active_csrf,
    require_active_principal,
    require_origin,
)
from management.core.urls import api_prefix
from management.db.session import get_db
from management.identity.models import User, UserStatus
from management.infra.email_sender import EmailSender, get_email_sender
from management.notifications.notification_stream import notifications_changed
from management.notifications.service import (
    drop_invitation_notices,
    drop_preferences,
    notify_project_members,
    notify_user,
)
from management.projects.models import Project, ProjectMembership
from management.projects.project_status import derive_project_status
from management.projects.schemas.projects import (
    ProjectCreate,
    ProjectInviteCandidate,
    ProjectItem,
    ProjectMemberCreate,
    ProjectMemberItem,
    ProjectMemberRoleUpdate,
    ProjectPatch,
    ProjectStatus,
)
from management.projects.service import (
    OPEN_STATUSES,
    _email_invitation,
    _invite_candidates,
    _locked_member,
    _manage_project,
    _member_item,
    _project_item,
    _send_invitation,
    ensure_invitable_email,
    ensure_invitable_role,
    project_access,
    require_invite_access,
)
from management.projects.settings_service import invite_ttl_hours
from management.research.models import ResearchRun, active_run_filter

router = APIRouter(prefix="/projects", tags=["projects"])


INVITE_CANDIDATES_KEEP_ALIVE_SECONDS = 15


PROJECT_ERRORS = {
    403: {"model": ErrorResponse, "description": "Project Manager role is required"},
    404: {"model": ErrorResponse, "description": "The project does not exist or is not yours"},
    409: {"model": ErrorResponse, "description": "The project is archived or the change conflicts"},
}


@router.get(
    "",
    response_model=ApiResponse[list[ProjectItem]],
    summary="List my projects",
    description=(
        "Projects you are a member of, newest first. A Platform Admin sees every project. "
        "Archived projects are left out unless `include_archived` is true."
    ),
)
async def list_projects(
    include_archived: bool = False,
    q: SearchTerm = None,
    status: ProjectStatus | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[list[ProjectItem]]:
    own_membership = and_(
        ProjectMembership.project_id == Project.id,
        ProjectMembership.user_id == principal.user.id,
        ProjectMembership.status == "active",
    )
    query = select(Project, ProjectMembership)
    if await is_platform_admin(db, principal.user.id):
        query = query.outerjoin(ProjectMembership, own_membership)
    else:
        query = query.join(ProjectMembership, own_membership)
    if not include_archived:
        query = query.where(Project.archived_at.is_(None))
    if status == "archived":
        query = query.where(Project.archived_at.is_not(None))
    elif status is not None:
        query = query.where(Project.archived_at.is_(None), Project.status == status)
    if q:
        query = query.where(matches(q, Project.name, Project.description))
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await db.execute(
            query.order_by(Project.created_at.desc(), Project.id).limit(limit).offset(offset)
        )
    ).all()
    return paginated(
        [_project_item(project, membership) for project, membership in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "",
    response_model=ApiResponse[ProjectItem],
    status_code=201,
    summary="Create a project",
    description=(
        "Any signed-in user can create a project; they own it and become its Project Manager."
    ),
)
async def create_project(
    body: ProjectCreate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    request_id = getattr(request.state, "request_id", None)
    project = Project(
        name=body.name,
        description=body.description,
        domain=body.domain,
        objective=body.objective,
        tags=body.tags,
        status="draft",
        owner_user_id=principal.user.id,
    )
    db.add(project)
    await db.flush()
    membership = ProjectMembership(
        project_id=project.id,
        user_id=principal.user.id,
        role_code=ProjectRole.MANAGER,
        status="active",
        created_by_user_id=principal.user.id,
    )
    db.add(membership)
    await db.flush()
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.created",
        resource_type="project",
        resource_id=project.id,
        project_id=project.id,
        request_id=request_id,
        details={"name": project.name},
    )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.member_added",
        resource_type="project_membership",
        resource_id=membership.id,
        project_id=project.id,
        request_id=request_id,
        details={"user_id": str(principal.user.id), "role": ProjectRole.MANAGER},
    )
    await db.flush()
    return ok(_project_item(project, membership), "Project created")


@router.get(
    "/{project_id}",
    response_model=ApiResponse[ProjectItem],
    summary="Get a project",
    responses={404: PROJECT_ERRORS[404]},
)
async def get_project_route(
    project_id: UUID,
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await require_project_access(db, principal, project_id)
    return ok(_project_item(project, membership))


@router.patch(
    "/{project_id}",
    response_model=ApiResponse[ProjectItem],
    summary="Update project details",
    responses=PROJECT_ERRORS,
)
async def update_project(
    project_id: UUID,
    body: ProjectPatch,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _manage_project(db, principal, project_id)
    ensure_writable_project(project)
    if not body.model_fields_set:
        raise APIError(422, "EMPTY_UPDATE", "Provide at least one project field to update")
    for field in body.model_fields_set:
        setattr(project, field, getattr(body, field))
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.updated",
        resource_type="project",
        resource_id=project.id,
        project_id=project.id,
        request_id=getattr(request.state, "request_id", None),
        details={"fields": sorted(body.model_fields_set)},
    )
    await db.flush()
    return ok(_project_item(project, membership), "Project updated")


@router.post(
    "/{project_id}/archive",
    response_model=ApiResponse[ProjectItem],
    summary="Archive a project (read-only, restorable)",
    responses=PROJECT_ERRORS,
)
async def archive_project(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _manage_project(db, principal, project_id)
    if project.archived_at is None:
        project.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="project.archived",
            resource_type="project",
            resource_id=project.id,
            project_id=project.id,
            request_id=getattr(request.state, "request_id", None),
        )
        await db.flush()
    return ok(_project_item(project, membership), "Project archived")


@router.post(
    "/{project_id}/restore",
    response_model=ApiResponse[ProjectItem],
    summary="Restore an archived project",
    responses=PROJECT_ERRORS,
)
async def restore_project(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _manage_project(db, principal, project_id)
    if project.archived_at is not None:
        project.archived_at = None
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="project.restored",
            resource_type="project",
            resource_id=project.id,
            project_id=project.id,
            request_id=getattr(request.state, "request_id", None),
        )
        await db.flush()
    return ok(_project_item(project, membership), "Project restored")


@router.post(
    "/{project_id}/complete",
    response_model=ApiResponse[ProjectItem],
    summary="Mark the project's research as completed",
    description="Project Manager only. Refused while a run is in progress.",
    responses=PROJECT_ERRORS,
)
async def complete_project(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _manage_project(db, principal, project_id)
    ensure_writable_project(project)
    if project.status != "completed":
        active = await db.scalar(
            select(ResearchRun.id).where(
                ResearchRun.project_id == project_id,
                active_run_filter(),
            )
        )
        if active is not None:
            raise APIError(409, "RUN_ACTIVE", "Wait for the run in progress to finish first")
        project.status = "completed"
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="project.completed",
            resource_type="project",
            resource_id=project.id,
            project_id=project.id,
            request_id=getattr(request.state, "request_id", None),
        )
        await db.flush()
    return ok(_project_item(project, membership), "Project completed")


@router.post(
    "/{project_id}/reopen",
    response_model=ApiResponse[ProjectItem],
    summary="Reopen a completed project",
    description="Project Manager only. The status goes back to what the project's data shows.",
    responses=PROJECT_ERRORS,
)
async def reopen_project(
    project_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[ProjectItem]:
    project, membership = await _manage_project(db, principal, project_id)
    ensure_writable_project(project)
    if project.status == "completed":
        project.status = await derive_project_status(db, project)
        record_audit(
            db,
            actor_user_id=principal.user.id,
            action="project.reopened",
            resource_type="project",
            resource_id=project.id,
            project_id=project.id,
            request_id=getattr(request.state, "request_id", None),
        )
        await db.flush()
    return ok(_project_item(project, membership), "Project reopened")


@router.get(
    "/{project_id}/members",
    response_model=ApiResponse[list[ProjectMemberItem]],
    summary="List project members and pending invitations",
    description="An item with `status: invited` has not accepted yet and has no access.",
    responses={404: PROJECT_ERRORS[404]},
)
async def list_project_members(
    project_id: UUID,
    q: SearchTerm = None,
    role: ProjectRole | None = None,
    status: Literal["invited", "active"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[list[ProjectMemberItem]]:
    await require_project_access(db, principal, project_id)
    filters = [
        ProjectMembership.project_id == project_id,
        ProjectMembership.status.in_([status] if status else OPEN_STATUSES),
    ]
    if role is not None:
        filters.append(ProjectMembership.role_code == role)
    if q:
        filters.append(matches(q, User.email, User.display_name))
    total = int(
        await db.scalar(
            select(func.count())
            .select_from(ProjectMembership)
            .join(User, User.id == ProjectMembership.user_id)
            .where(*filters)
        )
        or 0
    )
    rows = (
        await db.execute(
            select(ProjectMembership, User)
            .join(User, User.id == ProjectMembership.user_id)
            .where(*filters)
            .order_by(User.email_normalized)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return paginated(
        [_member_item(membership, user, prefix) for membership, user in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/{project_id}/invite-candidates",
    response_model=ApiResponse[list[ProjectInviteCandidate]],
    summary="List users available for a project invitation",
    description=(
        "Whoever may invite can choose active, email-verified regular users: Project Managers, "
        "Platform Admins, and Researchers when the access setting `inviters` is "
        "`researchers` (403 INVITE_NOT_ALLOWED otherwise). With allowed `domains` set, only "
        "emails at one of them are listed. "
        "Platform Admin accounts are excluded from the candidates. "
        "Existing active members and pending invitations (including expired ones) are "
        "excluded before pagination. Removed members and cancelled or declined invitations "
        "can be invited again. Archived projects are read-only."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "An active session is required"},
        **PROJECT_ERRORS,
    },
)
async def list_project_invite_candidates(
    project_id: UUID,
    q: SearchTerm = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_active_principal),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[list[ProjectInviteCandidate]]:
    rights = await require_invite_access(db, principal, project_id)
    ensure_writable_project(rights.project)
    return await _invite_candidates(
        db, project_id, q, limit, offset, prefix, rights.access["domains"]
    )


@router.get(
    "/{project_id}/invite-candidates/stream",
    response_class=StreamingResponse,
    summary="Follow project invitation candidates in realtime",
    description=(
        "Authenticated SSE for whoever may invite (see the GET list). An invite-candidates "
        "event contains the same envelope and filtered page as the GET list, immediately "
        "and after committed changes. Replace the page on every snapshot. session-ended "
        "or access-ended means close the stream. Reconnect to obtain current state."
    ),
    responses={
        200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
        401: {"model": ErrorResponse, "description": "An active session is required"},
        **PROJECT_ERRORS,
        503: {"model": ErrorResponse, "description": "Candidate realtime is unavailable"},
    },
)
async def stream_project_invite_candidates(
    project_id: UUID,
    request: Request,
    q: SearchTerm = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> StreamingResponse:
    if request.headers.get("Origin") is not None:
        require_origin(request)
    factory = request.app.state.session_factory
    prefix = api_prefix(request)

    async def authorize(db: AsyncSession) -> list[str]:
        """Checked again on every event, so a changed role or setting ends the stream."""
        principal = await get_principal(request, db)
        rights = await require_invite_access(db, principal, project_id)
        ensure_writable_project(rights.project)
        return rights.access["domains"]

    async with factory() as db:
        await authorize(db)
        await db.commit()
    hub = request.app.state.invite_candidates_hub
    try:
        await hub.start()
    except (SQLAlchemyError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        raise APIError(
            503, "INVITE_CANDIDATES_UNAVAILABLE", "Candidate realtime is unavailable"
        ) from exc

    async def events():
        async with hub.subscribe(project_id) as changes:
            refresh, last_domains = True, None
            while True:
                try:
                    # Never keep a database transaction/connection across an SSE wait.
                    async with factory() as db:
                        domains = await authorize(db)
                        if refresh or domains != last_domains:
                            last_domains, refresh = domains, True
                            snapshot = await _invite_candidates(
                                db, project_id, q, limit, offset, prefix, domains
                            )
                            await db.commit()
                except APIError as exc:
                    event_name = "session-ended" if exc.status_code == 401 else "access-ended"
                    yield f"event: {event_name}\ndata: {json.dumps({'code': exc.code})}\n\n"
                    return
                if refresh:
                    yield f"event: invite-candidates\ndata: {snapshot.model_dump_json()}\n\n"
                else:
                    yield ": keep-alive\n\n"
                try:
                    if not await asyncio.wait_for(
                        changes.get(), timeout=INVITE_CANDIDATES_KEEP_ALIVE_SECONDS
                    ):
                        return
                    refresh = True
                except TimeoutError:
                    # Only check authorization on heartbeats; do not poll candidates
                    # or persist activity touches that would extend the idle session.
                    refresh = False

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/{project_id}/members",
    response_model=ApiResponse[ProjectMemberItem],
    status_code=201,
    summary="Invite a registered user to the project",
    description=(
        "The person must already have an account. They are told in the app and by email "
        "and join with the given role once they accept; until then they have no access. "
        "The invitation can be accepted for as long as the project's access settings say "
        "(7 days unless changed). Who may invite follows the access setting `inviters` "
        "(403 INVITE_NOT_ALLOWED); a Researcher may not invite a Project Manager. An email "
        "outside the allowed `domains` answers 403 INVITE_DOMAIN_NOT_ALLOWED."
    ),
    responses=PROJECT_ERRORS,
)
async def add_project_member(
    project_id: UUID,
    body: ProjectMemberCreate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
    sender: EmailSender = Depends(get_email_sender),
) -> ApiResponse[ProjectMemberItem]:
    settings: Settings = request.app.state.settings
    await lock_project_scope(db, project_id)
    # Refuse callers who cannot invite before looking the email up, so the answer never
    # tells them whether an account exists. Not locked yet: user rows are locked before the
    # project row, the same order a suspension uses.
    current = await require_invite_access(db, principal, project_id)
    ensure_writable_project(current.project)
    ensure_invitable_role(current, body.role)
    ensure_invitable_email(current, str(body.email))
    user = await db.scalar(
        select(User)
        .where(
            User.email_normalized == normalize_email(str(body.email)),
            User.email_verified_at.is_not(None),
        )
        .with_for_update()
    )
    if user is None:
        raise APIError(
            404, "REGISTERED_USER_NOT_FOUND", "No registered user has this verified email"
        )
    if user.status == UserStatus.SUSPENDED:
        raise APIError(409, "USER_SUSPENDED", "A suspended user cannot be added to a project")
    rights = await require_invite_access(db, principal, project_id, lock=True)
    project = rights.project
    ensure_writable_project(project)
    existing = await db.scalar(
        select(ProjectMembership.id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user.id,
            ProjectMembership.status.in_(OPEN_STATUSES),
        )
    )
    if existing:
        raise APIError(
            409, "MEMBERSHIP_EXISTS", "User is already a project member or already invited"
        )
    membership = ProjectMembership(
        project_id=project_id,
        user_id=user.id,
        role_code=body.role,
        status="invited",
        created_by_user_id=principal.user.id,
    )
    _send_invitation(
        db, membership, principal.user.id, ttl_hours=invite_ttl_hours(rights.access, settings)
    )
    db.add(membership)
    await db.flush()
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.member_invited",
        resource_type="project_membership",
        resource_id=membership.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"user_id": str(user.id), "role": body.role},
    )
    # Commit first: the email must never describe an invitation that was rolled back.
    await db.commit()
    sent = await _email_invitation(
        settings, sender, project=project, membership=membership, user=user, inviter=principal.user
    )
    return ok(_member_item(membership, user, prefix, invite_email_sent=sent), "Invitation sent")


@router.post(
    "/{project_id}/members/{membership_id}/invite",
    response_model=ApiResponse[ProjectMemberItem],
    summary="Send an expired invitation again",
    description=(
        "Only an invitation that has expired can be sent again. It is valid again for as "
        "long as the project's access settings say. Project Manager only. An email outside "
        "the allowed `domains` now answers 403 INVITE_DOMAIN_NOT_ALLOWED. "
        "To reach the user sooner, cancel the invitation and invite them again."
    ),
    responses=PROJECT_ERRORS,
)
async def resend_project_invitation(
    project_id: UUID,
    membership_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
    sender: EmailSender = Depends(get_email_sender),
) -> ApiResponse[ProjectMemberItem]:
    settings: Settings = request.app.state.settings
    await lock_project_scope(db, project_id)
    await require_project_access(db, principal, project_id, manage=True)
    membership, user = await _locked_member(db, project_id, membership_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    rights = await project_access(db, principal, project_id)
    if membership.status != "invited":
        raise APIError(409, "INVITE_NOT_PENDING", "This user is already a project member")
    if user.status == UserStatus.SUSPENDED:
        raise APIError(409, "USER_SUSPENDED", "A suspended user cannot be invited")
    if not invite_expired(membership):
        raise APIError(
            409, "INVITE_STILL_VALID", "The invitation can be sent again once it has expired"
        )
    ensure_invitable_email(rights, user.email)
    # The new notice replaces the one about the expired invitation.
    await drop_invitation_notices(db, user.id, project_id)
    # Whoever sends it again is the inviter from now on, and is told the answer.
    membership.created_by_user_id = principal.user.id
    _send_invitation(
        db, membership, principal.user.id, ttl_hours=invite_ttl_hours(rights.access, settings)
    )
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.member_invite_sent",
        resource_type="project_membership",
        resource_id=membership.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"user_id": str(user.id), "role": membership.role_code},
    )
    await db.commit()
    sent = await _email_invitation(
        settings, sender, project=project, membership=membership, user=user, inviter=principal.user
    )
    return ok(
        _member_item(membership, user, prefix, invite_email_sent=sent), "Invitation sent again"
    )


@router.put(
    "/{project_id}/members/{membership_id}",
    response_model=ApiResponse[ProjectMemberItem],
    summary="Change the project role of a member or of a pending invitation",
    responses=PROJECT_ERRORS,
)
async def update_project_member_role(
    project_id: UUID,
    membership_id: UUID,
    body: ProjectMemberRoleUpdate,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
    prefix: str = Depends(api_prefix),
) -> ApiResponse[ProjectMemberItem]:
    await lock_project_scope(db, project_id)
    await require_project_access(db, principal, project_id, manage=True)
    membership, user = await _locked_member(db, project_id, membership_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    if membership.role_code == body.role:
        return ok(_member_item(membership, user, prefix))
    if (
        membership.status == "active"
        and membership.role_code == ProjectRole.MANAGER
        and user.status == UserStatus.ACTIVE
        and await active_manager_count(db, project_id) <= 1
    ):
        raise APIError(
            409, "LAST_PROJECT_MANAGER", "The last Project Manager role cannot be removed"
        )
    before = membership.role_code
    membership.role_code = body.role
    await db.flush()
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.member_role_changed",
        resource_type="project_membership",
        resource_id=membership.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"user_id": str(membership.user_id), "before": before, "after": body.role},
    )
    if membership.user_id != principal.user.id:
        # Reaches active members only: someone still invited sees the role in the invitation.
        await notify_project_members(
            db,
            project_id,
            "member_role_changed",
            actor_user_id=principal.user.id,
            only_user_id=membership.user_id,
        )
    return ok(_member_item(membership, user, prefix), "Project member role updated")


@router.delete(
    "/{project_id}/members/{membership_id}",
    response_model=ApiResponse[None],
    summary="Remove a member from the project, or cancel a pending invitation",
    responses=PROJECT_ERRORS,
)
async def remove_project_member(
    project_id: UUID,
    membership_id: UUID,
    request: Request,
    principal: Principal = Depends(require_active_csrf),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse[None]:
    await lock_project_scope(db, project_id)
    await require_project_access(db, principal, project_id, manage=True)
    membership, user = await _locked_member(db, project_id, membership_id)
    project, _ = await require_project_access(db, principal, project_id, manage=True, lock=True)
    ensure_writable_project(project)
    was_invitation = membership.status == "invited"
    if (
        not was_invitation
        and membership.role_code == ProjectRole.MANAGER
        and user.status == UserStatus.ACTIVE
        and await active_manager_count(db, project_id) <= 1
    ):
        raise APIError(409, "LAST_PROJECT_MANAGER", "The last Project Manager cannot be removed")
    membership.status = "revoked"
    membership.revoked_at = datetime.now(UTC)
    if was_invitation:
        await drop_invitation_notices(db, membership.user_id, project_id)
    else:
        await drop_preferences(db, project_id, membership.user_id)
        notifications_changed(db, membership.user_id)
    record_audit(
        db,
        actor_user_id=principal.user.id,
        action="project.member_invite_cancelled" if was_invitation else "project.member_revoked",
        resource_type="project_membership",
        resource_id=membership.id,
        project_id=project_id,
        request_id=getattr(request.state, "request_id", None),
        details={"user_id": str(membership.user_id)},
    )
    if (
        not was_invitation
        and membership.user_id != principal.user.id
        and user.status == UserStatus.ACTIVE
    ):
        notify_user(
            db,
            membership.user_id,
            "removed_from_project",
            project_id=project_id,
            actor_user_id=principal.user.id,
        )
    await db.flush()
    return ok(None, "Invitation cancelled" if was_invitation else "Project member removed")
