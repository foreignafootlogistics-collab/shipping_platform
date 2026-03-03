from datetime import datetime
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import current_user

from app.extensions import db
from app.models import Claim, ClaimAuditLog, DBMessage, Wallet, WalletTransaction
from app.forms import AdminClaimDecisionForm
from app.routes.admin_auth_routes import admin_required
from app.utils.email_utils import send_claim_status_update_email

admin_claims_bp = Blueprint("admin_claims", __name__, url_prefix="/admin/claims")

@admin_claims_bp.route("/", methods=["GET"])
@admin_required
def queue():
    status = request.args.get("status", "submitted")
    q = Claim.query

    if status and status != "all":
        q = q.filter(Claim.status == status)

    claims = q.order_by(Claim.created_at.desc()).all()
    return render_template("admin/claims/queue.html", claims=claims, status=status)

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

        form.refund_issued.data = bool(claim.refund_issued)
        form.refund_issued_method.data = claim.refund_issued_method or ""
        form.refunded_amount_jmd.data = claim.refunded_amount_jmd
        form.refund_reference.data = claim.refund_reference

    if form.validate_on_submit():
        old_status = claim.status or "submitted"
        new_status = form.status.data

        claim.status = new_status
        claim.approved_amount_jmd = form.approved_amount_jmd.data if form.approved_amount_jmd.data is not None else claim.approved_amount_jmd
        claim.decision_reason = (form.decision_reason.data or "").strip() or None
        claim.admin_notes = (form.admin_notes.data or "").strip() or None
        claim.reviewed_by_admin_id = current_user.id
        claim.reviewed_at = claim.reviewed_at or datetime.utcnow()

        # --- refund issued handling ---
        wants_refund_issued = bool(form.refund_issued.data)

        if wants_refund_issued:
            # require method when issuing refund
            method = (form.refund_issued_method.data or "").strip()
            if not method:
                flash("Select 'Refund Method Issued' before marking refund as issued.", "warning")
                return redirect(url_for("admin_claims.review", claim_id=claim.id))

            # default refunded amount: approved amount if available, else claimed value
            refunded_amount = form.refunded_amount_jmd.data
            if refunded_amount is None:
                refunded_amount = claim.approved_amount_jmd or claim.item_value_jmd

            # Set flags
            claim.refund_issued = True
            claim.refund_issued_method = method
            claim.refunded_amount_jmd = refunded_amount
            claim.refund_reference = (form.refund_reference.data or "").strip() or None
            claim.refund_issued_at = datetime.utcnow()
            claim.refund_issued_by_admin_id = current_user.id

            # recommended: if refund issued, force status paid (unless rejected)
            if claim.status != "rejected":
                claim.status = "paid"

            # Wallet credit option
            if method == "wallet_credit":
                wallet = Wallet.query.filter_by(user_id=claim.user_id).first()
                if not wallet:
                    wallet = Wallet(user_id=claim.user_id, balance=0)
                    db.session.add(wallet)
                    db.session.flush()

                # update wallet balance (assumes numeric/decimal)
                wallet.balance = (wallet.balance or 0) + refunded_amount

                db.session.add(WalletTransaction(
                    wallet_id=wallet.id,
                    amount=refunded_amount,
                    txn_type="credit",
                    description=f"Claim refund credited (Claim #{claim.id})",
                    created_at=datetime.utcnow()
                ))

        # audit log
        db.session.add(ClaimAuditLog(
            claim_id=claim.id,
            action="status_changed",
            from_status=old_status,
            to_status=claim.status,
            actor_admin_id=current_user.id,
            message=f"Admin updated claim: {old_status} → {claim.status}"
        ))

        db.session.commit()

        # notify customer (email + DBMessage)
        send_claim_status_update_email(
            user_email=claim.user.email,
            full_name=claim.user.full_name,
            claim=claim,
            recipient_user_id=claim.user_id
        )

        flash("Claim updated.", "success")
        return redirect(url_for("admin_claims.review", claim_id=claim.id))

    logs = claim.audit_logs.order_by(ClaimAuditLog.created_at.desc()).all()
    return render_template("admin/claims/review_claim.html", claim=claim, form=form, logs=logs)