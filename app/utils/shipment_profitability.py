from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import (
    Discount,
    ExpectedPackageCollection,
    Invoice,
    Package,
    Payment,
)


MONEY_PLACES = Decimal("0.01")


def _decimal(value):
    try:
        return Decimal(str(value or 0))
    except Exception:
        return Decimal("0")


def _money(value):
    return _decimal(value).quantize(
        MONEY_PLACES,
        rounding=ROUND_HALF_UP,
    )


def _float_money(value):
    return float(_money(value))


def _package_customer_charge(package):
    """
    Package-level amount used to allocate an invoice when one
    invoice contains packages from multiple shipment collections.
    """
    for field_name in (
        "amount_due",
        "grand_total",
    ):
        value = _decimal(
            getattr(package, field_name, 0)
        )

        if value > 0:
            return value

    return Decimal("0")


def _package_pass_through(package):
    """
    Government/customs charges collected from the customer but
    excluded from FAFL operating revenue.
    """
    customs_total = _decimal(
        getattr(package, "customs_total", 0)
    )

    component_total = sum(
        (
            _decimal(
                getattr(package, field_name, 0)
            )
            for field_name in (
                "duty",
                "gct",
                "scf",
                "envl",
                "caf",
                "stamp",
            )
        ),
        Decimal("0"),
    )

    # Avoid counting customs_total and its components twice.
    return max(
        customs_total,
        component_total,
        Decimal("0"),
    )


def _snapshot_package_ids(record):
    snapshot = record.calculation_snapshot or {}
    package_ids = []
    seen_ids = set()

    for row in snapshot.get("package_rows", []):
        raw_id = row.get("package_id")

        try:
            package_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        if package_id not in seen_ids:
            package_ids.append(package_id)
            seen_ids.add(package_id)

    return package_ids


def _load_report_context(records):
    """
    Bulk-load everything needed for the complete report.

    This prevents running several SQL queries separately for
    every invoice in every EPC.
    """
    package_ids_by_record = {}
    all_selected_package_ids = set()

    for record in records:
        package_ids = _snapshot_package_ids(record)
        package_ids_by_record[record.id] = package_ids
        all_selected_package_ids.update(package_ids)

    if not all_selected_package_ids:
        return {
            "package_ids_by_record": package_ids_by_record,
            "selected_package_map": {},
            "invoice_map": {},
            "invoice_packages": {},
            "payment_totals": {},
            "discount_totals": {},
        }

    selected_packages = (
        Package.query
        .filter(
            Package.id.in_(
                all_selected_package_ids
            )
        )
        .all()
    )

    selected_package_map = {
        package.id: package
        for package in selected_packages
    }

    invoice_ids = {
        package.invoice_id
        for package in selected_packages
        if package.invoice_id
    }

    if not invoice_ids:
        return {
            "package_ids_by_record": package_ids_by_record,
            "selected_package_map": selected_package_map,
            "invoice_map": {},
            "invoice_packages": {},
            "payment_totals": {},
            "discount_totals": {},
        }

    invoices = (
        Invoice.query
        .options(
            selectinload(Invoice.user)
        )
        .filter(
            Invoice.id.in_(invoice_ids)
        )
        .all()
    )

    invoice_map = {
        invoice.id: invoice
        for invoice in invoices
    }

    all_invoice_packages = (
        Package.query
        .filter(
            Package.invoice_id.in_(
                invoice_ids
            )
        )
        .all()
    )

    invoice_packages = {}

    for package in all_invoice_packages:
        invoice_packages.setdefault(
            package.invoice_id,
            [],
        ).append(package)

    payment_query = (
        db.session.query(
            Payment.invoice_id,
            func.coalesce(
                func.sum(Payment.amount_jmd),
                0,
            ),
        )
        .filter(
            Payment.invoice_id.in_(
                invoice_ids
            )
        )
    )

    if hasattr(Payment, "transaction_type"):
        payment_query = payment_query.filter(
            Payment.transaction_type
            == "invoice_payment"
        )

    if hasattr(Payment, "status"):
        payment_query = payment_query.filter(
            func.lower(Payment.status)
            == "completed"
        )

    payment_rows = (
        payment_query
        .group_by(Payment.invoice_id)
        .all()
    )

    payment_totals = {
        invoice_id: _decimal(total)
        for invoice_id, total in payment_rows
    }

    discount_rows = (
        db.session.query(
            Discount.invoice_id,
            func.coalesce(
                func.sum(Discount.amount_jmd),
                0,
            ),
        )
        .filter(
            Discount.invoice_id.in_(
                invoice_ids
            )
        )
        .group_by(Discount.invoice_id)
        .all()
    )

    discount_totals = {
        invoice_id: _decimal(total)
        for invoice_id, total in discount_rows
    }

    return {
        "package_ids_by_record": package_ids_by_record,
        "selected_package_map": selected_package_map,
        "invoice_map": invoice_map,
        "invoice_packages": invoice_packages,
        "payment_totals": payment_totals,
        "discount_totals": discount_totals,
    }


def _invoice_totals(
    invoice,
    all_invoice_packages,
    context,
):
    """
    Calculate invoice totals from already bulk-loaded data.
    """
    package_sum = sum(
        (
            _package_customer_charge(package)
            for package in all_invoice_packages
        ),
        Decimal("0"),
    )

    subtotal = _decimal(
        getattr(
            invoice,
            "subtotal_before_discount",
            0,
        )
        or package_sum
        or getattr(invoice, "grand_total", 0)
        or getattr(invoice, "amount", 0)
        or getattr(invoice, "invoice_value", 0)
        or 0
    )

    subtotal = max(
        subtotal,
        Decimal("0"),
    )

    discount_rows_total = max(
        context["discount_totals"].get(
            invoice.id,
            Decimal("0"),
        ),
        Decimal("0"),
    )

    saved_discount_total = max(
        _decimal(
            getattr(
                invoice,
                "discount_total",
                0,
            )
        ),
        Decimal("0"),
    )

    # Older invoices may use Discount rows while newer invoices
    # may store Invoice.discount_total. Do not add both.
    discount_total = max(
        discount_rows_total,
        saved_discount_total,
    )

    discount_total = min(
        discount_total,
        subtotal,
    )

    payments_total = max(
        context["payment_totals"].get(
            invoice.id,
            Decimal("0"),
        ),
        Decimal("0"),
    )

    net_billed = max(
        subtotal - discount_total,
        Decimal("0"),
    )

    collected = min(
        payments_total,
        net_billed,
    )

    return (
        subtotal,
        discount_total,
        net_billed,
        collected,
    )


def calculate_shipment_profitability(
    record,
    context=None,
):
    """
    Calculate profitability for one EPC.

    The EPC calculation snapshot determines which packages belong
    to the profitability calculation, so later shipment splitting
    does not change previously captured package membership.
    """
    if not isinstance(
        record,
        ExpectedPackageCollection,
    ):
        raise TypeError(
            "Expected an ExpectedPackageCollection record."
        )

    if context is None:
        context = _load_report_context([record])

    package_ids = context[
        "package_ids_by_record"
    ].get(
        record.id,
        [],
    )

    package_map = context[
        "selected_package_map"
    ]

    selected_packages = [
        package_map[package_id]
        for package_id in package_ids
        if package_id in package_map
    ]

    missing_package_count = (
        len(package_ids)
        - len(selected_packages)
    )

    invoice_groups = {}
    uninvoiced_package_ids = []

    for package in selected_packages:
        if package.invoice_id:
            invoice_groups.setdefault(
                package.invoice_id,
                [],
            ).append(package)
        else:
            uninvoiced_package_ids.append(
                package.id
            )

    customer_billed = Decimal("0")
    customer_collected = Decimal("0")
    customer_outstanding = Decimal("0")
    pass_through = Decimal("0")
    allocated_discount = Decimal("0")

    invoice_rows = []

    for (
        invoice_id,
        shipment_packages,
    ) in invoice_groups.items():
        invoice = context[
            "invoice_map"
        ].get(invoice_id)

        if not invoice:
            continue

        all_invoice_packages = context[
            "invoice_packages"
        ].get(
            invoice_id,
            [],
        )

        selected_weight = sum(
            (
                _package_customer_charge(package)
                for package in shipment_packages
            ),
            Decimal("0"),
        )

        invoice_weight = sum(
            (
                _package_customer_charge(package)
                for package in all_invoice_packages
            ),
            Decimal("0"),
        )

        if invoice_weight > 0:
            allocation_share = (
                selected_weight
                / invoice_weight
            )
        elif all_invoice_packages:
            allocation_share = (
                Decimal(len(shipment_packages))
                / Decimal(len(all_invoice_packages))
            )
        else:
            allocation_share = Decimal("0")

        allocation_share = min(
            max(
                allocation_share,
                Decimal("0"),
            ),
            Decimal("1"),
        )

        (
            invoice_subtotal,
            invoice_discount,
            invoice_net_billed,
            invoice_collected,
        ) = _invoice_totals(
            invoice,
            all_invoice_packages,
            context,
        )

        allocated_billed = (
            invoice_net_billed
            * allocation_share
        )

        allocated_collected = (
            invoice_collected
            * allocation_share
        )

        allocated_outstanding = max(
            allocated_billed
            - allocated_collected,
            Decimal("0"),
        )

        invoice_allocated_discount = (
            invoice_discount
            * allocation_share
        )

        selected_pass_through = sum(
            (
                _package_pass_through(package)
                for package in shipment_packages
            ),
            Decimal("0"),
        )

        selected_pass_through = min(
            selected_pass_through,
            allocated_billed,
        )

        customer_billed += allocated_billed
        customer_collected += allocated_collected
        customer_outstanding += (
            allocated_outstanding
        )
        pass_through += selected_pass_through
        allocated_discount += (
            invoice_allocated_discount
        )

        customer_name = ""

        if invoice.user:
            customer_name = (
                invoice.user.full_name
                or invoice.user.email
                or ""
            )

        invoice_rows.append(
            {
                "invoice_id": invoice.id,
                "invoice_number": (
                    invoice.invoice_number
                ),
                "customer_name": customer_name,
                "package_count": len(
                    shipment_packages
                ),
                "selected_package_count": len(
                    shipment_packages
                ),
                "invoice_package_count": len(
                    all_invoice_packages
                ),
                "allocation_percent": (
                    _float_money(
                        allocation_share
                        * Decimal("100")
                    )
                ),
                "allocated_billed_jmd": (
                    _float_money(
                        allocated_billed
                    )
                ),
                "billed_jmd": (
                    _float_money(
                        allocated_billed
                    )
                ),
                "discount_jmd": (
                    _float_money(
                        invoice_allocated_discount
                    )
                ),
                "allocated_collected_jmd": (
                    _float_money(
                        allocated_collected
                    )
                ),
                "collected_jmd": (
                    _float_money(
                        allocated_collected
                    )
                ),
                "outstanding_jmd": (
                    _float_money(
                        allocated_outstanding
                    )
                ),
                "pass_through_jmd": (
                    _float_money(
                        selected_pass_through
                    )
                ),
            }
        )

    fafl_operating_revenue = max(
        customer_billed - pass_through,
        Decimal("0"),
    )

    if record.actual_total_jmd is not None:
        supplier_cost = _decimal(
            record.actual_total_jmd
        )
        supplier_cost_basis = "Actual supplier payment"
    else:
        supplier_cost = _decimal(
            record.expected_total_jmd
        )
        supplier_cost_basis = "Expected EPC cost"

    gross_profit = (
        fafl_operating_revenue
        - supplier_cost
    )

    if fafl_operating_revenue > 0:
        profit_margin = (
            gross_profit
            / fafl_operating_revenue
            * Decimal("100")
        )
    else:
        profit_margin = Decimal("0")

    if customer_billed > 0:
        collected_percent = (
            customer_collected
            / customer_billed
            * Decimal("100")
        )

        outstanding_percent = (
            customer_outstanding
            / customer_billed
            * Decimal("100")
        )
    else:
        collected_percent = Decimal("0")
        outstanding_percent = Decimal("0")

    supplier_is_actual = (
        record.actual_total_jmd is not None
    )

    if (
        supplier_is_actual
        and customer_billed > 0
        and customer_outstanding
        <= Decimal("0.01")
    ):
        profitability_status = "realized"
    elif (
        customer_collected > 0
        or supplier_is_actual
    ):
        profitability_status = "partial"
    else:
        profitability_status = "projected"

    shipment = record.shipment

    shipment_reference = (
        (
            shipment.sl_name
            or shipment.sl_id
        )
        if shipment
        else "Deleted Shipment"
    )

    return {
        "record_id": record.id,
        "collection_id": record.id,
        "collection_number": (
            record.collection_number
        ),
        "shipment_id": (
            shipment.id
            if shipment
            else None
        ),
        "shipment_name": shipment_reference,
        "shipment_reference": shipment_reference,
        "shipment_code": (
            shipment.sl_id
            if shipment
            else ""
        ),
        "shipment_created_at": (
            shipment.created_at
            if shipment
            else record.created_at
        ),
        "epc_status": record.status,
        "status": profitability_status,
        "profitability_status": (
            profitability_status
        ),
        "package_count": len(package_ids),
        "loaded_package_count": (
            len(selected_packages)
        ),
        "missing_package_count": (
            missing_package_count
        ),
        "invoiced_package_count": (
            len(selected_packages)
            - len(uninvoiced_package_ids)
        ),
        "uninvoiced_package_count": (
            len(uninvoiced_package_ids)
        ),
        "uninvoiced_package_ids": (
            uninvoiced_package_ids
        ),
        "invoice_count": len(invoice_rows),
        "customer_billed_jmd": (
            _float_money(customer_billed)
        ),
        "discount_jmd": (
            _float_money(allocated_discount)
        ),
        "pass_through_jmd": (
            _float_money(pass_through)
        ),
        "fafl_revenue_jmd": (
            _float_money(
                fafl_operating_revenue
            )
        ),
        "fafl_operating_revenue_jmd": (
            _float_money(
                fafl_operating_revenue
            )
        ),
        "customer_collected_jmd": (
            _float_money(
                customer_collected
            )
        ),
        "customer_outstanding_jmd": (
            _float_money(
                customer_outstanding
            )
        ),
        "collected_percent": (
            _float_money(collected_percent)
        ),
        "outstanding_percent": (
            _float_money(
                outstanding_percent
            )
        ),
        "supplier_cost_jmd": (
            _float_money(supplier_cost)
        ),
        "supplier_cost_source": (
            supplier_cost_basis
        ),
        "supplier_cost_basis": (
            supplier_cost_basis
        ),
        "gross_profit_jmd": (
            _float_money(gross_profit)
        ),
        "profit_margin": (
            _float_money(profit_margin)
        ),
        "profit_margin_percent": (
            _float_money(profit_margin)
        ),
        "invoice_rows": sorted(
            invoice_rows,
            key=lambda row: (
                row["invoice_number"] or ""
            ),
        ),
    }


def calculate_profitability_report(records):
    records = list(records)

    context = _load_report_context(
        records
    )

    rows = [
        calculate_shipment_profitability(
            record,
            context=context,
        )
        for record in records
    ]

    totals = {
        "customer_billed_jmd": 0.0,
        "pass_through_jmd": 0.0,
        "fafl_revenue_jmd": 0.0,
        "fafl_operating_revenue_jmd": 0.0,
        "customer_collected_jmd": 0.0,
        "customer_outstanding_jmd": 0.0,
        "supplier_cost_jmd": 0.0,
        "gross_profit_jmd": 0.0,
    }

    for row in rows:
        for key in totals:
            totals[key] += float(
                row.get(key, 0) or 0
            )

    for key in totals:
        totals[key] = _float_money(
            totals[key]
        )

    operating_revenue = _decimal(
        totals["fafl_revenue_jmd"]
    )

    gross_profit = _decimal(
        totals["gross_profit_jmd"]
    )

    if operating_revenue > 0:
        overall_margin = (
            gross_profit
            / operating_revenue
            * Decimal("100")
        )
    else:
        overall_margin = Decimal("0")

    totals["profit_margin"] = (
        _float_money(overall_margin)
    )

    totals["profit_margin_percent"] = (
        _float_money(overall_margin)
    )

    return rows, totals