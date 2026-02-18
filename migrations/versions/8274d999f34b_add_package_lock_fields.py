"""Add package lock fields

Revision ID: 8274d999f34b
Revises: 67b33e4f7192
Create Date: 2026-02-18 00:40:12.989050
"""
from alembic import op
import sqlalchemy as sa

revision = '8274d999f34b'
down_revision = '67b33e4f7192'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        # ✅ add NOT NULL with a server default so existing rows get FALSE
        batch_op.add_column(sa.Column(
            'is_locked',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false')
        ))
        batch_op.add_column(sa.Column('locked_reason', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True))

        # ✅ optional: remove the server default after the column exists
        batch_op.alter_column('is_locked', server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_column('locked_at')
        batch_op.drop_column('locked_reason')
        batch_op.drop_column('is_locked')

