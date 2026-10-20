"""Google connections to a whole account, kept for its owner to pick files with.

Revision ID: 20261016_0031
Revises: 20261015_0030
Create Date: 2026-10-16
"""

from alembic import op

revision = "20261016_0031"
down_revision = "20261015_0030"
branch_labels = None
depends_on = None

KIND_CHECK = "ck_data_connections_kind"
KINDS = "'postgres', 'mysql', 'bigquery', 'google_sheets', 'google_drive', 'prometheus', 'influxdb'"


def upgrade() -> None:
    with op.batch_alter_table("data_connections") as batch:
        batch.drop_constraint(KIND_CHECK, type_="check")
        batch.create_check_constraint(KIND_CHECK, f"kind IN ({KINDS}, 'google')")


def downgrade() -> None:
    # Fails while a Google account connection exists: delete those first.
    with op.batch_alter_table("data_connections") as batch:
        batch.drop_constraint(KIND_CHECK, type_="check")
        batch.create_check_constraint(KIND_CHECK, f"kind IN ({KINDS})")
