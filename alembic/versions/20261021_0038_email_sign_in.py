"""Sign-in by email: a link beside the code, and whether the user has chosen a name.

Revision ID: 20261021_0038
Revises: 20261020_0037
Create Date: 2026-10-21
"""

import sqlalchemy as sa

from alembic import op

revision = "20261021_0038"
down_revision = "20261020_0037"
branch_labels = None
depends_on = None


def _purposes(values: str) -> None:
    with op.batch_alter_table("email_otps") as batch:
        batch.drop_constraint("ck_email_otps_purpose", type_="check")
        batch.create_check_constraint("ck_email_otps_purpose", f"purpose IN ({values})")


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True))
    # Everyone who could sign in before already has a name.
    op.execute("UPDATE users SET onboarded_at = created_at WHERE email_verified_at IS NOT NULL")
    with op.batch_alter_table("email_otps") as batch:
        batch.add_column(sa.Column("link_token_digest", sa.String(64), nullable=True))
        batch.create_unique_constraint("uq_email_otps_link_token_digest", ["link_token_digest"])
    _purposes("'verify_email', 'reset_password', 'sign_in'")


def downgrade() -> None:
    op.execute("DELETE FROM email_otps WHERE purpose = 'sign_in'")
    _purposes("'verify_email', 'reset_password'")
    with op.batch_alter_table("email_otps") as batch:
        batch.drop_constraint("uq_email_otps_link_token_digest", type_="unique")
        batch.drop_column("link_token_digest")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("onboarded_at")
