"""PI v3: stage_review consultations, critique notes, the critic model role, run obligations."""

import sqlalchemy as sa

from alembic import op

revision = "20261015_0030"
down_revision = "20261015_0029"
branch_labels = None
depends_on = None

KINDS = "'charter','gate_question','gate_review','stage_outcome','failure','close'"
NOTE_KINDS = "'decision','lesson','risk','observation','question','directive','override','brief'"


def _swap(table, name, new):
    with op.batch_alter_table(table) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, new)


def upgrade() -> None:
    with op.batch_alter_table("research_runs") as batch:
        batch.add_column(sa.Column("obligations", sa.JSON(), nullable=False, server_default="[]"))
    _swap("pi_consultations", "ck_pi_consultations_kind", f"kind IN ({KINDS},'stage_review')")
    _swap("pi_notes", "ck_pi_notes_kind", f"kind IN ({NOTE_KINDS},'critique')")
    _swap("model_profiles", "ck_model_profiles_role", "role IN ('stage','review','critic')")


def downgrade() -> None:
    op.execute("DELETE FROM pi_notes WHERE kind = 'critique'")
    op.execute(
        "UPDATE pi_notes SET consultation_id = NULL WHERE consultation_id IN "
        "(SELECT id FROM pi_consultations WHERE kind = 'stage_review')"
    )
    op.execute("DELETE FROM pi_consultations WHERE kind = 'stage_review'")
    op.execute("DELETE FROM model_profiles WHERE role = 'critic'")
    _swap("pi_consultations", "ck_pi_consultations_kind", f"kind IN ({KINDS})")
    _swap("pi_notes", "ck_pi_notes_kind", f"kind IN ({NOTE_KINDS})")
    _swap("model_profiles", "ck_model_profiles_role", "role IN ('stage','review')")
    with op.batch_alter_table("research_runs") as batch:
        batch.drop_column("obligations")
