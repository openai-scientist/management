"""Build a delivery's message from the records it points at, when it is sent.

Only metadata goes out: names, a run's topic and outcome, a short comment excerpt. Never
research data, files, results or email addresses.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from management.identity.models import User
from management.integrations.messages import Message, Tone
from management.integrations.models import IntegrationDelivery
from management.projects.models import Comment, Project, ProjectMembership
from management.research.models import ResearchRun

EXCERPT_LENGTH = 200
_OUTCOMES: dict[str, tuple[str, Tone]] = {
    "completed": ("Run completed", "success"),
    "failed": ("Run failed", "danger"),
    "stopped": ("Run stopped", "neutral"),
    "budget_exceeded": ("Run out of budget", "warning"),
}
_ROLES = {"project_manager": "Project Manager", "researcher": "Researcher", "reviewer": "Reviewer"}


def run_name(run: ResearchRun) -> str:
    topic = (run.topic or "").strip()
    return topic or f"Run {str(run.id)[:8]}"


def _duration(run: ResearchRun) -> str | None:
    start: datetime | None = run.started_at or run.created_at
    if run.finished_at is None or start is None:
        return None
    minutes = max(0, int((run.finished_at - start).total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


async def _person(db: AsyncSession, user_id: UUID | None) -> str:
    user = await db.get(User, user_id) if user_id else None
    return (user.display_name or "").strip() if user and user.display_name else "A member"


def _excerpt(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= EXCERPT_LENGTH else flat[: EXCERPT_LENGTH - 1] + "…"


async def build_message(
    db: AsyncSession, delivery: IntegrationDelivery, project: Project, app_url: str | None
) -> Message | None:
    """The message for a delivery, or None when what it points at is gone."""
    ref = delivery.ref
    base = f"{app_url.rstrip('/')}/projects/{project.id}" if app_url else None
    if delivery.event_key == "member.joined":
        membership = await db.get(ProjectMembership, UUID(ref["membership_id"]))
        if membership is None:
            return None
        role = _ROLES.get(membership.role_code, membership.role_code)
        return Message(
            "New member",
            f"{await _person(db, membership.user_id)} joined the project",
            "info",
            (("Project", project.name), ("Role", role)),
            link_label="Open project",
            link_url=base,
        )

    run = await db.get(ResearchRun, UUID(ref["run_id"]))
    if run is None:
        return None
    link = f"{base}/experiments/runs/{run.id}" if base else None
    name = run_name(run)
    if delivery.event_key == "run.awaiting_review":
        return Message(
            "Review needed",
            name,
            "warning",
            (("Project", project.name), ("Started by", await _person(db, run.created_by_user_id))),
            link_label="Open review",
            link_url=link,
        )
    if delivery.event_key == "run.finished":
        title, tone = _OUTCOMES.get(ref.get("status", run.status), ("Run finished", "neutral"))
        facts = [("Project", project.name)]
        duration = _duration(run)
        if duration:
            facts.append(("Duration", duration))
        return Message(title, name, tone, tuple(facts), link_label="Open run", link_url=link)
    # run.commented: a deleted comment still announces the discussion, without its words.
    comment = await db.get(Comment, UUID(ref["comment_id"]))
    author = await _person(db, comment.author_user_id if comment else None)
    return Message(
        "New comment",
        f"{author} commented on {name}",
        "info",
        (("Project", project.name),),
        quote=_excerpt(comment.body) if comment is not None else None,
        link_label="Open discussion",
        link_url=link,
    )
