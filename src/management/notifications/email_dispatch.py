"""Sends the notice emails queued by ``notify_project_members``, after their commit.

A loop in the app's lifespan picks up pending rows in small batches. Each row is checked
again at send time, so a member removed, a group switched off or a notice that waited
too long gets no email. A row is claimed under SKIP LOCKED and committed before it is
sent, so neither several API processes nor a failure mid-batch send the same email twice.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from management.identity.models import User, UserStatus
from management.infra.email_sender import EmailSender
from management.infra.email_templates import notice_kinds, project_notice
from management.notifications.preferences import emails_kind, project_email_groups
from management.projects.models import NotificationEmail, Project, ProjectMembership
from management.research.models import ResearchRun

logger = logging.getLogger("management.notifications.email")

MAX_ATTEMPTS = 3
# A notice older than this is no longer news; it is dropped rather than sent late.
STALE_AFTER = timedelta(hours=24)
# Finished rows are kept this long for support questions, then deleted.
KEEP_FOR = timedelta(days=30)


def _aware(moment: datetime) -> datetime:
    # SQLite returns naive datetimes.
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def send_pending(
    session_factory: async_sessionmaker[AsyncSession],
    sender: EmailSender,
    *,
    app_url: str | None,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    """Send one batch of pending notice emails. Returns how many rows it picked up.

    Each row is claimed (``sending``) and committed before its network send, then settled
    in its own commit. A crash or error mid-batch therefore never sends a row twice: at
    worst a claimed row stays ``sending`` and is purged later.
    """
    now = now or datetime.now(UTC)
    async with session_factory() as db:
        await db.execute(
            delete(NotificationEmail).where(
                NotificationEmail.status != "pending",
                NotificationEmail.created_at < now - KEEP_FOR,
            )
        )
        await db.commit()
        query = (
            select(NotificationEmail.id)
            .where(NotificationEmail.status == "pending")
            .order_by(NotificationEmail.created_at)
            .limit(limit)
        )
        ids = list(await db.scalars(query))
    email_groups: dict[UUID, frozenset[str]] = {}
    for row_id in ids:
        try:
            await _send_one(session_factory, sender, row_id, app_url, now, email_groups)
        except Exception:  # noqa: BLE001
            # Never logs the row: it names a recipient.
            logger.exception("notice email could not be prepared")
            async with session_factory() as db:
                row = await db.get(NotificationEmail, row_id)
                if row is not None and row.status == "pending":
                    row.status = "skipped"
                    await db.commit()
    return len(ids)


async def _send_one(
    session_factory: async_sessionmaker[AsyncSession],
    sender: EmailSender,
    row_id: UUID,
    app_url: str | None,
    now: datetime,
    email_groups: dict[UUID, frozenset[str]],
) -> None:
    async with session_factory() as db:
        query = select(NotificationEmail).where(
            NotificationEmail.id == row_id, NotificationEmail.status == "pending"
        )
        if db.bind.dialect.name == "postgresql":
            # Another process already claiming this row skips it.
            query = query.with_for_update(skip_locked=True)
        row = await db.scalar(query)
        if row is None:
            return
        message = await _message(db, row, app_url, now, email_groups)
        row.status = "skipped" if message is None else "sending"
        row.attempts += message is not None
        attempts = row.attempts
        await db.commit()
    if message is None:
        return
    to, subject, text, html = message
    sent = await sender.send(to=to, subject=subject, text=text, html=html)
    async with session_factory() as db:
        row = await db.get(NotificationEmail, row_id)
        if row is None:
            return
        if sent:
            row.status, row.sent_at = "sent", now
        else:
            row.status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        await db.commit()


async def _message(
    db: AsyncSession,
    row: NotificationEmail,
    app_url: str | None,
    now: datetime,
    email_groups: dict[UUID, frozenset[str]],
) -> tuple[str, str, str, str] | None:
    """The email for this row, or None when it should no longer be sent."""
    if now - _aware(row.created_at) > STALE_AFTER or row.kind not in notice_kinds():
        return None
    if row.project_id not in email_groups:
        email_groups[row.project_id] = await project_email_groups(db, row.project_id)
    if not emails_kind(email_groups[row.project_id], row.kind):
        return None
    user = await db.get(User, row.recipient_user_id)
    project = await db.get(Project, row.project_id)
    member = await db.scalar(
        select(ProjectMembership.user_id).where(
            ProjectMembership.project_id == row.project_id,
            ProjectMembership.user_id == row.recipient_user_id,
            ProjectMembership.status == "active",
        )
    )
    if user is None or user.status != UserStatus.ACTIVE or project is None or member is None:
        return None
    actor = await db.get(User, row.actor_user_id) if row.actor_user_id else None
    run = await db.get(ResearchRun, row.run_id) if row.run_id else None
    subject, text, html = project_notice(
        row.kind,
        recipient_name=user.display_name or user.email,
        project_name=project.name,
        actor_name=(actor.display_name or actor.email) if actor else None,
        run_topic=run.topic if run else None,
        project_url=f"{app_url.rstrip('/')}/projects/{project.id}" if app_url else None,
    )
    return user.email, subject, text, html


async def dispatch_forever(app_state, interval: float = 10.0) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await send_pending(
                app_state.session_factory,
                app_state.email_sender,
                app_url=app_state.settings.app_url,
            )
        except Exception:  # noqa: BLE001
            # Never logs the rows: they name recipients.
            logger.exception("notice email batch failed")
