from datetime import datetime, timezone, timedelta
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify
from flask_login import current_user
from sqlalchemy import or_

from app.extensions import db
from app.models import Claim, ClaimAuditLog, Wallet, WalletTransaction, Payment, User, AuditLog
from app.forms import AdminClaimDecisionForm
from app.routes.admin_auth_routes import admin_required
from app.utils.email_utils import send_claim_status_update_email
from app.utils.time import to_jamaica

from zoneinfo import ZoneInfo

from app.utils.claims import (
    get_eligible_claim_packages,
)

# Optional in-app messaging
try:
    from app.models import Message
except Exception:
    Message = None


admin_claims_bp = Blueprint("admin_claims", __name__, url_prefix="/admin/claims")


def create_audit_log(
    module,
    action,
    admin_id=None,
    user_id=None,
    entity_type=None,
    entity_id=None,
    reason=None,
    description=None,
    old_value=None,
    new_value=None,
):
    log = AuditLog(
        module=module,
        action=action,
        admin_id=admin_id,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        reason=reason,
        description=description,
        old_value=old_value,
        new_value=new_value,
    )
    db.session.add(log)


def _to_decimal(x, default="0"):
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


@admin_claims_bp.route(
    "/",
    methods=["GET"],
)
@admin_required
def queue():
    allowed_statuses = {
        "all",
        "submitted",
        "under_review",
        "need_more_info",
        "approved",
        "rejected",
        "paid",
    }

    status = (
        request.args.get(
            "status",
            "submitted",
        )
        or "submitted"
    ).strip().lower()

    if status not in allowed_statuses:
        status = "submitted"

    search = (
        request.args.get("search") or ""
    ).strip()

    date_filter = (
        request.args.get("date") or ""
    ).strip()

    page = request.args.get(
        "page",
        1,
        type=int,
    )

    per_page = request.args.get(
        "per_page",
        10,
        type=int,
    )

    if page < 1:
        page = 1

    if per_page not in {
        10,
        25,
        50,
        100,
    }:
        per_page = 10

    query = Claim.query.join(
        Claim.user
    )

    if status != "all":
        query = query.filter(
            Claim.status == status
        )

    if search:
        like = f"%{search}%"

        query = query.filter(
            or_(
                Claim.case_id.ilike(like),
                Claim.house_awb.ilike(like),
                Claim.tracking_number.ilike(like),
                Claim.status.ilike(like),

                Claim.user.has(
                    User.full_name.ilike(like)
                ),

                Claim.user.has(
                    User.email.ilike(like)
                ),

                Claim.user.has(
                    User.registration_number.ilike(
                        like
                    )
                ),
            )
        )

    # ---------------------------------
    # Jamaica business-date filtering
    # ---------------------------------
    jamaica_timezone = ZoneInfo(
        "America/Jamaica"
    )

    today_jamaica = datetime.now(
        jamaica_timezone
    ).date()

    start_date = None
    end_date = None

    if date_filter == "today":
        start_date = today_jamaica
        end_date = (
            today_jamaica
            + timedelta(days=1)
        )

    elif date_filter == "7_days":
        start_date = (
            today_jamaica
            - timedelta(days=6)
        )

        end_date = (
            today_jamaica
            + timedelta(days=1)
        )

    elif date_filter == "30_days":
        start_date = (
            today_jamaica
            - timedelta(days=29)
        )

        end_date = (
            today_jamaica
            + timedelta(days=1)
        )

    if start_date and end_date:
        start_jamaica = datetime.combine(
            start_date,
            datetime.min.time(),
            tzinfo=jamaica_timezone,
        )

        end_jamaica = datetime.combine(
            end_date,
            datetime.min.time(),
            tzinfo=jamaica_timezone,
        )

        start_utc = start_jamaica.astimezone(
            timezone.utc
        )

        end_utc = end_jamaica.astimezone(
            timezone.utc
        )

        query = query.filter(
            Claim.created_at >= start_utc,
            Claim.created_at < end_utc,
        )

    pagination = (
        query
        .order_by(
            Claim.created_at.desc()
        )
        .paginate(
            page=page,
            per_page=per_page,
            error_out=False,
        )
    )

    claims = pagination.items

    counts = {
        "all": Claim.query.count(),

        "submitted": (
            Claim.query
            .filter_by(status="submitted")
            .count()
        ),

        "under_review": (
            Claim.query
            .filter_by(status="under_review")
            .count()
        ),

        "need_more_info": (
            Claim.query
            .filter_by(
                status="need_more_info"
            )
            .count()
        ),

        "approved": (
            Claim.query
            .filter_by(status="approved")
            .count()
        ),

        "rejected": (
            Claim.query
            .filter_by(status="rejected")
            .count()
        ),

        "paid": (
            Claim.query
            .filter_by(status="paid")
            .count()
        ),
    }

    claim_customers = (
        User.query
        .filter(
            User.is_admin.isnot(True),
            User.is_enabled.is_(True),
        )
        .order_by(
            User.full_name.asc(),
            User.email.asc(),
        )
        .all()
    )

    return render_template(
        "admin/claims/queue.html",
        claims=claims,
        status=status,
        counts=counts,
        search=search,
        date_filter=date_filter,
        page=page,
        per_page=per_page,
        total_pages=max(
            pagination.pages or 1,
            1,
        ),
        total_results=pagination.total,
        start_index=(
            ((page - 1) * per_page) + 1
            if pagination.total
            else 0
        ),
        end_index=min(
            page * per_page,
            pagination.total,
        ),
        claim_customers=claim_customers,
    )

@admin_claims_bp.route(
    "/customers/<int:user_id>/eligible-packages",
    methods=["GET"],
)
@admin_required
def eligible_packages(user_id):
    user = db.session.get(
        User,
        user_id,
    )

    if not user:
        return jsonify({
            "success": False,
            "error": "Customer not found.",
        }), 404

    packages = get_eligible_claim_packages(
        user.id
    )

    return jsonify({
        "success": True,
        "customer": {
            "id": user.id,
            "name": (
                user.full_name
                or user.email
                or f"Customer #{user.id}"
            ),
            "registration_number": (
                user.registration_number
                or ""
            ),
        },
        "packages": [
            {
                "id": package.id,
                "house_awb": (
                    package.house_awb or ""
                ),
                "tracking_number": (
                    package.tracking_number
                    or ""
                ),
                "description": (
                    package.description or ""
                ),
                "status": (
                    package.status or ""
                ),
            }
            for package in packages
        ],
    })

@admin_claims_bp.route("/<int:claim_id>", methods=["GET", "POST"])
@admin_required
def review(claim_id):
    claim = Claim.query.get_or_404(claim_id)
    form = AdminClaimDecisionForm()

    if request.method == "GET":
        form.status.data = claim.status or "submitted"
        form.approved_amount_jmd.data = claim.approved_amount_jmd
        form.decision_reason.data = claim.decision_reason
        form.admin_notes.data = claim.admin_notes

        form.refund_issued.data = bool(getattr(claim, "refund_issued", False))
        form.refund_issued_method.data = getattr(claim, "refund_issued_method", "") or ""
        form.refunded_amount_jmd.data = getattr(claim, "refunded_amount_jmd", None)
        form.refund_reference.data = getattr(claim, "refund_reference", None)

    if form.validate_on_submit():
        old_status = claim.status or "submitted"
        new_status = form.status.data

        old_approved_amount = claim.approved_amount_jmd
        old_decision_reason = claim.decision_reason
        old_admin_notes = claim.admin_notes

        claim.status = new_status

        if form.approved_amount_jmd.data is not None:
            claim.approved_amount_jmd = form.approved_amount_jmd.data

        claim.decision_reason = (form.decision_reason.data or "").strip() or None
        claim.admin_notes = (form.admin_notes.data or "").strip() or None

        claim.reviewed_by_admin_id = current_user.id
        claim.reviewed_at = claim.reviewed_at or datetime.now(timezone.utc)

        wants_refund_issued = bool(form.refund_issued.data)

        if wants_refund_issued:
            method = (form.refund_issued_method.data or "").strip()

            if not method:
                flash("Select 'Refund Method Issued' before marking refund as issued.", "warning")
                return redirect(url_for("admin_claims.review", claim_id=claim.id))

            refunded_amount = form.refunded_amount_jmd.data

            if refunded_amount is None:
                refunded_amount = claim.approved_amount_jmd or claim.item_value_jmd or 0

            refunded_amount_dec = _to_decimal(refunded_amount)

            claim.refund_issued = True
            claim.refund_issued_method = method
            claim.refunded_amount_jmd = refunded_amount_dec
            claim.refund_reference = (form.refund_reference.data or "").strip() or None
            claim.refund_issued_at = datetime.now(timezone.utc)
            claim.refund_issued_by_admin_id = current_user.id

            existing_refund_payment = (
                Payment.query
                .filter(
                    Payment.user_id == claim.user_id,
                    Payment.claim_id == claim.id,
                    Payment.transaction_type == "package_refund",
                    Payment.status == "completed"
                )
                .first()
            )

            if not existing_refund_payment:
                refund_payment = Payment(
                    user_id=claim.user_id,
                    invoice_id=None,
                    claim_id=claim.id,
                    method=method,
                    amount_jmd=float(refunded_amount_dec),
                    reference=(form.refund_reference.data or "").strip() or None,
                    notes=(
                        f"Refund issued for claim {claim.case_id or ('#' + str(claim.id))} - "
                        f"{(claim.user.full_name or 'Unknown Customer').strip()}"
                        + (
                            f" ({(claim.user.registration_number or '').strip()})"
                            if (claim.user.registration_number or '').strip()
                            else ""
                        )
                    ),
                    transaction_type="package_refund",
                    status="completed",
                    source="admin",
                    authorized_by_admin_id=current_user.id,
                    created_at=datetime.now(timezone.utc)
                )
                db.session.add(refund_payment)

            if claim.status != "rejected":
                claim.status = "paid"

            if getattr(claim, "package_id", None):
                linked_pkg = claim.package
                if linked_pkg:
                    linked_pkg.status = "Claim Refunded"

            elif claim.house_awb or claim.tracking_number:
                from app.models import Package

                filters = [Package.user_id == claim.user_id]
                matchers = []

                if claim.house_awb:
                    matchers.append(Package.house_awb == claim.house_awb)

                if claim.tracking_number:
                    matchers.append(Package.tracking_number == claim.tracking_number)

                if matchers:
                    linked_pkg = (
                        Package.query
                        .filter(*filters)
                        .filter(or_(*matchers))
                        .order_by(Package.id.desc())
                        .first()
                    )

                    if linked_pkg:
                        linked_pkg.status = "Claim Refunded"

            if method == "wallet_credit":
                wallet = Wallet.query.filter_by(user_id=claim.user_id).first()

                if not wallet:
                    wallet = Wallet(
                        user_id=claim.user_id,
                        ewallet_balance=0.0
                    )
                    db.session.add(wallet)
                    db.session.flush()

                wallet.ewallet_balance = (
                    float(wallet.ewallet_balance or 0) + float(refunded_amount_dec)
                )

                db.session.add(
                    WalletTransaction(
                        user_id=claim.user_id,
                        amount=float(refunded_amount_dec),
                        description=f"Claim refund credited ({claim.case_id or ('Claim #' + str(claim.id))})",
                        type="credit",
                        action="credit",
                        reason="Claim Refund",
                        invoice_number=None,
                        admin_id=current_user.id,
                        created_at=datetime.now(timezone.utc)
                    )
                )

            create_audit_log(
                module="Claims",
                action="Claim Refund Issued",
                admin_id=current_user.id,
                user_id=claim.user_id,
                entity_type="Claim",
                entity_id=claim.id,
                reason=method,
                description=(
                    f"Refund issued for claim {claim.case_id or ('#' + str(claim.id))}. "
                    f"Method: {method}. Amount: JMD {float(refunded_amount_dec):,.2f}."
                ),
                old_value="Refund Pending",
                new_value=f"Refund Issued - JMD {float(refunded_amount_dec):,.2f}",
            )

        claim_detail_changes = []

        if str(old_approved_amount or "") != str(claim.approved_amount_jmd or ""):
            claim_detail_changes.append(
                f"Approved Amount: {old_approved_amount or 0} → {claim.approved_amount_jmd or 0}"
            )

        if str(old_decision_reason or "") != str(claim.decision_reason or ""):
            claim_detail_changes.append("Decision Reason changed")

        if str(old_admin_notes or "") != str(claim.admin_notes or ""):
            claim_detail_changes.append("Admin Notes changed")

        if claim_detail_changes:
            create_audit_log(
                module="Claims",
                action="Claim Details Updated",
                admin_id=current_user.id,
                user_id=claim.user_id,
                entity_type="Claim",
                entity_id=claim.id,
                reason=claim.decision_reason or "Claim details update",
                description=(
                    f"Claim {claim.case_id or ('#' + str(claim.id))} details updated. "
                    + "; ".join(claim_detail_changes)
                ),
                old_value=(
                    f"Status: {old_status}; "
                    f"Approved Amount: {old_approved_amount or 0}; "
                    f"Decision Reason: {old_decision_reason or '—'}; "
                    f"Admin Notes: {old_admin_notes or '—'}"
                ),
                new_value=(
                    f"Status: {claim.status}; "
                    f"Approved Amount: {claim.approved_amount_jmd or 0}; "
                    f"Decision Reason: {claim.decision_reason or '—'}; "
                    f"Admin Notes: {claim.admin_notes or '—'}"
                ),
            )

        if old_status != claim.status:
            claim_action_label = (
                f"Claim {str(claim.status or '').replace('_', ' ').title()}"
            )

            create_audit_log(
                module="Claims",
                action=claim_action_label,
                admin_id=current_user.id,
                user_id=claim.user_id,
                entity_type="Claim",
                entity_id=claim.id,
                reason=claim.decision_reason or "Claim review",
                description=(
                    f"Claim {claim.case_id or ('#' + str(claim.id))} "
                    f"changed from {old_status} to {claim.status}."
                ),
                old_value=old_status,
                new_value=claim.status,
            )

            db.session.add(
                ClaimAuditLog(
                    claim_id=claim.id,
                    action="status_changed",
                    from_status=old_status,
                    to_status=claim.status,
                    actor_admin_id=current_user.id,
                    message=(
                        f"Admin updated claim "
                        f"{claim.case_id or ('#' + str(claim.id))}: "
                        f"{old_status} → {claim.status}"
                    )
                )
            )

        db.session.commit()

        try:
            send_claim_status_update_email(
                user_email=claim.user.email,
                full_name=claim.user.full_name,
                claim=claim,
                recipient_user_id=claim.user_id
            )
        except Exception:
            pass

        if Message is not None:
            try:
                title = f"Claim Update: {claim.case_id or ('Claim #' + str(claim.id))}"
                body = f"Your claim status has been updated to '{claim.status}'."

                if claim.decision_reason:
                    body += f"\n\nReason: {claim.decision_reason}"

                msg = Message(
                    sender_id=current_user.id,
                    recipient_id=claim.user_id,
                    subject=title,
                    body=body,
                    is_read=False,
                    created_at=datetime.now(timezone.utc)
                )

                db.session.add(msg)
                db.session.commit()

            except Exception:
                db.session.rollback()

        flash("Claim updated.", "success")

        return redirect(url_for("admin_claims.review", claim_id=claim.id))

    logs = claim.audit_logs.order_by(
        ClaimAuditLog.created_at.desc()
    ).all()

    return render_template(
        "admin/claims/review_claim.html",
        claim=claim,
        form=form,
        logs=logs
    )