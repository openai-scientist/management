"""Stage runs, decisions, model configuration, and per-stage callback identity."""

import sqlalchemy as sa

from alembic import op

revision = "20261014_0026"
down_revision = "20261010_0025"
branch_labels = None
depends_on = None


def _id(name="id", *args, **kwargs):
    return sa.Column(name, sa.Uuid(), *args, **kwargs)


def _time(name, nullable=False):
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=None if nullable else sa.func.now(),
    )


def upgrade():
    op.create_table(
        "stage_runs",
        _id(primary_key=True),
        _id(
            "research_run_id",
            sa.ForeignKey("research_runs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("iteration", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("stage_key", sa.String(100), nullable=False),
        sa.Column("engine_key", sa.String(100), nullable=False),
        sa.Column("engine_run_id", sa.String(200)),
        _id("prior_stage_run_id", sa.ForeignKey("stage_runs.id")),
        _id("origin_decision_id"),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING"),
        sa.Column("phase", sa.String(32)),
        sa.Column("step_key", sa.String(100)),
        sa.Column("waiting_input", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("open_gate", sa.JSON()),
        sa.Column("input_refs", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("config_snapshot", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("last_source_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure", sa.JSON()),
        _time("engine_unreachable_since", True),
        _time("created_at"),
        _time("updated_at"),
        _time("finished_at", True),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','ACCEPTED',"
            "'FAILED','STOPPED','SUPERSEDED')",
            name="ck_stage_runs_status",
        ),
    )
    op.create_index(
        "ix_stage_runs_run_iteration_stage",
        "stage_runs",
        ["research_run_id", "iteration", "stage_key"],
    )
    op.create_table(
        "stage_decisions",
        _id(primary_key=True),
        _id("stage_run_id", sa.ForeignKey("stage_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("gate_id", sa.String(200)),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("actor", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("status", sa.String(20), nullable=False, server_default="applied"),
        _id("decided_by_user_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
        _time("decided_at"),
        sa.CheckConstraint(
            "action IN ('answer_gate','approve','comment','accept','send_back','rerun','stop')",
            name="ck_stage_decisions_action",
        ),
        sa.CheckConstraint("actor IN ('user','system')", name="ck_stage_decisions_actor"),
        sa.CheckConstraint("status IN ('pending','applied')", name="ck_stage_decisions_status"),
        sa.UniqueConstraint("stage_run_id", "gate_id", name="uq_stage_decisions_gate"),
    )
    op.create_foreign_key(
        "fk_stage_runs_origin_decision",
        "stage_runs",
        "stage_decisions",
        ["origin_decision_id"],
        ["id"],
    )
    op.create_table(
        "model_profiles",
        _id(primary_key=True),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(500)),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_key_encrypted", sa.Text()),
        sa.Column("extra", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("scope", sa.String(20), nullable=False, server_default="global"),
        sa.Column("input_usd_per_mtok", sa.Numeric(12, 6)),
        sa.Column("output_usd_per_mtok", sa.Numeric(12, 6)),
        _id("updated_by_user_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
        _time("updated_at"),
        sa.CheckConstraint("role IN ('stage','review')", name="ck_model_profiles_role"),
        sa.CheckConstraint("scope = 'global'", name="ck_model_profiles_scope"),
        sa.UniqueConstraint("role", "scope", name="uq_model_profiles_role_scope"),
    )
    op.create_table(
        "integration_secrets",
        sa.Column("name", sa.String(100), primary_key=True),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        _id("updated_by_user_id", sa.ForeignKey("users.id", ondelete="SET NULL")),
        _time("updated_at"),
    )
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("pipeline_key", sa.String(100)))
        batch.add_column(sa.Column("current_stage_key", sa.String(100)))
        batch.add_column(sa.Column("interaction_mode", sa.String(20)))
        batch.add_column(sa.Column("iteration", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(
            sa.Column("cost_unknown", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.drop_constraint("ck_research_runs_status", type_="check")
        batch.create_check_constraint(
            "ck_research_runs_status",
            "status IN ('queued','running','paused','awaiting_review',"
            "'completed','budget_exceeded','failed','stopped')",
        )
        batch.create_check_constraint(
            "ck_research_runs_interaction_mode",
            "interaction_mode IS NULL OR interaction_mode IN ('auto','light','copilot','full')",
        )
    op.drop_index("uq_research_runs_active_project", table_name="research_runs")
    active = sa.text(
        "status IN ('queued','running','paused','awaiting_review') "
        "OR (status = 'budget_exceeded' AND pipeline_key IS NOT NULL)"
    )
    op.create_index(
        "uq_research_runs_active_project",
        "research_runs",
        ["project_id"],
        unique=True,
        postgresql_where=active,
        sqlite_where=active,
    )
    for table in ("run_events", "run_gates", "run_artifacts"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(_id("stage_run_id"))
            batch.create_foreign_key(
                f"fk_{table}_stage_run", "stage_runs", ["stage_run_id"], ["id"]
            )
    op.drop_index("run_events_source_seq", table_name="run_events")
    op.create_index(
        "run_events_source_seq",
        "run_events",
        ["stage_run_id", "source_seq"],
        unique=True,
        postgresql_where=sa.text("source_seq IS NOT NULL"),
        sqlite_where=sa.text("source_seq IS NOT NULL"),
    )
    op.drop_index("uq_run_gates_open", table_name="run_gates")
    op.create_index(
        "uq_run_gates_open",
        "run_gates",
        ["stage_run_id"],
        unique=True,
        postgresql_where=sa.text("answer IS NULL"),
        sqlite_where=sa.text("answer IS NULL"),
    )
    with op.batch_alter_table("run_artifacts") as batch:
        batch.add_column(sa.Column("schema", sa.String(160)))
        batch.add_column(sa.Column("version", sa.Integer()))
        batch.add_column(
            sa.Column("primary", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("review_round", sa.Integer()))
        batch.add_column(sa.Column("metadata", sa.JSON(), nullable=False, server_default="{}"))
        batch.drop_constraint("uq_run_artifacts_filename", type_="unique")
        batch.create_unique_constraint(
            "uq_run_artifacts_stage_schema_version", ["stage_run_id", "schema", "version"]
        )


def downgrade():
    with op.batch_alter_table("run_artifacts") as batch:
        batch.drop_constraint("uq_run_artifacts_stage_schema_version", type_="unique")
        batch.create_unique_constraint("uq_run_artifacts_filename", ["run_id", "filename"])
        for column in ("metadata", "review_round", "primary", "version", "schema"):
            batch.drop_column(column)
    op.drop_index("uq_run_gates_open", table_name="run_gates")
    op.create_index(
        "uq_run_gates_open",
        "run_gates",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("answer IS NULL"),
    )
    op.drop_index("run_events_source_seq", table_name="run_events")
    op.create_index(
        "run_events_source_seq",
        "run_events",
        ["run_id", "source_seq"],
        unique=True,
        postgresql_where=sa.text("source_seq IS NOT NULL"),
    )
    for table in ("run_artifacts", "run_gates", "run_events"):
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_stage_run", type_="foreignkey")
            batch.drop_column("stage_run_id")
    op.drop_index("uq_research_runs_active_project", table_name="research_runs")
    active = sa.text("status IN ('queued','running','paused','awaiting_review')")
    op.create_index(
        "uq_research_runs_active_project",
        "research_runs",
        ["project_id"],
        unique=True,
        postgresql_where=active,
        sqlite_where=active,
    )
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_constraint("ck_research_runs_interaction_mode", type_="check")
        batch.drop_constraint("ck_research_runs_status", type_="check")
        batch.create_check_constraint(
            "ck_research_runs_status",
            "status IN ('queued','running','paused','awaiting_review',"
            "'completed','budget_exceeded','failed')",
        )
        for column in (
            "cost_unknown",
            "iteration",
            "interaction_mode",
            "current_stage_key",
            "pipeline_key",
        ):
            batch.drop_column(column)
    op.drop_table("integration_secrets")
    op.drop_table("model_profiles")
    op.drop_constraint("fk_stage_runs_origin_decision", "stage_runs", type_="foreignkey")
    op.drop_table("stage_decisions")
    op.drop_index("ix_stage_runs_run_iteration_stage", table_name="stage_runs")
    op.drop_table("stage_runs")
