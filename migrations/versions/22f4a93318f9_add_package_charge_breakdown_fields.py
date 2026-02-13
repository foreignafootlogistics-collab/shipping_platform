"""add package charge breakdown fields

Revision ID: 22f4a93318f9
Revises: 751729cded60
Create Date: 2026-02-12 20:37:05.195147

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '22f4a93318f9'
down_revision = '751729cded60'
branch_labels = None
depends_on = None


def upgrade():
    # ✅ Add columns with SERVER defaults so existing rows get 0 (and NOT NULL works)
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.add_column(sa.Column('duty', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('gct', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('scf', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('envl', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('caf', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('stamp', sa.Float(), nullable=False, server_default=sa.text("0")))

        batch_op.add_column(sa.Column('customs_total', sa.Float(), nullable=False, server_default=sa.text("0")))

        batch_op.add_column(sa.Column('freight_fee', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('handling_fee', sa.Float(), nullable=False, server_default=sa.text("0")))

        batch_op.add_column(sa.Column('freight_total', sa.Float(), nullable=False, server_default=sa.text("0")))
        batch_op.add_column(sa.Column('grand_total', sa.Float(), nullable=False, server_default=sa.text("0")))

    # ✅ optional safety backfill (server_default normally already handles existing rows)
    op.execute("""
        UPDATE packages
        SET
          duty = COALESCE(duty, 0),
          gct = COALESCE(gct, 0),
          scf = COALESCE(scf, 0),
          envl = COALESCE(envl, 0),
          caf = COALESCE(caf, 0),
          stamp = COALESCE(stamp, 0),
          customs_total = COALESCE(customs_total, 0),
          freight_fee = COALESCE(freight_fee, 0),
          handling_fee = COALESCE(handling_fee, 0),
          freight_total = COALESCE(freight_total, 0),
          grand_total = COALESCE(grand_total, 0)
    """)

    # ✅ remove server defaults so DB doesn't permanently force defaults (your app model defaults take over)
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.alter_column('duty', server_default=None)
        batch_op.alter_column('gct', server_default=None)
        batch_op.alter_column('scf', server_default=None)
        batch_op.alter_column('envl', server_default=None)
        batch_op.alter_column('caf', server_default=None)
        batch_op.alter_column('stamp', server_default=None)

        batch_op.alter_column('customs_total', server_default=None)

        batch_op.alter_column('freight_fee', server_default=None)
        batch_op.alter_column('handling_fee', server_default=None)

        batch_op.alter_column('freight_total', server_default=None)
        batch_op.alter_column('grand_total', server_default=None)


def downgrade():
    with op.batch_alter_table('packages', schema=None) as batch_op:
        batch_op.drop_column('grand_total')
        batch_op.drop_column('freight_total')
        batch_op.drop_column('handling_fee')
        batch_op.drop_column('freight_fee')
        batch_op.drop_column('customs_total')
        batch_op.drop_column('stamp')
        batch_op.drop_column('caf')
        batch_op.drop_column('envl')
        batch_op.drop_column('scf')
        batch_op.drop_column('gct')
        batch_op.drop_column('duty')

