"""add package sorting codes

Revision ID: 570337e2011b
Revises: 6e822c38a507
Create Date: 2026-07-30 00:09:02.072408
"""

from alembic import op
import sqlalchemy as sa


# Revision identifiers used by Alembic.
revision = "570337e2011b"
down_revision = "6e822c38a507"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table(
        "packages",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "sort_code",
                sa.String(length=20),
                server_default="UNASSIGNED",
                nullable=False,
            )
        )

        batch_op.add_column(
            sa.Column(
                "sort_code_source",
                sa.String(length=30),
                server_default="system",
                nullable=False,
            )
        )

        batch_op.add_column(
            sa.Column(
                "sort_code_locked",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            )
        )

        batch_op.add_column(
            sa.Column(
                "sort_code_updated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

        batch_op.add_column(
            sa.Column(
                "sort_code_updated_by_id",
                sa.Integer(),
                nullable=True,
            )
        )

        batch_op.create_index(
            "ix_packages_sort_code",
            ["sort_code"],
            unique=False,
        )

        batch_op.create_index(
            "ix_packages_sort_code_source",
            ["sort_code_source"],
            unique=False,
        )

        batch_op.create_index(
            "ix_packages_sort_code_updated_by_id",
            ["sort_code_updated_by_id"],
            unique=False,
        )

        batch_op.create_foreign_key(
            "fk_packages_sort_code_updated_by_id_users",
            "users",
            ["sort_code_updated_by_id"],
            ["id"],
        )

    with op.batch_alter_table(
        "users",
        schema=None,
    ) as batch_op:
        batch_op.add_column(
            sa.Column(
                "default_sort_code",
                sa.String(length=20),
                server_default="UNASSIGNED",
                nullable=False,
            )
        )

        batch_op.create_index(
            "ix_users_default_sort_code",
            ["default_sort_code"],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table(
        "packages",
        schema=None,
    ) as batch_op:
        batch_op.drop_constraint(
            "fk_packages_sort_code_updated_by_id_users",
            type_="foreignkey",
        )

        batch_op.drop_index(
            "ix_packages_sort_code_updated_by_id"
        )

        batch_op.drop_index(
            "ix_packages_sort_code_source"
        )

        batch_op.drop_index(
            "ix_packages_sort_code"
        )

        batch_op.drop_column(
            "sort_code_updated_by_id"
        )

        batch_op.drop_column(
            "sort_code_updated_at"
        )

        batch_op.drop_column(
            "sort_code_locked"
        )

        batch_op.drop_column(
            "sort_code_source"
        )

        batch_op.drop_column(
            "sort_code"
        )

    with op.batch_alter_table(
        "users",
        schema=None,
    ) as batch_op:
        batch_op.drop_index(
            "ix_users_default_sort_code"
        )

        batch_op.drop_column(
            "default_sort_code"
        )