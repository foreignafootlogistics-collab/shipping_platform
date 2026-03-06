"""add invoice emailed timestamp

Revision ID: f30ee4ebe1d6
Revises: 5fc46428a95e
Create Date: 2026-03-06 16:09:41.435403

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f30ee4ebe1d6'
down_revision = '5fc46428a95e'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('invoice_emailed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column(
                'invoice_email_failed',
                sa.Boolean(),
                nullable=False,
                server_default=sa.text('false')
            )
        )
        batch_op.add_column(sa.Column('invoice_email_failed_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('invoice_email_failure_reason', sa.Text(), nullable=True))

    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.alter_column('invoice_email_failed', server_default=None)


def downgrade():
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.drop_column('invoice_email_failure_reason')
        batch_op.drop_column('invoice_email_failed_at')
        batch_op.drop_column('invoice_email_failed')
        batch_op.drop_column('invoice_emailed_at')