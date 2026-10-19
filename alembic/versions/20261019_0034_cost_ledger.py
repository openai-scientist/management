"""Ledger of model spend by project and month, and when it began.

Revision ID: 20261019_0034
Revises: 20261019_0033
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "20261019_0034"
down_revision = "20261019_0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("research_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20)),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(200)),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("cost_usd", sa.Numeric(14, 6)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source IN ('stage','consultation','popper')", name="ck_cost_entries_source"
        ),
        sa.UniqueConstraint("source_key", name="uq_cost_entries_source_key"),
    )
    op.create_index(
        "ix_cost_entries_project_occurred", "cost_entries", ["project_id", "occurred_at"]
    )
    op.create_index("ix_cost_entries_source_id", "cost_entries", ["source_id"])
    op.create_table(
        "cost_ledger_start",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Spend from here on is recorded; earlier months report that usage is not available.
    op.execute("INSERT INTO cost_ledger_start (id, started_at) VALUES (1, CURRENT_TIMESTAMP)")


def downgrade() -> None:
    op.drop_table("cost_ledger_start")
    op.drop_index("ix_cost_entries_source_id", table_name="cost_entries")
    op.drop_index("ix_cost_entries_project_occurred", table_name="cost_entries")
    op.drop_table("cost_entries")
