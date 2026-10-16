"""Let the AI PI be the actor of a stage decision."""

from alembic import op

revision = "20261015_0028"
down_revision = "20261014_0027"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("stage_decisions") as batch:
        batch.drop_constraint("ck_stage_decisions_actor", type_="check")
        batch.create_check_constraint("ck_stage_decisions_actor", "actor IN ('user','system','pi')")


def downgrade():
    with op.batch_alter_table("stage_decisions") as batch:
        batch.drop_constraint("ck_stage_decisions_actor", type_="check")
        batch.create_check_constraint("ck_stage_decisions_actor", "actor IN ('user','system')")
