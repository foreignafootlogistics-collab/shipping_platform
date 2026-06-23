"""add purchase request module

Revision ID: f2c81874e80f
Revises: c2f6a80b549f
Create Date: 2026-06-22 08:50:04.642172

"""
from alembic import op
import sqlalchemy as sa


revision = "f2c81874e80f"
down_revision = "c2f6a80b549f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "purchase_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("request_number", sa.String(length=30), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("product_url", sa.Text(), nullable=False),
        sa.Column("store_name", sa.String(length=120), nullable=True),
        sa.Column("item_name", sa.String(length=255), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("color", sa.String(length=100), nullable=True),
        sa.Column("size", sa.String(length=100), nullable=True),
        sa.Column("item_price_usd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("service_fee_jmd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("quoted_item_price_usd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("quoted_service_fee_jmd", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("quoted_at", sa.DateTime(), nullable=True),
        sa.Column("customer_fafl_number", sa.String(length=20), nullable=True),
        sa.Column("order_number", sa.String(length=120), nullable=True),
        sa.Column("merchant_tracking_number", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("invoice_id", sa.Integer(), nullable=True),
        sa.Column("package_id", sa.Integer(), nullable=True),
        sa.Column("purchased_by_admin_id", sa.Integer(), nullable=True),
        sa.Column("purchased_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.ForeignKeyConstraint(["package_id"], ["packages.id"]),
        sa.ForeignKeyConstraint(["purchased_by_admin_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("purchase_requests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_purchase_requests_invoice_id"), ["invoice_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_purchase_requests_package_id"), ["package_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_purchase_requests_request_number"), ["request_number"], unique=True)
        batch_op.create_index(batch_op.f("ix_purchase_requests_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_purchase_requests_user_id"), ["user_id"], unique=False)

    op.create_table(
        "purchase_request_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("purchase_request_id", sa.Integer(), nullable=False),
        sa.Column("file_url", sa.Text(), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=True),
        sa.Column("cloud_public_id", sa.String(length=255), nullable=True),
        sa.Column("cloud_resource_type", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["purchase_request_id"],
            ["purchase_requests.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    with op.batch_alter_table("purchase_request_attachments", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_purchase_request_attachments_purchase_request_id"),
            ["purchase_request_id"],
            unique=False,
        )

    with op.batch_alter_table("packages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("purchase_request_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_packages_purchase_request_id"),
            ["purchase_request_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_packages_purchase_request_id",
            "purchase_requests",
            ["purchase_request_id"],
            ["id"],
        )


def downgrade():
    with op.batch_alter_table("packages", schema=None) as batch_op:
        batch_op.drop_constraint("fk_packages_purchase_request_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_packages_purchase_request_id"))
        batch_op.drop_column("purchase_request_id")

    with op.batch_alter_table("purchase_request_attachments", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_purchase_request_attachments_purchase_request_id"))

    op.drop_table("purchase_request_attachments")

    with op.batch_alter_table("purchase_requests", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_purchase_requests_user_id"))
        batch_op.drop_index(batch_op.f("ix_purchase_requests_status"))
        batch_op.drop_index(batch_op.f("ix_purchase_requests_request_number"))
        batch_op.drop_index(batch_op.f("ix_purchase_requests_package_id"))
        batch_op.drop_index(batch_op.f("ix_purchase_requests_invoice_id"))

    op.drop_table("purchase_requests")