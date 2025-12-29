"""add epc flag to packages

Revision ID: e6bb196ca2da
Revises: 5c56d887ff76
Create Date: 2025-11-24 20:20:58.232369

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e6bb196ca2da'
down_revision = '5c56d887ff76'
branch_labels = None
depends_on = None


def upgrade():
    # 1) add column with server_default so existing rows get 0
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'epc',
                sa.Integer(),
                nullable=False,
                server_default='0'   # for existing rows
            )
        )

    # 2) optional: drop the default at the DB level (model still has default=0)
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.alter_column('epc', server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_column('epc')