"""Add subject to notifications

Revision ID: f0541b2b1e7c
Revises: dbf339644ffa
Create Date: 2026-01-08 11:52:19.788087
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f0541b2b1e7c'
down_revision = 'dbf339644ffa'
branch_labels = None
depends_on = None


def upgrade():
    # 1) Add subject safely with a server_default so existing rows get a value
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('subject', sa.String(length=120), nullable=False, server_default='Notification')
        )

    # 2) Optional cleanup: remove default after backfill (keeps schema clean)
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.alter_column('subject', server_default=None)


def downgrade():
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('subject')
