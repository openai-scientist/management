"""Stage-aware event timeline and legacy step backfill."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_0027"
down_revision = "20261014_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_events", sa.Column("step_key", sa.String(100)))
    op.add_column(
        "run_events", sa.Column("iteration", sa.Integer(), nullable=False, server_default="1")
    )
    op.add_column("run_events", sa.Column("phase", sa.String(32)))
    op.add_column(
        "run_events", sa.Column("class", sa.String(20), nullable=False, server_default="lifecycle")
    )
    op.add_column("run_events", sa.Column("summary", sa.Text()))
    op.add_column("run_events", sa.Column("refs", sa.JSON(), nullable=False, server_default="[]"))
    op.execute(
        "UPDATE run_events SET step_key = stage_key, stage_key = NULL WHERE stage_run_id IS NULL"
    )
    op.execute(
        "UPDATE run_events SET iteration = "
        "(SELECT iteration FROM stage_runs WHERE stage_runs.id = run_events.stage_run_id) "
        "WHERE stage_run_id IS NOT NULL"
    )
    op.execute(
        "UPDATE run_events SET class = 'platform' "
        "WHERE stage_run_id IS NOT NULL AND source_seq IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE run_events SET stage_key = COALESCE(step_key, stage_key) WHERE stage_run_id IS NULL"
    )
    for column in ("refs", "summary", "class", "phase", "iteration", "step_key"):
        op.drop_column("run_events", column)
