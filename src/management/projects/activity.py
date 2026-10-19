"""The project activity feed: a safe, readable projection of the project's audit events.

Only actions listed in ``ACTIONS`` are shown. Raw audit ``details`` never leave this module:
each item carries a normalized action, an English summary, the actor, the resource it is
about and, for settings changes, the allowlisted field changes.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from management.audit.models import AuditEvent
from management.connections.models import DataConnection
from management.core.urls import avatar_url
from management.datasets.models import Dataset
from management.identity.models import User
from management.integrations.models import ProjectIntegration
from management.projects.models import ProjectFile
from management.projects.schemas.activity import (
    ActivityActor,
    ActivityCategory,
    ActivityChange,
    ActivityItem,
    ActivityResource,
)
from management.projects.settings_service import PRIVATE_FIELDS
from management.research.models import ResearchRun

# Detail keys that may name a resource that no longer exists. Nothing else from `details`
# is ever shown: other keys can hold old values, emails, hosts or file hashes.
LABEL_KEYS = ("name", "title", "filename", "topic")

RUN_STATUS_ACTIONS = {
    "completed": ("run.completed", "completed a research run"),
    "failed": ("run.failed", "research run failed"),
    "stopped": ("run.stopped", "stopped a research run"),
    "awaiting_review": ("run.awaiting_review", "research run is waiting for review"),
}

DECISION_SUMMARIES = {
    "answer_gate": "answered a question in a research run",
    "approve": "approved a research run step",
    "comment": "commented on a research run step",
    "accept": "accepted a research run stage",
    "send_back": "sent a research run back to an earlier stage",
    "rerun": "ran a research run stage again",
    "stop": "stopped a research run",
}

Describe = Callable[[dict[str, Any]], tuple[str, str]]


@dataclass(frozen=True)
class Entry:
    category: ActivityCategory
    # Which resource the item points at: decides the label lookup and the resource type.
    resource: str
    describe: Describe


def _fixed(action: str, summary: str) -> Describe:
    return lambda _details: (action, summary)


def _run_status(details: dict[str, Any]) -> tuple[str, str]:
    return RUN_STATUS_ACTIONS.get(
        details.get("after"), ("run.status_changed", "changed a research run's status")
    )


def _decision(details: dict[str, Any]) -> tuple[str, str]:
    decision = details.get("decision")
    return (
        f"run.decision.{decision}" if decision in DECISION_SUMMARIES else "run.decision",
        DECISION_SUMMARIES.get(decision, "recorded a decision on a research run"),
    )


ACTIONS: dict[str, Entry] = {
    # research
    "run.created": Entry("research", "run", _fixed("run.created", "started a research run")),
    "run.status_changed": Entry("research", "run", _run_status),
    "run.paused": Entry("research", "run", _fixed("run.paused", "paused a research run")),
    "run.resumed": Entry("research", "run", _fixed("run.resumed", "resumed a research run")),
    "run.abandoned": Entry("research", "run", _fixed("run.abandoned", "abandoned a research run")),
    "run.gate_answered": Entry(
        "research", "run", _fixed("run.gate_answered", "answered a question in a research run")
    ),
    "run.decision_recorded": Entry("research", "run", _decision),
    "frame_review.submitted": Entry(
        "research", "run_from_details", _fixed("review.submitted", "submitted a review")
    ),
    "comment.created": Entry(
        "research", "run_from_details", _fixed("comment.created", "commented on a research run")
    ),
    "comment.deleted": Entry(
        "research", "run_from_details", _fixed("comment.deleted", "deleted a comment")
    ),
    # data
    "dataset.created": Entry("data", "dataset", _fixed("dataset.created", "added a dataset")),
    "dataset.version_added": Entry(
        "data", "dataset_from_details", _fixed("dataset.version_added", "added a dataset version")
    ),
    "dataset.updated": Entry("data", "dataset", _fixed("dataset.updated", "updated a dataset")),
    "dataset.deleted": Entry("data", "dataset", _fixed("dataset.deleted", "deleted a dataset")),
    "project_file.uploaded": Entry(
        "data", "project_file", _fixed("file.uploaded", "uploaded a file")
    ),
    "project_file.deleted": Entry("data", "project_file", _fixed("file.deleted", "deleted a file")),
    "connection.created": Entry(
        "data", "data_connection", _fixed("connection.created", "added a data connection")
    ),
    "connection.renamed": Entry(
        "data", "data_connection", _fixed("connection.renamed", "renamed a data connection")
    ),
    "connection.reauthorized": Entry(
        "data",
        "data_connection",
        _fixed("connection.reauthorized", "reconnected a data connection"),
    ),
    "connection.import_started": Entry(
        "data", "data_connection", _fixed("connection.import_started", "imported data")
    ),
    "connection.deleted": Entry(
        "data", "data_connection", _fixed("connection.deleted", "deleted a data connection")
    ),
    # members: an accepted invitation is recorded as `member_added`, so it shows once.
    "project.member_invited": Entry(
        "members", "member", _fixed("member.invited", "invited a member")
    ),
    "project.member_invite_sent": Entry(
        "members", "member", _fixed("member.invite_resent", "sent an invitation again")
    ),
    "project.member_invite_declined": Entry(
        "members", "member", _fixed("member.invite_declined", "declined an invitation")
    ),
    "project.member_invite_cancelled": Entry(
        "members", "member", _fixed("member.invite_cancelled", "cancelled an invitation")
    ),
    "project.member_added": Entry(
        "members", "member", _fixed("member.joined", "joined the project")
    ),
    "project.member_role_changed": Entry(
        "members", "member", _fixed("member.role_changed", "changed a member's role")
    ),
    "project.member_revoked": Entry(
        "members", "member", _fixed("member.removed", "removed a member")
    ),
    "project.ownership_transferred": Entry(
        "members",
        "project",
        _fixed("project.ownership_transferred", "transferred project ownership"),
    ),
    # settings
    "project.settings_updated": Entry(
        "settings", "project", _fixed("settings.updated", "changed project settings")
    ),
    "project.updated": Entry(
        "settings", "project", _fixed("project.updated", "updated the project details")
    ),
    "project.archived": Entry(
        "settings", "project", _fixed("project.archived", "archived the project")
    ),
    "project.restored": Entry(
        "settings", "project", _fixed("project.restored", "restored the project")
    ),
    "project.completed": Entry(
        "settings", "project", _fixed("project.completed", "marked the project complete")
    ),
    "project.reopened": Entry(
        "settings", "project", _fixed("project.reopened", "reopened the project")
    ),
    "integration.created": Entry(
        "settings", "project_integration", _fixed("integration.created", "added an integration")
    ),
    "integration.updated": Entry(
        "settings", "project_integration", _fixed("integration.updated", "updated an integration")
    ),
    "integration.deleted": Entry(
        "settings", "project_integration", _fixed("integration.deleted", "deleted an integration")
    ),
    "integration.auto_disabled": Entry(
        "settings",
        "project_integration",
        _fixed("integration.auto_disabled", "integration was turned off after failures"),
    ),
}


def actions_for(category: ActivityCategory | None) -> list[str]:
    return [
        action
        for action, entry in ACTIONS.items()
        if category is None or entry.category == category
    ]


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def _target(row: AuditEvent, entry: Entry) -> tuple[str, UUID | None]:
    """The resource type shown to the client and the id to look up."""
    details = row.details or {}
    match entry.resource:
        case "run_from_details":
            return "research_run", _uuid(details.get("run_id"))
        case "dataset_from_details":
            return "dataset", _uuid(details.get("dataset_id"))
        case "member":
            return "user", _uuid(details.get("user_id"))
        case "run":
            return "research_run", _uuid(row.resource_id)
        case "project":
            return "project", _uuid(row.resource_id)
        case other:
            return other, _uuid(row.resource_id)


def _changes(row: AuditEvent) -> list[ActivityChange]:
    """Settings changes by field. Private fields name the change but never their values."""
    if row.action != "project.settings_updated":
        return []
    details = row.details or {}
    section = details.get("section")
    values = details.get("values") or {}
    changes = []
    for field in details.get("changed_fields") or []:
        value = values.get(field) if field not in PRIVATE_FIELDS else None
        changes.append(
            ActivityChange(
                field=f"{section}.{field}",
                before=value.get("from") if isinstance(value, dict) else None,
                after=value.get("to") if isinstance(value, dict) else None,
            )
        )
    return changes


LABEL_QUERIES = {
    "research_run": (ResearchRun, ResearchRun.topic),
    "dataset": (Dataset, Dataset.name),
    "data_connection": (DataConnection, DataConnection.name),
    "project_file": (ProjectFile, ProjectFile.original_filename),
    "project_integration": (ProjectIntegration, ProjectIntegration.name),
    "user": (User, User.display_name),
}


async def _labels(
    db: AsyncSession, project_id: UUID, project_name: str, wanted: dict[str, set[UUID]]
) -> dict[tuple[str, UUID], str]:
    """Current names of the resources that still exist, keyed by (type, id)."""
    found: dict[tuple[str, UUID], str] = {}
    for kind, ids in wanted.items():
        if kind == "project":
            found.update({(kind, i): project_name for i in ids if i == project_id})
            continue
        model, column = LABEL_QUERIES[kind]
        query = select(model.id, column).where(model.id.in_(ids))
        if kind != "user":
            # A resource of another project is never named here, even if an id points to it.
            query = query.where(model.project_id == project_id)
        for row_id, name in (await db.execute(query)).all():
            found[(kind, row_id)] = name or ""
    return found


async def list_activity(
    db: AsyncSession,
    *,
    project_id: UUID,
    project_name: str,
    category: ActivityCategory | None,
    from_time: datetime | None,
    to_time: datetime | None,
    limit: int,
    offset: int,
    prefix: str,
) -> tuple[list[ActivityItem], int]:
    query = select(AuditEvent).where(
        AuditEvent.project_id == project_id, AuditEvent.action.in_(actions_for(category))
    )
    if from_time is not None:
        query = query.where(AuditEvent.created_at >= from_time)
    if to_time is not None:
        query = query.where(AuditEvent.created_at < to_time)
    total = int(await db.scalar(select(func.count()).select_from(query.subquery())) or 0)
    rows = (
        await db.scalars(
            query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    targets = {row.id: _target(row, ACTIONS[row.action]) for row in rows}
    wanted: dict[str, set[UUID]] = {}
    for kind, target_id in targets.values():
        if target_id is not None:
            wanted.setdefault(kind, set()).add(target_id)
    labels = await _labels(db, project_id, project_name, wanted)

    actor_ids = {row.actor_user_id for row in rows if row.actor_user_id}
    actors: dict[UUID, tuple[str, str | None]] = {}
    if actor_ids:
        result = await db.execute(
            select(User.id, User.display_name, User.avatar_storage_key).where(
                User.id.in_(actor_ids)
            )
        )
        actors = {uid: (name, key) for uid, name, key in result.all()}

    items = []
    for row in rows:
        entry = ACTIONS[row.action]
        action, summary = entry.describe(row.details or {})
        kind, target_id = targets[row.id]
        label = labels.get((kind, target_id)) if target_id else None
        # A name kept in the audit row stands in only for what that row deleted.
        snapshot = (
            next(
                (row.details[key] for key in LABEL_KEYS if isinstance(row.details.get(key), str)),
                None,
            )
            if row.action.endswith(".deleted") and row.details
            else None
        )
        items.append(
            ActivityItem(
                id=str(row.id),
                project_id=str(project_id),
                category=entry.category,
                action=action,
                actor=_actor(row.actor_user_id, actors, prefix),
                resource=ActivityResource(
                    type=kind,
                    id=str(target_id) if target_id else None,
                    display_name=label if label is not None else (snapshot or _deleted(kind)),
                    available=label is not None,
                ),
                summary=summary,
                changes=_changes(row),
                created_at=row.created_at,
            )
        )
    return items, total


def _deleted(kind: str) -> str:
    return "Deleted user" if kind == "user" else f"Deleted {kind.replace('_', ' ')}"


def _actor(
    user_id: UUID | None, actors: dict[UUID, tuple[str, str | None]], prefix: str
) -> ActivityActor:
    if user_id is None:
        return ActivityActor(user_id=None, display_name="System", avatar_url=None, type="system")
    if user_id not in actors:
        return ActivityActor(
            user_id=None, display_name="Deleted user", avatar_url=None, type="user"
        )
    name, key = actors[user_id]
    return ActivityActor(
        user_id=str(user_id),
        display_name=name,
        avatar_url=avatar_url(prefix, user_id, key),
        type="user",
    )
