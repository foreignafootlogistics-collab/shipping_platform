from datetime import datetime, timezone
from decimal import Decimal

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import current_user
from sqlalchemy import or_

from app.extensions import db
from app.models import Claim, ClaimAuditLog, Wallet, WalletTransaction, Payment
from app.forms import AdminClaimDecisionForm
from app.routes.admin_auth_routes import admin_required
from app.utils.email_utils import send_claim_status_update_email

# Optional in-app messaging
try:
    from app.models import Message
except Exception:
    Message = None


admin_claims_bp = Blueprint("admin_claims", __name__, url_prefix="/admin/claims")


def _to_decimal(x, default="0"):
    if x is None:
        return Decimal(default)
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


# -----------------------------------------------------------
# CLAIM QUEUE
# -----------------------------------------------------------

@admin_claims_bp.route("/", methods=["GET"])
@admin_required
def queue():

    status = request.args.get("status", "submitted")

    q = Claim.query

    if status and status != "all":
        q = q.filter(Claim.status == status)

    claims = q.order_by(Claim.created_at.desc()).all()

    return render_template(
        "admin/claims/queue.html",
        claims=claims,
        status=status
    )


# -----------------------------------------------------------
# CLAIM REVIEW
# -----------------------------------------------------------

@admin_claims_bp.route("/<int:claim_id>", methods=["GET", "POST"])
@admin_required
def review(claim_id):

    claim = Claim.query.get_or_404(claim_id)
    form = AdminClaimDecisionForm()

    # -------------------------------------------------------
    # PREFILL FORM
    # -------------------------------------------------------

    if request.method == "GET":

        form.status.data = claim.status or "submitted"
        form.approved_amount_jmd.data = claim.approved_amount_jmd
        form.decision_reason.data = claim.decision_reason
        form.admin_notes.data = claim.admin_notes

        form.refund_issued.data = bool(getattr(claim, "refund_issued", False))
        form.refund_issued_method.data = getattr(claim, "refund_issued_method", "") or ""
        form.refunded_amount_jmd.data = getattr(claim, "refunded_amount_jmd", None)
        form.refund_reference.data = getattr(claim, "refund_reference", None)

    # -------------------------------------------------------
    # PROCESS FORM
    # -------------------------------------------------------

    if form.validate_on_submit():

        old_status = claim.status or "submitted"
        new_status = form.status.data

        claim.status = new_status

        if form.approved_amount_jmd.data is not None:
            claim.approved_amount_jmd = form.approved_amount_jmd.data

        claim.decision_reason = (form.decision_reason.data or "").strip() or None
        claim.admin_notes = (form.admin_notes.data or "").strip() or None

        claim.reviewed_by_admin_id = current_user.id
        claim.reviewed_at = claim.reviewed_at or datetime.now(timezone.utc)

        # ---------------------------------------------------
        # REFUND PROCESSING
        # ---------------------------------------------------

        wants_refund_issued = bool(form.refund_issued.data)

        if wants_refund_issued:

            method = (form.refund_issued_method.data or "").strip()

            if not method:
                flash(
                    "Select 'Refund Method Issued' before marking refund as issued.",
                    "warning"
                )
                return redirect(
                    url_for("admin_claims.review", claim_id=claim.id)
                )

            refunded_amount = form.refunded_amount_jmd.data

            if refunded_amount is None:
                refunded_amount = claim.approved_amount_jmd or claim.item_value_jmd or 0

            refunded_amount_dec = _to_decimal(refunded_amount)

            claim.refund_issued = True
            claim.refund_issued_method = method
            claim.refunded_amount_jmd = refunded_amount_dec

            claim.refund_reference = (
                (form.refund_reference.data or "").strip() or None
            )

            claim.refund_issued_at = datetime.now(timezone.utc)
            claim.refund_issued_by_admin_id = current_user.id

            # ------------------------------------------------
            # CREATE PAYMENT RECORD
            # ------------------------------------------------

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
                    notes=f"Refund issued for claim {claim.case_id or ('#' + str(claim.id))}",
                    transaction_type="package_refund",
                    status="completed",
                    source="admin",
                    authorized_by_admin_id=current_user.id,
                    created_at=datetime.now(timezone.utc)
                )

                db.session.add(refund_payment)

            # ------------------------------------------------
            # UPDATE CLAIM STATUS
            # ------------------------------------------------

            if claim.status != "rejected":
                claim.status = "paid"

            # ------------------------------------------------
            # MARK LINKED PACKAGE AS CLAIM REFUNDED
            # ------------------------------------------------
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

            # ------------------------------------------------
            # WALLET CREDIT OPTION
            # ------------------------------------------------

            if method == "wallet_credit":

                wallet = Wallet.query.filter_by(user_id=claim.user_id).first()

                if not wallet:
                    wallet = Wallet(
                        user_id=claim.user_id,
                        ewallet_balance=0.0
                    )
                    db.session.add(wallet)
                    db.session.flush()

                wallet.ewallet_balance = float(wallet.ewallet_balance or 0) + float(refunded_amount_dec)

                db.session.add(
                    WalletTransaction(
                        user_id=claim.user_id,
                        amount=float(refunded_amount_dec),
                        description=f"Claim refund credited ({claim.case_id or ('Claim #' + str(claim.id))})",
                        type="credit",
                        created_at=datetime.now(timezone.utc)
                    )
                )

        # ---------------------------------------------------
        # AUDIT LOG
        # ---------------------------------------------------

        db.session.add(
            ClaimAuditLog(
                claim_id=claim.id,
                action="status_changed",
                from_status=old_status,
                to_status=claim.status,
                actor_admin_id=current_user.id,
                message=f"Admin updated claim {claim.case_id or ('#' + str(claim.id))}: {old_status} → {claim.status}"
            )
        )

        db.session.commit()

        # ---------------------------------------------------
        # EMAIL NOTIFICATION
        # ---------------------------------------------------

        try:

            send_claim_status_update_email(
                user_email=claim.user.email,
                full_name=claim.user.full_name,
                claim=claim,
                recipient_user_id=claim.user_id
            )

        except Exception:
            pass

        # ---------------------------------------------------
        # IN-APP MESSAGE
        # ---------------------------------------------------

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

        return redirect(
            url_for("admin_claims.review", claim_id=claim.id)
        )

    logs = claim.audit_logs.order_by(
        ClaimAuditLog.created_at.desc()
    ).all()

    return render_template(
        "admin/claims/review_claim.html",
        claim=claim,
        form=form,
        logs=logs
    )

