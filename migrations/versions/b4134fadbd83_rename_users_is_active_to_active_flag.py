"""rename users.is_active to active flag

Revision ID: b4134fadbd83
Revises: 4b583571fc11
Create Date: 2025-11-04 08:18:57.439907

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b4134fadbd83'
down_revision = '4b583571fc11'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    def has_column(table, col):
        return any(c["name"] == col for c in insp.get_columns(table))

    # add active if missing
    if not has_column("users", "active"):
        with op.batch_alter_table("users") as b:
            b.add_column(sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("1")))
        with op.batch_alter_table("users") as b:
            b.alter_column("active", server_default=None)

    # drop old is_active if exists
    if has_column("users", "is_active"):
        with op.batch_alter_table("users") as b:
            b.drop_column("is_active")

def downgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("is_active", sa.Boolean(), nullable=True))
        b.drop_column("active")