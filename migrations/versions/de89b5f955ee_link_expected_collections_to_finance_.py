"""link expected collections to finance expenses

Revision ID: de89b5f955ee
Revises: 43d9be30286c
Create Date: 2026-08-05 22:31:35.632517

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "de89b5f955ee"
down_revision = "43d9be30286c"
branch_labels = None
depends_on = None


FK_NAME = (
    "fk_expected_package_collections_"
    "expense_id_expenses"
)


def upgrade():
    with op.batch_alter_table(
        "expected_package_collections",
        schema=None,
    ) as batch_op:

        batch_op.add_column(
            sa.Column(
                "expense_id",
                sa.Integer(),
                nullable=True,
            )
        )

        batch_op.add_column(
            sa.Column(
                "paid_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )

        batch_op.create_index(
            batch_op.f(
                "ix_expected_package_collections_expense_id"
            ),
            ["expense_id"],
            unique=True,
        )

        batch_op.create_foreign_key(
            FK_NAME,
            "expenses",
            ["expense_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table(
        "expected_package_collections",
        schema=None,
    ) as batch_op:

        batch_op.drop_constraint(
            FK_NAME,
            type_="foreignkey",
        )

        batch_op.drop_index(
            batch_op.f(
                "ix_expected_package_collections_expense_id"
            )
        )

        batch_op.drop_column(
            "paid_at"
        )

        batch_op.drop_column(
            "expense_id"
        )