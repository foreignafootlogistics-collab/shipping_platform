"""add packages customer columns

Revision ID: 616cea7e6410
Revises: 643d14423f26
Create Date: 2025-11-17 18:52:52.391778

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '616cea7e6410'
down_revision = '643d14423f26'
branch_labels = None
depends_on = None


def upgrade():
    # add the new columns used by the logistics dashboard
    with op.batch_alter_table("packages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("customer_name", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("customer_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("merchant", sa.String(), nullable=True))


def downgrade():
    # remove them if we ever roll back
    with op.batch_alter_table("packages", schema=None) as batch_op:
        batch_op.drop_column("merchant")
        batch_op.drop_column("customer_id")
        batch_op.drop_column("customer_name")