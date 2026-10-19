"""Turn changes to runs, comments and memberships into deliveries for the project's channels.

A listener on every session's flush sees each change wherever the code makes it: a run's
status is set in several places (the API, the orchestrator, Popper callbacks), and a new
one cannot forget to post. The deliveries join the flush that carries the change, so a
rollback leaves nothing to send.

A delivery holds only ids. The worker builds the message when it sends, so no user text
waits in the outbox.
"""

import logging
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import event, inspect, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from management.integrations.catalog import INTEGRATION_FINISHED_STATUSES
from management.integrations.models import IntegrationDelivery, ProjectIntegration
from management.projects.models import Comment, ProjectMembership
from management.research.models import ResearchRun

logger = logging.getLogger(__name__)

_NAMESPACE = uuid5(NAMESPACE_URL, "research-platform/integrations")

Event = tuple[UUID, str, UUID, dict[str, Any]]


def _changed_status(target: Any) -> str | None:
    """The status before this flush, when the flush changes it to a different value."""
    history = inspect(target).attrs.status.history
    if not history.has_changes() or not history.deleted:
        return None
    return history.deleted[0]


def _run_event(session: Session, run: ResearchRun) -> Event | None:
    before = _changed_status(run)
    if before is None:
        return None
    if run.status == "awaiting_review":
        key = "run.awaiting_review"
    elif run.status in INTEGRATION_FINISHED_STATUSES:
        key = "run.finished"
    else:
        return None
    # Each transition is its own message; a run waits for review once per stage.
    return run.project_id, key, uuid4(), {"run_id": str(run.id), "status": run.status}


def _comment_event(session: Session, comment: Comment) -> Event | None:
    # The run may be in this same flush, not yet in the database.
    pending = next(
        (obj for obj in session.new if isinstance(obj, ResearchRun) and obj.id == comment.run_id),
        None,
    )
    project_id = (
        pending.project_id
        if pending is not None
        else session.scalar(select(ResearchRun.project_id).where(ResearchRun.id == comment.run_id))
    )
    if project_id is None:
        return None
    # One message per discussion, ever: the first comment on a run, or on one of its results.
    # Later comments, and a comment deleted and written again, share this id.
    target = comment.artifact_id or "run"
    return (
        project_id,
        "run.commented",
        uuid5(_NAMESPACE, f"{comment.run_id}:{target}:commented"),
        {
            "run_id": str(comment.run_id),
            "comment_id": str(comment.id),
            "artifact_id": str(comment.artifact_id) if comment.artifact_id else None,
        },
    )


def _member_event(membership: ProjectMembership) -> Event | None:
    if membership.status != "active" or _changed_status(membership) != "invited":
        return None
    return (
        membership.project_id,
        "member.joined",
        uuid5(_NAMESPACE, f"{membership.id}:joined"),
        {"membership_id": str(membership.id), "user_id": str(membership.user_id)},
    )


def _events(session: Session) -> list[Event]:
    found: list[Event | None] = []
    for target in session.dirty:
        if isinstance(target, ResearchRun):
            found.append(_run_event(session, target))
        elif isinstance(target, ProjectMembership):
            found.append(_member_event(target))
    for target in session.new:
        if isinstance(target, Comment):
            if target.id is None:
                target.id = uuid4()
            found.append(_comment_event(session, target))
    return [item for item in found if item is not None]


def enqueue(session: Session, events: list[Event]) -> None:
    """Add a delivery per active integration that posts the event; repeats are dropped.

    Runs in a savepoint: an integration deleted at the same moment, or tables not migrated
    yet, lose the message but never fail the change that caused it.
    """
    connection = session.connection()
    if connection.dialect.name != "postgresql":
        # pysqlite commits a savepoint opened outside its own transaction; tests run there.
        _insert(session, events)
        return
    try:
        with connection.begin_nested():
            _insert(session, events)
    except DBAPIError:
        logger.warning("Integration deliveries not queued", exc_info=False)


def _insert(session: Session, events: list[Event]) -> None:
    projects = {project_id for project_id, *_ in events}
    integrations = session.scalars(
        select(ProjectIntegration).where(
            ProjectIntegration.project_id.in_(projects), ProjectIntegration.status == "active"
        )
    ).all()
    rows = [
        {
            "integration_id": integration.id,
            "event_id": event_id,
            "event_key": key,
            "ref": ref,
            "config_version": integration.config_version,
        }
        for project_id, key, event_id, ref in events
        for integration in integrations
        if integration.project_id == project_id and key in integration.events
    ]
    if not rows:
        return
    insert = postgres_insert if session.connection().dialect.name == "postgresql" else sqlite_insert
    statement = insert(IntegrationDelivery).on_conflict_do_nothing(
        index_elements=["integration_id", "event_id"]
    )
    session.execute(statement, rows)


@event.listens_for(Session, "before_flush")
def _post_changes(session: Session, _context: Any, _instances: Any) -> None:
    events = _events(session)
    if events:
        enqueue(session, events)
