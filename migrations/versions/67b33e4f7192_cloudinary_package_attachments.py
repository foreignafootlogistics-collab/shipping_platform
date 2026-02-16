"""cloudinary package attachments

Revision ID: 67b33e4f7192
Revises: 926404325f48
Create Date: 2026-02-15 17:56:19.853734
"""
from alembic import op
import sqlalchemy as sa

revision = '67b33e4f7192'
down_revision = '926404325f48'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('package_attachments', sa.Column('file_url', sa.Text(), nullable=True))
    op.add_column('package_attachments', sa.Column('cloud_public_id', sa.String(length=255), nullable=True))
    op.add_column('package_attachments', sa.Column('cloud_resource_type', sa.String(length=20), nullable=True))

    op.execute("UPDATE package_attachments SET file_url = file_name WHERE file_url IS NULL")

    op.alter_column('package_attachments', 'file_url', existing_type=sa.Text(), nullable=False)

def downgrade():
    op.drop_column('package_attachments', 'cloud_resource_type')
    op.drop_column('package_attachments', 'cloud_public_id')
    op.drop_column('package_attachments', 'file_url')
