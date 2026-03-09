"""add bad address fields to package

Revision ID: 73799bd1d0d0
Revises: 3a830906b06a
Create Date: 2026-03-08 20:13:45.452023
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '73799bd1d0d0'
down_revision = '3a830906b06a'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'bad_address',
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('false')
            )
        )
        batch_op.add_column(
            sa.Column(
                'bad_address_fee',
                sa.Numeric(precision=10, scale=2),
                nullable=False,
                server_default=sa.text('0')
            )
        )

    # optional: remove defaults after existing rows are populated
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.alter_column('bad_address', server_default=None)
        batch_op.alter_column('bad_address_fee', server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_column('bad_address_fee')
        batch_op.drop_column('bad_address')