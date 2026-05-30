"""add delivery scan tracking

Revision ID: 11d9ddc98297
Revises: 25fa57f9f2de
Create Date: 2026-05-29 20:04:12.121056
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "11d9ddc98297"
down_revision = "25fa57f9f2de"
branch_labels = None
depends_on = None


def upgrade():

    with op.batch_alter_table("packages", schema=None) as batch_op:

        batch_op.add_column(
            sa.Column(
                "delivery_scan_status",
                sa.String(length=30),
                nullable=False,
                server_default="not_scanned"
            )
        )

        batch_op.add_column(
            sa.Column(
                "delivery_scanned_at",
                sa.DateTime(timezone=True),
                nullable=True
            )
        )

        batch_op.add_column(
            sa.Column(
                "delivery_scanned_by_id",
                sa.Integer(),
                nullable=True
            )
        )

        batch_op.create_index(
            batch_op.f("ix_packages_delivery_scan_status"),
            ["delivery_scan_status"],
            unique=False
        )

        batch_op.create_index(
            batch_op.f("ix_packages_delivery_scanned_by_id"),
            ["delivery_scanned_by_id"],
            unique=False
        )

        batch_op.create_foreign_key(
            None,
            "users",
            ["delivery_scanned_by_id"],
            ["id"]
        )


def downgrade():

    with op.batch_alter_table("packages", schema=None) as batch_op:

        batch_op.drop_constraint(None, type_="foreignkey")

        batch_op.drop_index(
            batch_op.f("ix_packages_delivery_scanned_by_id")
        )

        batch_op.drop_index(
            batch_op.f("ix_packages_delivery_scan_status")
        )

        batch_op.drop_column("delivery_scanned_by_id")
        batch_op.drop_column("delivery_scanned_at")
        batch_op.drop_column("delivery_scan_status")