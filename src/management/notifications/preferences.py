"""A member's notice choices in one project, and which notices they turn off.

Members choose in-app notices only. Email is switched per group for the whole project by a
Project Manager in project settings, and shown here read-only. Digests and quiet hours are
stored with their defaults and reported as not supported; turning them on is refused, as is
a member's own email.
"""

import re
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from management.core.errors import APIError
from management.projects.models import NotificationPreference, ProjectMembership, ProjectSetting

EventKey = Literal["review", "finished", "comments", "members", "budget", "integrations"]

# Group: label, default in-app, default email. The order is the order shown.
GROUPS: dict[str, tuple[str, bool, bool]] = {
    "review": ("Review requests", True, True),
    "finished": ("Run results", True, False),
    "comments": ("Comments", True, True),
    "members": ("Members", True, False),
    "budget": ("Budget alerts", True, True),
    "integrations": ("Integration failures", True, True),
}
KIND_GROUP = {
    "run_awaiting_review": "review",
    "run_finished": "finished",
    "run_commented": "comments",
    "added_to_project": "members",
    "invite_accepted": "members",
    "invite_declined": "members",
    "member_role_changed": "members",
    "budget_warning": "budget",
    "budget_exceeded": "budget",
    "integration_failed": "integrations",
}
# Notices about the member's own access. They always arrive.
MANDATORY_KINDS = frozenset({"project_invited", "removed_from_project"})

# The project's timezone when its settings never named one (GeneralSettings default).
DEFAULT_TIMEZONE = "Asia/Ho_Chi_Minh"
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# Members have no email choice: the Project Manager switches email per group for the project.
EMAIL_BY_PROJECT = "EMAIL_SET_BY_PROJECT"


class ChannelFlags(BaseModel):
    in_app: bool
    email: bool


class ChannelReasons(BaseModel):
    in_app: str | None
    email: str | None


class EventPreference(BaseModel):
    key: EventKey
    label: str
    in_app: bool
    email: bool = Field(description="The project's email switch for this group (Manager-set)")
    supported: ChannelFlags
    reason_codes: ChannelReasons


def _zone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Use an IANA timezone such as Asia/Ho_Chi_Minh") from exc
    return value


def _time(value: str) -> str:
    if not TIME.match(value):
        raise ValueError("Use a 24-hour time as HH:mm")
    return value


class Digest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["off", "daily", "weekly"]
    timezone: str
    send_at: str
    weekday: int = Field(ge=1, le=7, description="ISO weekday, 1 is Monday; weekly only")

    _zone = field_validator("timezone")(_zone)
    _time = field_validator("send_at")(_time)


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    timezone: str
    start: str
    end: str = Field(description="May be earlier than start, for hours past midnight")

    _zone = field_validator("timezone")(_zone)
    _time = field_validator("start", "end")(_time)


class Capability(BaseModel):
    supported: bool
    reason_code: str | None


class NotificationPreferences(BaseModel):
    project_id: str
    version: int
    updated_at: datetime | None
    events: list[EventPreference]
    digest: Digest
    quiet_hours: QuietHours
    capabilities: dict[str, Capability]


class ChannelPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_app: bool = None  # type: ignore[assignment]  # optional, but never null
    email: bool = None  # type: ignore[assignment]


class DigestPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    frequency: Literal["off", "daily", "weekly"] = None  # type: ignore[assignment]
    timezone: str = None  # type: ignore[assignment]
    send_at: str = None  # type: ignore[assignment]
    weekday: int = None  # type: ignore[assignment]


class QuietHoursPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = None  # type: ignore[assignment]
    timezone: str = None  # type: ignore[assignment]
    start: str = None  # type: ignore[assignment]
    end: str = None  # type: ignore[assignment]


class PreferencesPatch(BaseModel):
    """Only the fields to change. Objects merge field by field."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0)
    events: dict[EventKey, ChannelPatch] = None  # type: ignore[assignment]
    digest: DigestPatch = None  # type: ignore[assignment]
    quiet_hours: QuietHoursPatch = None  # type: ignore[assignment]


def _defaults(timezone: str) -> tuple[dict, dict, dict]:
    events = {key: {"in_app": app, "email": email} for key, (_, app, email) in GROUPS.items()}
    digest = {"frequency": "off", "timezone": timezone, "send_at": "08:00", "weekday": 1}
    quiet = {"enabled": False, "timezone": timezone, "start": "22:00", "end": "08:00"}
    return events, digest, quiet


async def _sections(db: AsyncSession, project_id: UUID) -> dict:
    sections = await db.scalar(
        select(ProjectSetting.sections).where(ProjectSetting.project_id == project_id)
    )
    return sections or {}


def _timezone(sections: dict) -> str:
    return (sections.get("general") or {}).get("timezone") or DEFAULT_TIMEZONE


def _email_groups(sections: dict) -> frozenset[str]:
    """The groups a Project Manager turned email on for; none until they do.

    Reads the stored JSON directly (projects' service sits above this layer); keep in step
    with ``NotificationSettings`` and its all-off defaults.
    """
    stored = sections.get("notifications") or {}
    return frozenset(key for key in GROUPS if stored.get(key) is True)


async def project_email_groups(db: AsyncSession, project_id: UUID) -> frozenset[str]:
    return _email_groups(await _sections(db, project_id))


def emails_kind(groups: frozenset[str], kind: str) -> bool:
    """Whether notices of this kind are emailed under the project's switches."""
    return KIND_GROUP.get(kind) in groups


async def require_member(db: AsyncSession, project_id: UUID, user_id: UUID) -> None:
    """Preferences belong to active members. Anyone else, admins included, gets 404."""
    member = await db.scalar(
        select(ProjectMembership.user_id).where(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == user_id,
            ProjectMembership.status == "active",
        )
    )
    if member is None:
        raise APIError(404, "NOT_FOUND", "Project not found")


async def _row(
    db: AsyncSession, project_id: UUID, user_id: UUID, *, lock: bool = False
) -> NotificationPreference | None:
    query = select(NotificationPreference).where(
        NotificationPreference.project_id == project_id,
        NotificationPreference.user_id == user_id,
    )
    if lock:
        query = query.with_for_update()
    return await db.scalar(query.execution_options(populate_existing=True))


def _view(
    project_id: UUID,
    row: NotificationPreference | None,
    timezone: str,
    email_groups: frozenset[str],
) -> NotificationPreferences:
    events, digest, quiet = _defaults(timezone)
    if row is not None:
        events = {key: {**value, **row.events.get(key, {})} for key, value in events.items()}
        digest, quiet = {**digest, **row.digest}, {**quiet, **row.quiet_hours}
    for key, channel in events.items():
        # Email is the project's switch for the group, never the member's own.
        channel["email"] = key in email_groups
    return NotificationPreferences(
        project_id=str(project_id),
        version=row.version if row else 0,
        updated_at=row.updated_at if row else None,
        events=[
            EventPreference(
                key=key,
                label=GROUPS[key][0],
                **events[key],
                supported=ChannelFlags(in_app=True, email=False),
                reason_codes=ChannelReasons(in_app=None, email=EMAIL_BY_PROJECT),
            )
            for key in GROUPS
        ],
        digest=Digest(**digest),
        quiet_hours=QuietHours(**quiet),
        capabilities={
            "digest": Capability(supported=False, reason_code="DIGEST_NOT_AVAILABLE"),
            "quiet_hours": Capability(supported=False, reason_code="QUIET_HOURS_NOT_AVAILABLE"),
        },
    )


async def read_preferences(
    db: AsyncSession, project_id: UUID, user_id: UUID
) -> NotificationPreferences:
    row = await _row(db, project_id, user_id)
    sections = await _sections(db, project_id)
    return _view(project_id, row, _timezone(sections), _email_groups(sections))


def _unsupported(body: PreferencesPatch) -> str | None:
    sent = body.model_dump(exclude_unset=True)
    if any(channel.get("email") is True for channel in (sent.get("events") or {}).values()):
        return "Email notices are set for the whole project by its Project Manager. Keep email off."
    if (sent.get("digest") or {}).get("frequency") not in (None, "off"):
        return "Digests are not available yet. Keep the digest off."
    if (sent.get("quiet_hours") or {}).get("enabled") is True:
        return "Quiet hours are not available yet. Keep them off."
    return None


async def patch_preferences(
    db: AsyncSession, project_id: UUID, user_id: UUID, body: PreferencesPatch
) -> NotificationPreferences:
    sent = body.model_dump(exclude_unset=True)
    sent.pop("version")
    changes = [*(sent.get("events") or {}).values(), sent.get("digest"), sent.get("quiet_hours")]
    if not any(changes):
        raise APIError(422, "EMPTY_UPDATE", "Provide at least one preference to change")
    message = _unsupported(body)
    if message:
        raise APIError(422, "NOTIFICATION_PREFERENCE_UNSUPPORTED", message)

    row = await _row(db, project_id, user_id, lock=True)
    if body.version != (row.version if row else 0):
        raise APIError(
            409,
            "NOTIFICATION_PREFERENCES_VERSION_CONFLICT",
            "These preferences were changed in another tab. Reload to see them.",
        )
    sections = await _sections(db, project_id)
    current = _view(project_id, row, _timezone(sections), _email_groups(sections))
    # Only the member's own channel is saved; email is read from project settings.
    events = {e.key: {"in_app": e.in_app} for e in current.events}
    for key, channel in (sent.get("events") or {}).items():
        if "in_app" in channel:
            events[key] = {"in_app": channel["in_app"]}
    merged = {}
    for name, model, saved in (
        ("digest", Digest, current.digest),
        ("quiet_hours", QuietHours, current.quiet_hours),
    ):
        try:
            merged[name] = model(**{**saved.model_dump(), **(sent.get(name) or {})})
        except ValidationError as exc:
            raise RequestValidationError(
                [{**error, "loc": ("body", name, *error["loc"])} for error in exc.errors()]
            ) from exc
    digest, quiet = merged["digest"], merged["quiet_hours"]
    if quiet.start == quiet.end:
        raise APIError(422, "VALIDATION_ERROR", "Quiet hours must start and end at different times")

    now = datetime.now(UTC)
    if row is None:
        # The timezone is copied now, so a later change to the project's does not move it.
        row = NotificationPreference(project_id=project_id, user_id=user_id, version=0)
        db.add(row)
    row.events, row.digest, row.quiet_hours = events, digest.model_dump(), quiet.model_dump()
    row.version += 1
    row.updated_at = now
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise APIError(
            409,
            "NOTIFICATION_PREFERENCES_VERSION_CONFLICT",
            "These preferences were changed in another tab. Reload to see them.",
        ) from exc
    return _view(project_id, row, digest.timezone, _email_groups(sections))


async def muted_users(
    db: AsyncSession, project_id: UUID, kind: str, user_ids: list[UUID]
) -> set[UUID]:
    """The users among ``user_ids`` who turned off in-app notices of this kind."""
    group = KIND_GROUP.get(kind)
    if group is None or not user_ids:
        return set()
    rows = await db.execute(
        select(NotificationPreference.user_id, NotificationPreference.events).where(
            NotificationPreference.project_id == project_id,
            NotificationPreference.user_id.in_(user_ids),
        )
    )
    return {uid for uid, events in rows if (events.get(group) or {}).get("in_app") is False}
