"""add subscription tracking to packages

Revision ID: aa28aa38304c
Revises: 1da552eb37cf
Create Date: 2026-04-29 20:16:01.047978
"""
from alembic import op
import sqlalchemy as sa


revision = 'aa28aa38304c'
down_revision = '1da552eb37cf'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'subscription_applied',
                sa.Boolean(),
                server_default=sa.text('false'),
                nullable=False
            )
        )
        batch_op.add_column(sa.Column('subscription_applied_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('subscription_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('subscription_result', sa.String(length=40), nullable=True))
        batch_op.create_index(batch_op.f('ix_packages_subscription_id'), ['subscription_id'], unique=False)
        batch_op.create_foreign_key(
            'fk_packages_subscription_id_subscriptions',
            'subscriptions',
            ['subscription_id'],
            ['id']
        )

    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.alter_column('subscription_applied', server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_constraint('fk_packages_subscription_id_subscriptions', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_packages_subscription_id'))
        batch_op.drop_column('subscription_result')
        batch_op.drop_column('subscription_id')
        batch_op.drop_column('subscription_applied_at')
        batch_op.drop_column('subscription_applied')