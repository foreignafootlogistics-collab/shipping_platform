"""add invoices.amount and users.is_superadmin

Revision ID: c6502b1a9236
Revises: ecbf8e6545e6
Create Date: 2025-11-22 10:13:08.132820

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c6502b1a9236'
down_revision = 'ecbf8e6545e6'
branch_labels = None
depends_on = None


def upgrade():
    # --- INVOICES: add amount fields, timestamps, etc. ---
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('amount', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('amount_due', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('grand_total', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('date_issued', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('created_at', sa.DateTime(), nullable=True))
        batch_op.drop_column('total')

    # --- PACKAGES: safely cast text dates to TIMESTAMP and adjust other cols ---
    with op.batch_alter_table('packages', schema=None) as batch_op:
        # Cast from TEXT/VARCHAR to TIMESTAMP, safely treating empty string as NULL
        batch_op.alter_column(
            'received_date',
            existing_type=sa.VARCHAR(),
            type_=sa.DateTime(),
            existing_nullable=True,
            postgresql_using="NULLIF(received_date, '')::timestamp without time zone",
        )
        batch_op.alter_column(
            'date_received',
            existing_type=sa.VARCHAR(),
            type_=sa.DateTime(),
            existing_nullable=True,
            postgresql_using="NULLIF(date_received, '')::timestamp without time zone",
        )
        batch_op.alter_column(
            'created_at',
            existing_type=sa.VARCHAR(),
            type_=sa.DateTime(),
            existing_nullable=True,
            postgresql_using="NULLIF(created_at, '')::timestamp without time zone",
        )

        # weight: numeric → float
        batch_op.alter_column(
            'weight',
            existing_type=sa.NUMERIC(precision=10, scale=2),
            type_=sa.Float(),
            existing_nullable=True,
        )

        # remove obsolete columns that are no longer in the model
        batch_op.drop_column('house_number')
        batch_op.drop_column('customer_name')
        batch_op.drop_column('customer_id')
        batch_op.drop_column('manifest_date')

    # --- PREALERTS: cast purchase_date text → DATE ---
    with op.batch_alter_table('prealerts', schema=None) as batch_op:
        batch_op.alter_column(
            'purchase_date',
            existing_type=sa.VARCHAR(),
            type_=sa.Date(),
            existing_nullable=True,
            postgresql_using="NULLIF(purchase_date, '')::date",
        )

    # --- USERS: add is_superadmin if missing + unique referral_code ---
    # Use raw SQL with IF NOT EXISTS so we don't crash if the column is already there
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_superadmin BOOLEAN DEFAULT false")

    with op.batch_alter_table('users', schema=None) as batch_op:
        # Unique constraint on referral_code
        batch_op.create_unique_constraint(None, ['referral_code'])

    # ### end Alembic commands ###


def downgrade():
    # --- USERS: drop unique + is_superadmin ---
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_constraint(None, type_='unique')

    # Drop the column only if it exists (safe for Postgres)
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_superadmin")

    # --- PREALERTS: revert purchase_date back to VARCHAR ---
    with op.batch_alter_table('prealerts', schema=None) as batch_op:
        batch_op.alter_column(
            'purchase_date',
            existing_type=sa.Date(),
            type_=sa.VARCHAR(),
            existing_nullable=True,
        )

    # --- PACKAGES: revert types and add dropped columns back ---
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manifest_date', sa.VARCHAR(), autoincrement=False, nullable=True))
        batch_op.add_column(sa.Column('customer_id', sa.VARCHAR(), autoincrement=False, nullable=True))
        batch_op.add_column(sa.Column('customer_name', sa.VARCHAR(), autoincrement=False, nullable=True))
        batch_op.add_column(sa.Column('house_number', sa.VARCHAR(), autoincrement=False, nullable=True))

        batch_op.alter_column(
            'weight',
            existing_type=sa.Float(),
            type_=sa.NUMERIC(precision=10, scale=2),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'created_at',
            existing_type=sa.DateTime(),
            type_=sa.VARCHAR(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'date_received',
            existing_type=sa.DateTime(),
            type_=sa.VARCHAR(),
            existing_nullable=True,
        )
        batch_op.alter_column(
            'received_date',
            existing_type=sa.DateTime(),
            type_=sa.VARCHAR(),
            existing_nullable=True,
        )

    # --- INVOICES: revert to old structure ---
    with op.batch_alter_table('invoices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('total', sa.DOUBLE_PRECISION(precision=53), autoincrement=False, nullable=True))
        batch_op.drop_column('created_at')
        batch_op.drop_column('date_issued')
        batch_op.drop_column('grand_total')
        batch_op.drop_column('amount_due')
        batch_op.drop_column('amount')

    # ### end Alembic commands ###
