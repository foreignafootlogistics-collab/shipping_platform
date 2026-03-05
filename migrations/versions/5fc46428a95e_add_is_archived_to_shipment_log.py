"""Add is_archived to shipment_log

Revision ID: 5fc46428a95e
Revises: 943b7d319ceb
Create Date: 2026-03-05 11:47:04.771511

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '5fc46428a95e'
down_revision = '943b7d319ceb'
branch_labels = None
depends_on = None


def upgrade():
    # 1) add column (default false so existing rows are safe)
    op.add_column(
        "shipment_log",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false"))
    )

    # 2) backfill: if archived_at already set, mark is_archived true
    op.execute("""
        UPDATE shipment_log
        SET is_archived = TRUE
        WHERE archived_at IS NOT NULL
    """)

    # 3) optional index
    op.create_index("ix_shipment_log_is_archived", "shipment_log", ["is_archived"], unique=False)

    # 4) remove server_default (nice cleanup; optional)
    op.alter_column("shipment_log", "is_archived", server_default=None)


def downgrade():
    op.drop_index("ix_shipment_log_is_archived", table_name="shipment_log")
    op.drop_column("shipment_log", "is_archived")
