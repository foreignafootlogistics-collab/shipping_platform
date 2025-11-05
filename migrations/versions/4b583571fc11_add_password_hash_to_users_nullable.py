"""add password_hash to users (nullable)

Revision ID: 4b583571fc11
Revises: 28feab3cc465
Create Date: 2025-11-04 07:44:03.290413

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4b583571fc11'
down_revision = '28feab3cc465'
branch_labels = None
depends_on = None


def upgrade():
    import sqlalchemy as sa
    from alembic import op

    bind = op.get_bind()
    insp = sa.inspect(bind)

    def has_column(table: str, col: str) -> bool:
        try:
            return any(c["name"] == col for c in insp.get_columns(table))
        except Exception:
            return False

    # Add users.password_hash if missing
    if not has_column("users", "password_hash"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))


def downgrade():
    import sqlalchemy as sa
    from alembic import op

    bind = op.get_bind()
    insp = sa.inspect(bind)

    def has_column(table: str, col: str) -> bool:
        try:
            return any(c["name"] == col for c in insp.get_columns(table))
        except Exception:
            return False

    # Drop users.password_hash if present
    if has_column("users", "password_hash"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("password_hash")
