"""add unknown package charges to expected collections

Revision ID: 43d9be30286c
Revises: ea4d5a84081c
Create Date: 2026-08-05 20:24:09.249275

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "43d9be30286c"
down_revision = "ea4d5a84081c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "expected_package_collections",
        schema=None,
    ) as batch_op:

        batch_op.add_column(
            sa.Column(
                "unknown_package_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )

        batch_op.add_column(
            sa.Column(
                "unknown_package_fee_usd",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default=sa.text("5.00"),
            )
        )

        batch_op.add_column(
            sa.Column(
                "unknown_charge_total_usd",
                sa.Numeric(precision=12, scale=2),
                nullable=False,
                server_default=sa.text("0.00"),
            )
        )


def downgrade():
    with op.batch_alter_table(
        "expected_package_collections",
        schema=None,
    ) as batch_op:

        batch_op.drop_column(
            "unknown_charge_total_usd"
        )

        batch_op.drop_column(
            "unknown_package_fee_usd"
        )

        batch_op.drop_column(
            "unknown_package_count"
        )