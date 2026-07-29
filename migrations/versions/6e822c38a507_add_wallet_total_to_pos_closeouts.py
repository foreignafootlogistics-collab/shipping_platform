"""add wallet total to pos closeouts

Revision ID: 6e822c38a507
Revises: c2351a947dad
Create Date: 2026-07-29 12:30:08.559611
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers used by Alembic.
revision = "6e822c38a507"
down_revision = "c2351a947dad"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "pos_closeouts",
        schema=None,
    ) as batch_op:
        # The temporary server default fills existing records
        # with JMD 0.00.
        batch_op.add_column(
            sa.Column(
                "expected_wallet",
                sa.Numeric(
                    precision=12,
                    scale=2,
                ),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

        # Remove the database default afterward.
        # New values will be provided by the application.
        batch_op.alter_column(
            "expected_wallet",
            server_default=None,
        )


def downgrade():
    with op.batch_alter_table(
        "pos_closeouts",
        schema=None,
    ) as batch_op:
        batch_op.drop_column(
            "expected_wallet"
        )