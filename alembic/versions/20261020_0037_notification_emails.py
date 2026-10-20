"""Notices queued for email, sent after commit while the project's email switch is on.

Revision ID: 20261020_0037
Revises: 20261019_0036
Create Date: 2026-10-20
"""

import sqlalchemy as sa

from alembic import op

revision = "20261020_0037"
down_revision = "20261019_0036"
branch_labels = None
depends_on = None

# Frozen copy of the notice kinds at this revision; the model list may grow later.
KINDS = (
    "'run_awaiting_review', 'run_finished', 'added_to_project', "
    "'run_commented', 'project_invited', 'invite_accepted', 'invite_declined', "
    "'member_role_changed', 'removed_from_project', 'integration_failed', "
    "'budget_warning', 'budget_exceeded'"
)


def upgrade() -> None:
    op.create_table(
        "notification_emails",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "recipient_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("research_runs.id", ondelete="CASCADE")),
        sa.Column("actor_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"kind IN ({KINDS})", name="ck_notification_emails_kind"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed', 'skipped')",
            name="ck_notification_emails_status",
        ),
    )
    op.create_index(
        "ix_notification_emails_status", "notification_emails", ["status", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_notification_emails_status", table_name="notification_emails")
    op.drop_table("notification_emails")
