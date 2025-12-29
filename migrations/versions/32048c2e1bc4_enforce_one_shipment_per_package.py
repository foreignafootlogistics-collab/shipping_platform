"""enforce one shipment per package

Revision ID: 32048c2e1bc4
Revises: e6bb196ca2da
Create Date: 2025-11-26 13:09:02.517223

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '32048c2e1bc4'
down_revision = 'e6bb196ca2da'
branch_labels = None
depends_on = None


def upgrade():
    # 1) Clean up duplicates in shipment_packages so we can safely add UNIQUE
    conn = op.get_bind()

    # This Postgres trick keeps one row per package_id and deletes the rest
    conn.execute(sa.text("""
        DELETE FROM shipment_packages a
        USING shipment_packages b
        WHERE a.package_id = b.package_id
          AND a.ctid < b.ctid;
    """))

    # 2) Now enforce: one row per package_id
    op.create_unique_constraint(
        "uq_shipment_packages_package",
        "shipment_packages",
        ["package_id"],
    )


def downgrade():
    op.drop_constraint(
        "uq_shipment_packages_package",
        "shipment_packages",
        type_="unique",
    )