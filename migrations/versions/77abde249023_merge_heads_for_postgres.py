"""merge heads for postgres

Revision ID: 77abde249023
Revises: 358365bdf36a, f7aab9ba8be9
Create Date: 2025-11-22 08:29:32.803840

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '77abde249023'
down_revision = ('358365bdf36a', 'f7aab9ba8be9')
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
