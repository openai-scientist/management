"""Budget alerts sent per project and month, and their notification kinds.

Revision ID: 20261019_0035
Revises: 20261019_0034
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "20261019_0035"
down_revision = "20261019_0034"
branch_labels = None
depends_on = None

KINDS = (
    "'run_awaiting_review', 'run_finished', 'added_to_project', 'run_commented', "
    "'project_invited', 'invite_accepted', 'invite_declined', "
    "'member_role_changed', 'removed_from_project', 'integration_failed'"
)


def upgrade() -> None:
    op.create_table(
        "spending_alerts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('budget_warning', 'budget_exceeded')", name="ck_spending_alerts_kind"
        ),
        sa.UniqueConstraint("project_id", "period", "kind", name="uq_spending_alerts_period_kind"),
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind",
        "notifications",
        f"kind IN ({KINDS}, 'budget_warning', 'budget_exceeded')",
    )


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE kind IN ('budget_warning', 'budget_exceeded')")
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint("ck_notifications_kind", "notifications", f"kind IN ({KINDS})")
    op.drop_table("spending_alerts")
