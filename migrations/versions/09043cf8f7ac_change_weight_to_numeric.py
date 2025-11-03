"""Change weight to Numeric

Revision ID: 09043cf8f7ac
Revises: eccba5019b6b
Create Date: 2025-08-23 11:12:26.152200

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '09043cf8f7ac'
down_revision = 'eccba5019b6b'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("packages") as batch_op:
        batch_op.alter_column(
            "weight",
            existing_type=sa.String(length=50),
            type_=sa.Numeric(10, 2),
            existing_nullable=True
        )


def downgrade():
    with op.batch_alter_table("packages") as batch_op:
        batch_op.alter_column(
            "weight",
            existing_type=sa.Numeric(10, 2),
            type_=sa.String(length=50),
            existing_nullable=True
        )
