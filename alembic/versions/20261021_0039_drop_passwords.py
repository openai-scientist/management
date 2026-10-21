"""Sign-in is by emailed code or Google only: passwords and their codes go.

One way in practice: the downgrade brings the columns back empty, so nobody has a
password afterwards. Back up the database before upgrading.

Revision ID: 20261021_0039
Revises: 20261021_0038
Create Date: 2026-10-21
"""

import sqlalchemy as sa

from alembic import op

revision = "20261021_0039"
down_revision = "20261021_0038"
branch_labels = None
depends_on = None


def _purposes(values: str) -> None:
    with op.batch_alter_table("email_otps") as batch:
        batch.drop_constraint("ck_email_otps_purpose", type_="check")
        batch.create_check_constraint("ck_email_otps_purpose", f"purpose IN ({values})")


def upgrade() -> None:
    # A session started with a temporary password, or on an inbox nobody proved, ends here:
    # without the password fence it would have full access.
    op.execute(
        "UPDATE auth_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE revoked_at IS NULL AND user_id IN ("
        "SELECT id FROM users WHERE must_change_password OR email_verified_at IS NULL)"
    )
    op.execute("DELETE FROM email_otps WHERE purpose IN ('verify_email', 'reset_password')")
    _purposes("'sign_in'")
    with op.batch_alter_table("email_otps") as batch:
        batch.drop_column("reset_token_digest")
        batch.drop_column("reset_token_expires_at")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("password_hash")
        batch.drop_column("must_change_password")


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("password_hash", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column(
                "must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )
    with op.batch_alter_table("email_otps") as batch:
        batch.add_column(sa.Column("reset_token_digest", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("reset_token_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
    _purposes("'verify_email', 'reset_password', 'sign_in'")
