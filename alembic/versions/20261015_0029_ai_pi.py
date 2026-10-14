"""AI PI: research charter, consultations and the lab notebook."""

import sqlalchemy as sa

from alembic import op

revision = "20261015_0029"
down_revision = "20261015_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("charter", sa.JSON()))
        batch.add_column(
            sa.Column("charter_status", sa.String(10), nullable=False, server_default="none")
        )
        batch.create_check_constraint(
            "ck_research_runs_charter_status", "charter_status IN ('none','pending','settled')"
        )
    op.create_table(
        "pi_consultations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "research_run_id",
            sa.Uuid(),
            sa.ForeignKey("research_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage_run_id", sa.Uuid(), sa.ForeignKey("stage_runs.id", ondelete="RESTRICT")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("gate_id", sa.String(200)),
        sa.Column("authority", sa.String(20), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON()),
        sa.Column("failure", sa.JSON()),
        sa.Column(
            "applied_decision_id",
            sa.Uuid(),
            sa.ForeignKey("stage_decisions.id", ondelete="SET NULL"),
        ),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "kind IN ('charter','gate_question','gate_review','stage_outcome','failure','close')",
            name="ck_pi_consultations_kind",
        ),
        sa.CheckConstraint(
            "authority IN ('pi','researcher')", name="ck_pi_consultations_authority"
        ),
        sa.CheckConstraint(
            "status IN ('queued','sent','decided','applied','awaiting_researcher',"
            "'endorsed','overridden','stale','failed')",
            name="ck_pi_consultations_status",
        ),
    )
    op.create_index(
        "ix_pi_consultations_run_status", "pi_consultations", ["research_run_id", "status"]
    )
    op.create_index("ix_pi_consultations_stage_kind", "pi_consultations", ["stage_run_id", "kind"])
    op.create_table(
        "pi_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "research_run_id",
            sa.Uuid(),
            sa.ForeignKey("research_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "consultation_id",
            sa.Uuid(),
            sa.ForeignKey("pi_consultations.id", ondelete="SET NULL"),
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("stage_key", sa.String(100)),
        sa.Column("actor", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('decision','lesson','risk','observation','question','directive',"
            "'override','brief')",
            name="ck_pi_notes_kind",
        ),
        sa.CheckConstraint("actor IN ('pi','user')", name="ck_pi_notes_actor"),
    )
    op.create_index("ix_pi_notes_run_created", "pi_notes", ["research_run_id", "created_at"])


def downgrade() -> None:
    op.drop_table("pi_notes")
    op.drop_table("pi_consultations")
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_constraint("ck_research_runs_charter_status", type_="check")
        batch.drop_column("charter_status")
        batch.drop_column("charter")
