"""sync user columns (minimal)

Revision ID: 643d14423f26
Revises: b4134fadbd83
Create Date: 2025-11-17 10:05:32.860032
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "643d14423f26"
down_revision = "b4134fadbd83"
branch_labels = None
depends_on = None


def upgrade():
    """Minimal upgrade: just add users.referred_by."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("referred_by", sa.Integer(), nullable=True))
        # If you ever want a FK later, you can add it in a new migration.
        # Keeping this minimal on purpose for the existing SQLite DB.


def downgrade():
    """Reverse the minimal upgrade."""
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("referred_by")

