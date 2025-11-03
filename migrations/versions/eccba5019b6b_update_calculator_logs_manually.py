"""Update calculator_logs safely

Revision ID: eccba5019b6b
Revises: 7807ed68b019
Create Date: 2025-08-21 17:46:01.428117
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'eccba5019b6b'
down_revision = '7807ed68b019'
branch_labels = None
depends_on = None


def upgrade():
    # Get existing columns
    existing_columns = [
        c[1] for c in op.get_bind().execute(
            sa.text("PRAGMA table_info(calculator_logs);")
        ).fetchall()
    ]

    with op.batch_alter_table('calculator_logs') as batch_op:
        # Rename 'icd_amount' → 'duty_amount'
        if 'duty_amount' not in existing_columns and 'icd_amount' in existing_columns:
            batch_op.alter_column('icd_amount', new_column_name='duty_amount')

        # Rename 'env' → 'envl_amount'
        if 'envl_amount' not in existing_columns and 'env' in existing_columns:
            batch_op.alter_column('env', new_column_name='envl_amount')

        # Add missing columns safely
        if 'category' not in existing_columns:
            batch_op.add_column(sa.Column('category', sa.String(length=100), nullable=True))
        if 'weight' not in existing_columns:
            batch_op.add_column(sa.Column('weight', sa.Float, nullable=True))
        if 'gct_amount' not in existing_columns:
            batch_op.add_column(sa.Column('gct_amount', sa.Float, nullable=True))
        if 'scf_amount' not in existing_columns:
            batch_op.add_column(sa.Column('scf_amount', sa.Float, nullable=True))
        if 'caf_amount' not in existing_columns:
            batch_op.add_column(sa.Column('caf_amount', sa.Float, nullable=True))


def downgrade():
    existing_columns = [
        c[1] for c in op.get_bind().execute(
            sa.text("PRAGMA table_info(calculator_logs);")
        ).fetchall()
    ]

    with op.batch_alter_table('calculator_logs') as batch_op:
        # Rename 'duty_amount' back → 'icd_amount'
        if 'duty_amount' in existing_columns and 'icd_amount' not in existing_columns:
            batch_op.alter_column('duty_amount', new_column_name='icd_amount')

        # Rename 'envl_amount' back → 'env'
        if 'envl_amount' in existing_columns and 'env' not in existing_columns:
            batch_op.alter_column('envl_amount', new_column_name='env')

        # Drop extra columns
        for col in ['category', 'weight', 'gct_amount', 'scf_amount', 'caf_amount']:
            if col in existing_columns:
                batch_op.drop_column(col)
