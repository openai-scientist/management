"""Chat integrations of a project and the outbox of their deliveries.

Revision ID: 20261019_0033
Revises: 20261017_0032
Create Date: 2026-10-19
"""

import sqlalchemy as sa

from alembic import op

revision = "20261019_0033"
down_revision = "20261017_0032"
branch_labels = None
depends_on = None

KINDS = (
    "'run_awaiting_review', 'run_finished', 'added_to_project', 'run_commented', "
    "'project_invited', 'invite_accepted', 'invite_declined', "
    "'member_role_changed', 'removed_from_project'"
)


def upgrade() -> None:
    op.create_table(
        "project_integrations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),
        sa.Column("url_hint", sa.String(300), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("last_delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(40)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_by_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('slack', 'teams', 'discord')", name="ck_integrations_provider"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'error')", name="ck_integrations_status"
        ),
    )
    op.create_index(
        "ix_project_integrations_project_created",
        "project_integrations",
        ["project_id", "created_at"],
    )
    op.create_index(
        "uq_project_integrations_project_name",
        "project_integrations",
        ["project_id", sa.text("lower(name)")],
        unique=True,
    )
    op.create_table(
        "integration_deliveries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "integration_id",
            sa.Uuid(),
            sa.ForeignKey("project_integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_key", sa.String(40), nullable=False),
        sa.Column("ref", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("claims", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("response_code", sa.Integer()),
        sa.Column("error_code", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'failed')", name="ck_deliveries_status"
        ),
        sa.UniqueConstraint("integration_id", "event_id", name="uq_deliveries_integration_event"),
    )
    op.create_index(
        "ix_integration_deliveries_due", "integration_deliveries", ["status", "next_attempt_at"]
    )
    op.create_index(
        "ix_integration_deliveries_integration_created",
        "integration_deliveries",
        ["integration_id", "created_at"],
    )
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint(
        "ck_notifications_kind", "notifications", f"kind IN ({KINDS}, 'integration_failed')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM notifications WHERE kind = 'integration_failed'")
    op.drop_constraint("ck_notifications_kind", "notifications", type_="check")
    op.create_check_constraint("ck_notifications_kind", "notifications", f"kind IN ({KINDS})")
    op.drop_table("integration_deliveries")
    op.drop_table("project_integrations")
