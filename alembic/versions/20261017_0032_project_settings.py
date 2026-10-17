"""Project settings, stored as one JSON object per settings section.

Revision ID: 20261017_0032
Revises: 20261016_0031
Create Date: 2026-10-17
"""

import sqlalchemy as sa

from alembic import op

revision = "20261017_0032"
down_revision = "20261016_0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_settings",
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("updated_by_user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("project_settings")
