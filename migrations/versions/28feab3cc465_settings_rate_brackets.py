"""settings + rate_brackets

Revision ID: 28feab3cc465
Revises: f7b150909b23
Create Date: 2025-11-04 00:19:39.779855
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "28feab3cc465"
down_revision = "f7b150909b23"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    def has_column(table, col):
        try:
            return any(c["name"] == col for c in insp.get_columns(table))
        except Exception:
            return False

    def has_table(name):
        try:
            return insp.has_table(name)
        except Exception:
            return False

    def has_index(table, name):
        try:
            return any(ix["name"] == name for ix in insp.get_indexes(table))
        except Exception:
            return False

    def create_index_if_missing(name, table, cols, unique=False):
        if not has_index(table, name):
            op.create_index(name, table, cols, unique=unique)

    # ---------- SETTINGS (guard if already exists) ----------
    if not has_table("settings"):
        op.create_table(
            "settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("company_name", sa.String(length=255), nullable=True),
            sa.Column("company_address", sa.Text(), nullable=True),
            sa.Column("company_email", sa.String(length=255), nullable=True),
            sa.Column("logo_path", sa.String(length=255), nullable=True),
            sa.Column("currency_code", sa.String(length=8), nullable=True),
            sa.Column("currency_symbol", sa.String(length=8), nullable=True),
            sa.Column("usd_to_jmd", sa.Numeric(precision=12, scale=4), nullable=True),
            sa.Column("date_format", sa.String(length=32), nullable=True),
            sa.Column("base_rate", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("handling_fee", sa.Numeric(precision=12, scale=2), nullable=True),
            sa.Column("branches", sa.Text(), nullable=True),
            sa.Column("terms", sa.Text(), nullable=True),
            sa.Column("privacy_policy", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=True,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id", name="pk_settings"),
        )

    # ---------- counters (drop if exists) ----------
    if has_table("counters"):
        op.drop_table("counters")

    # ---------- authorized_pickups ----------
    create_index_if_missing("ix_authorized_pickups_created_at", "authorized_pickups", ["created_at"])

    # ---------- calculator_logs ----------
    create_index_if_missing("ix_calculator_logs_created_at", "calculator_logs", ["created_at"])

    # ---------- discounts ----------
    create_index_if_missing("ix_discounts_invoice_id", "discounts", ["invoice_id"])

    # ---------- invoices (ALL adds guarded) ----------
    def add_invoice(colname, column_obj):
        if not has_column("invoices", colname):
            with op.batch_alter_table("invoices") as batch_op:
                batch_op.add_column(column_obj)

    add_invoice("amount", sa.Column("amount", sa.Float(), nullable=True))
    add_invoice("amount_due", sa.Column("amount_due", sa.Float(), nullable=True))
    add_invoice("grand_total", sa.Column("grand_total", sa.Float(), nullable=True))
    add_invoice("date_issued", sa.Column("date_issued", sa.DateTime(), nullable=True))
    add_invoice("created_at", sa.Column("created_at", sa.DateTime(), nullable=True))

    create_index_if_missing("ix_invoices_status", "invoices", ["status"])

    # ---------- messages ----------
    create_index_if_missing("ix_messages_created_at", "messages", ["created_at"])
    create_index_if_missing("ix_messages_is_read", "messages", ["is_read"])

    # ---------- notifications ----------
    create_index_if_missing("ix_notifications_created_at", "notifications", ["created_at"])
    create_index_if_missing("ix_notifications_is_read", "notifications", ["is_read"])

    # ---------- packages ----------
    # Defensive cleanup for any prior failed batch on SQLite
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_packages")

    with op.batch_alter_table("packages") as batch_op:
        if not has_column("packages", "category"):
            batch_op.add_column(sa.Column("category", sa.String(length=120), nullable=True))
        if not has_column("packages", "shipper"):
            batch_op.add_column(sa.Column("shipper", sa.String(length=120), nullable=True))
        if not has_column("packages", "epc"):
            batch_op.add_column(sa.Column("epc", sa.Integer(), nullable=False, server_default="0"))
        for col in (
            ("duty", sa.Float()),
            ("scf", sa.Float()),
            ("envl", sa.Float()),
            ("caf", sa.Float()),
            ("gct", sa.Float()),
            ("stamp", sa.Float()),
            ("customs_total", sa.Float()),
            ("freight_fee", sa.Float()),
            ("storage_fee", sa.Float()),
            ("freight_total", sa.Float()),
            ("other_charges", sa.Float()),
            ("grand_total", sa.Float()),
        ):
            if not has_column("packages", col[0]):
                batch_op.add_column(sa.Column(col[0], col[1], nullable=True))

        # Type normalizations (best-effort)
        try:
            batch_op.alter_column("weight", existing_type=sa.NUMERIC(precision=10, scale=2), type_=sa.Float(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("date_received", existing_type=sa.VARCHAR(), type_=sa.Date(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("received_date", existing_type=sa.VARCHAR(), type_=sa.Date(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("created_at", existing_type=sa.VARCHAR(), type_=sa.DateTime(), existing_nullable=True)
        except Exception:
            pass

        # best-effort drops (they may not exist)
        for col in ("customer_name", "customer_id", "merchant"):
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

    create_index_if_missing("ix_packages_created_at", "packages", ["created_at"])
    create_index_if_missing("ix_packages_epc", "packages", ["epc"])
    create_index_if_missing("ix_packages_status", "packages", ["status"])

    # ---------- payments ----------
    create_index_if_missing("ix_payments_created_at", "payments", ["created_at"])
    create_index_if_missing("ix_payments_invoice_id", "payments", ["invoice_id"])
    create_index_if_missing("ix_payments_user_id", "payments", ["user_id"])

    # ---------- pending_referrals ----------
    with op.batch_alter_table("pending_referrals") as batch_op:
        if not has_column("pending_referrals", "referred_user_id"):
            batch_op.add_column(sa.Column("referred_user_id", sa.Integer(), nullable=False))
        if not has_column("pending_referrals", "bonus_amount"):
            batch_op.add_column(sa.Column("bonus_amount", sa.Float(), nullable=False))
        # FK (best-effort)
        try:
            batch_op.create_foreign_key(
                "fk_pending_referrals_referred_user_id_users",
                referent_table="users",
                local_cols=["referred_user_id"],
                remote_cols=["id"],
                ondelete=None,
            )
        except Exception:
            pass
        # best-effort drops
        for col in ("accepted", "referred_email"):
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

    create_index_if_missing("ix_pending_referrals_created_at", "pending_referrals", ["created_at"])
    create_index_if_missing("ix_pending_referrals_referred_user_id", "pending_referrals", ["referred_user_id"])

    # ---------- prealerts ----------
    # Defensive cleanup for any prior failed batch on SQLite
    op.execute("DROP TABLE IF EXISTS _alembic_tmp_prealerts")

    # Add column + normalize type in a batch
    with op.batch_alter_table("prealerts") as batch_op:
        if not has_column("prealerts", "user_id"):
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        try:
            batch_op.alter_column(
                "purchase_date",
                existing_type=sa.VARCHAR(),
                type_=sa.Date(),
                existing_nullable=True,
            )
        except Exception:
            pass
        # DO NOT drop a named FK that may not exist on SQLite; skip it.
        # Also skip dropping an index by name if it might not exist.

    # Best-effort: create new FK (if needed)
    try:
        if has_column("prealerts", "user_id"):
            with op.batch_alter_table("prealerts") as batch_op:
                try:
                    batch_op.create_foreign_key(
                        "fk_prealerts_user_id_users",
                        referent_table="users",
                        local_cols=["user_id"],
                        remote_cols=["id"],
                        ondelete="SET NULL",
                    )
                except Exception:
                    pass
    except Exception:
        pass

    # Best-effort: drop old index if it exists
    try:
        with op.batch_alter_table("prealerts") as batch_op:
            try:
                batch_op.drop_index(batch_op.f("ix_prealerts_customer_id"))
            except Exception:
                pass
    except Exception:
        pass

    # Best-effort: drop old column if present
    if has_column("prealerts", "customer_id"):
        with op.batch_alter_table("prealerts") as batch_op:
            try:
                batch_op.drop_column("customer_id")
            except Exception:
                pass

    create_index_if_missing("ix_prealerts_created_at", "prealerts", ["created_at"])
    create_index_if_missing("ix_prealerts_prealert_number", "prealerts", ["prealert_number"])
    create_index_if_missing("ix_prealerts_user_id", "prealerts", ["user_id"])

    # ---------- rate_brackets ----------
    with op.batch_alter_table("rate_brackets") as batch_op:
        try:
            if not has_column("rate_brackets", "created_at"):
                batch_op.add_column(
                    sa.Column(
                        "created_at",
                        sa.DateTime(timezone=True),
                        server_default=sa.text("(CURRENT_TIMESTAMP)"),
                        nullable=True,
                    )
                )
        except Exception:
            pass
        try:
            batch_op.alter_column(
                "max_weight",
                existing_type=sa.INTEGER(),
                type_=sa.Numeric(precision=10, scale=2),
                existing_nullable=False,
            )
        except Exception:
            pass
        try:
            batch_op.alter_column(
                "rate",
                existing_type=sa.FLOAT(),
                type_=sa.Numeric(precision=12, scale=2),
                existing_nullable=False,
            )
        except Exception:
            pass
        # unique constraint (SQLite represents uniques as indexes; guard with try)
        try:
            batch_op.create_unique_constraint("ux_rate_brackets_max_weight", ["max_weight"])
        except Exception:
            pass

    # ---------- scheduled_deliveries ----------
    create_index_if_missing("ix_scheduled_deliveries_created_at", "scheduled_deliveries", ["created_at"])

    # ---------- shipment_log ----------
    create_index_if_missing("ix_shipment_log_created_at", "shipment_log", ["created_at"])

    # ---------- shipment_packages ----------
    with op.batch_alter_table("shipment_packages") as batch_op:
        try:
            batch_op.alter_column("shipment_id", existing_type=sa.INTEGER(), nullable=False)
        except Exception:
            pass
        try:
            batch_op.alter_column("package_id", existing_type=sa.INTEGER(), nullable=False)
        except Exception:
            pass

    # ---------- users ----------
    if not has_column("users", "password_hash"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.add_column(sa.Column("password_hash", sa.String(length=255), nullable=True))

    with op.batch_alter_table("users") as batch_op:
        try:
            batch_op.alter_column("password", existing_type=sa.BLOB(), nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("created_at", existing_type=sa.VARCHAR(), type_=sa.DateTime(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("date_registered", existing_type=sa.VARCHAR(), type_=sa.DateTime(), existing_nullable=True)
        except Exception:
            pass
        # self-FK (named) best-effort
        try:
            batch_op.create_foreign_key(
                "fk_users_referrer_id_users",
                referent_table="users",
                local_cols=["referrer_id"],
                remote_cols=["id"],
                ondelete="SET NULL",
            )
        except Exception:
            pass
        # best-effort drop old column
        try:
            batch_op.drop_column("referred_by")
        except Exception:
            pass

    create_index_if_missing("ix_users_is_admin", "users", ["is_admin"])
    create_index_if_missing("ix_users_referral_code", "users", ["referral_code"])
    create_index_if_missing("ix_users_referrer_id", "users", ["referrer_id"])
    create_index_if_missing("ix_users_role", "users", ["role"])

    # ---------- wallet_transactions ----------
    create_index_if_missing("ix_wallet_transactions_created_at", "wallet_transactions", ["created_at"])


def downgrade():
    bind = op.get_bind()
    insp = sa.inspect(bind)

    def has_column(table, col):
        try:
            return any(c["name"] == col for c in insp.get_columns(table))
        except Exception:
            return False

    def has_table(name):
        try:
            return insp.has_table(name)
        except Exception:
            return False

    def has_index(table, name):
        try:
            return any(ix["name"] == name for ix in insp.get_indexes(table))
        except Exception:
            return False

    def drop_index_if_exists(name, table):
        if has_index(table, name):
            op.drop_index(name, table_name=table)

    # users
    if has_column("users", "password_hash"):
        with op.batch_alter_table("users") as batch_op:
            batch_op.drop_column("password_hash")

    # invoices (reverse guarded)
    for col in ("created_at", "date_issued", "grand_total", "amount_due", "amount"):
        if has_column("invoices", col):
            with op.batch_alter_table("invoices") as batch_op:
                try:
                    batch_op.drop_column(col)
                except Exception:
                    pass

    # reverse indexes
    drop_index_if_exists("ix_wallet_transactions_created_at", "wallet_transactions")

    with op.batch_alter_table("users") as batch_op:
        try:
            batch_op.add_column(sa.Column("referred_by", sa.INTEGER(), nullable=True))
        except Exception:
            pass
        try:
            batch_op.drop_constraint("fk_users_referrer_id_users", type_="foreignkey")
        except Exception:
            pass

    drop_index_if_exists("ix_users_role", "users")
    drop_index_if_exists("ix_users_referrer_id", "users")
    drop_index_if_exists("ix_users_referral_code", "users")
    drop_index_if_exists("ix_users_is_admin", "users")

    with op.batch_alter_table("users") as batch_op:
        try:
            batch_op.alter_column("date_registered", existing_type=sa.DateTime(), type_=sa.VARCHAR(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("created_at", existing_type=sa.DateTime(), type_=sa.VARCHAR(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("password", existing_type=sa.BLOB(), nullable=False)
        except Exception:
            pass

    with op.batch_alter_table("shipment_packages") as batch_op:
        try:
            batch_op.alter_column("package_id", existing_type=sa.INTEGER(), nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("shipment_id", existing_type=sa.INTEGER(), nullable=True)
        except Exception:
            pass

    drop_index_if_exists("ix_shipment_log_created_at", "shipment_log")
    drop_index_if_exists("ix_scheduled_deliveries_created_at", "scheduled_deliveries")

    with op.batch_alter_table("rate_brackets") as batch_op:
        try:
            batch_op.drop_constraint("ux_rate_brackets_max_weight", type_="unique")
        except Exception:
            pass
        try:
            batch_op.alter_column("rate", existing_type=sa.Numeric(precision=12, scale=2), type_=sa.FLOAT(), existing_nullable=False)
        except Exception:
            pass
        try:
            batch_op.alter_column("max_weight", existing_type=sa.Numeric(precision=10, scale=2), type_=sa.INTEGER(), existing_nullable=False)
        except Exception:
            pass
        try:
            batch_op.drop_column("created_at")
        except Exception:
            pass

    # prealerts reverse (best-effort)
    if not has_column("prealerts", "customer_id"):
        with op.batch_alter_table("prealerts") as batch_op:
            try:
                batch_op.add_column(sa.Column("customer_id", sa.INTEGER(), nullable=True))
            except Exception:
                pass
    try:
        with op.batch_alter_table("prealerts") as batch_op:
            try:
                batch_op.drop_constraint("fk_prealerts_user_id_users", type_="foreignkey")
            except Exception:
                pass
    except Exception:
        pass
    drop_index_if_exists("ix_prealerts_user_id", "prealerts")
    drop_index_if_exists("ix_prealerts_prealert_number", "prealerts")
    drop_index_if_exists("ix_prealerts_created_at", "prealerts")
    try:
        with op.batch_alter_table("prealerts") as batch_op:
            try:
                batch_op.create_index(batch_op.f("ix_prealerts_customer_id"), ["customer_id"], unique=False)
            except Exception:
                pass
            try:
                batch_op.alter_column("purchase_date", existing_type=sa.Date(), type_=sa.VARCHAR(), existing_nullable=True)
            except Exception:
                pass
            try:
                batch_op.drop_column("user_id")
            except Exception:
                pass
    except Exception:
        pass

    # pending_referrals reverse
    drop_index_if_exists("ix_pending_referrals_referred_user_id", "pending_referrals")
    drop_index_if_exists("ix_pending_referrals_created_at", "pending_referrals")
    with op.batch_alter_table("pending_referrals") as batch_op:
        try:
            batch_op.add_column(sa.Column("referred_email", sa.VARCHAR(length=120), nullable=False))
        except Exception:
            pass
        try:
            batch_op.add_column(sa.Column("accepted", sa.BOOLEAN(), nullable=True))
        except Exception:
            pass
        try:
            batch_op.drop_column("bonus_amount")
        except Exception:
            pass
        try:
            batch_op.drop_column("referred_user_id")
        except Exception:
            pass

    # packages reverse (best-effort)
    drop_index_if_exists("ix_packages_status", "packages")
    drop_index_if_exists("ix_packages_epc", "packages")
    drop_index_if_exists("ix_packages_created_at", "packages")

    with op.batch_alter_table("packages") as batch_op:
        try:
            batch_op.add_column(sa.Column("merchant", sa.VARCHAR(), nullable=True))
        except Exception:
            pass
        try:
            batch_op.add_column(sa.Column("customer_id", sa.VARCHAR(), nullable=True))
        except Exception:
            pass
        try:
            batch_op.add_column(sa.Column("customer_name", sa.VARCHAR(), nullable=True))
        except Exception:
            pass

    with op.batch_alter_table("packages") as batch_op:
        try:
            batch_op.alter_column("created_at", existing_type=sa.DateTime(), type_=sa.VARCHAR(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("received_date", existing_type=sa.Date(), type_=sa.VARCHAR(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("date_received", existing_type=sa.Date(), type_=sa.VARCHAR(), existing_nullable=True)
        except Exception:
            pass
        try:
            batch_op.alter_column("weight", existing_type=sa.Float(), type_=sa.NUMERIC(precision=10, scale=2), existing_nullable=True)
        except Exception:
            pass
        # drop added columns (best-effort)
        for col in (
            "grand_total",
            "other_charges",
            "freight_total",
            "storage_fee",
            "freight_fee",
            "customs_total",
            "stamp",
            "gct",
            "caf",
            "envl",
            "scf",
            "duty",
            "epc",
            "shipper",
            "category",
        ):
            try:
                batch_op.drop_column(col)
            except Exception:
                pass

    # notifications reverse idx
    drop_index_if_exists("ix_notifications_is_read", "notifications")
    drop_index_if_exists("ix_notifications_created_at", "notifications")

    # messages reverse idx
    drop_index_if_exists("ix_messages_is_read", "messages")
    drop_index_if_exists("ix_messages_created_at", "messages")

    # invoices reverse idx
    drop_index_if_exists("ix_invoices_status", "invoices")

    # discounts/calculator/authorized_pickups reverse idx
    drop_index_if_exists("ix_discounts_invoice_id", "discounts")
    drop_index_if_exists("ix_calculator_logs_created_at", "calculator_logs")
    drop_index_if_exists("ix_authorized_pickups_created_at", "authorized_pickups")

    # recreate counters table
    if not has_table("counters"):
        op.create_table(
            "counters",
            sa.Column("name", sa.TEXT(), nullable=True),
            sa.Column("value", sa.INTEGER(), server_default=sa.text("0"), nullable=False),
            sa.PrimaryKeyConstraint("name", name="pk_counters"),
        )

    # drop settings last (if exists)
    if has_table("settings"):
        op.drop_table("settings")
