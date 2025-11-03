"""Recreate bills and payments tables

Revision ID: 3417c96448c5
Revises: 16296b50d575
Create Date: 2025-08-24 00:15:28.209036

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '3417c96448c5'
down_revision = '16296b50d575'
branch_labels = None
depends_on = None


def upgrade():
    # Recreate bills table
    op.create_table(
        'bills',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('package_id', sa.Integer, sa.ForeignKey('packages.id'), nullable=False),
        sa.Column('description', sa.Text, nullable=True),
        sa.Column('amount', sa.Float, nullable=False),
        sa.Column('status', sa.String, server_default='unpaid', nullable=True),
        sa.Column('due_date', sa.String, nullable=True),
        sa.Column('created_at', sa.DateTime, server_default=sa.func.current_timestamp(), nullable=True),
    )

    # Recreate payments table
    op.create_table(
        'payments',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('users.id'), nullable=False),
        sa.Column('bill_number', sa.String, nullable=False),
        sa.Column('payment_date', sa.String, nullable=False),
        sa.Column('payment_type', sa.String, nullable=False),
        sa.Column('amount', sa.Float, nullable=False),
        sa.Column('authorized_by', sa.String, nullable=False),
        sa.Column('invoice_path', sa.String, nullable=True),
    )

def downgrade():
    op.drop_table('payments')
    op.drop_table('bills')