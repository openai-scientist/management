"""Each member's notification choices per project.

Revision ID: 20261019_0036
Revises: 20261019_0035
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "20261019_0036"
down_revision = "20261019_0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_preferences",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("digest", sa.JSON(), nullable=False),
        sa.Column("quiet_hours", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "user_id", name="uq_notification_preferences_member"),
    )


def downgrade() -> None:
    op.drop_table("notification_preferences")
