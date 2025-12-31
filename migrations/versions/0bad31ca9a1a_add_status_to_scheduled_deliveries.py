"""add status to scheduled deliveries

Revision ID: 0bad31ca9a1a
Revises: 5dca82a7efcf
Create Date: 2025-12-30 22:43:37.849296

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '0bad31ca9a1a'
down_revision = '5dca82a7efcf'
branch_labels = None
depends_on = None


def upgrade():
    # 1) add with a SERVER default so existing rows get filled
    op.add_column(
        "scheduled_deliveries",
        sa.Column("status", sa.String(length=30), server_default="Scheduled", nullable=False),
    )

    # 2) optional: remove server default afterward (keep model default in Python)
    op.alter_column("scheduled_deliveries", "status", server_default=None)
    # ### end Alembic commands ###


def downgrade():
    op.drop_column("scheduled_deliveries", "status")


    # ### end Alembic commands ###
