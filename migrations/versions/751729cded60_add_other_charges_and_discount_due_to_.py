"""add other_charges and discount_due to packages

Revision ID: 751729cded60
Revises: 31026b82d5d1
Create Date: 2026-02-12 14:07:52.750233
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '751729cded60'
down_revision = '31026b82d5d1'
branch_labels = None
depends_on = None


def upgrade():
    # Add with server_default so existing rows get 0 instead of NULL
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('other_charges', sa.Float(), nullable=False, server_default='0'))
        batch_op.add_column(sa.Column('discount_due', sa.Float(), nullable=False, server_default='0'))

    # Remove defaults after backfill
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.alter_column('other_charges', nullable=False, server_default=None)
        batch_op.alter_column('discount_due', nullable=False, server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_column('discount_due')
        batch_op.drop_column('other_charges')
