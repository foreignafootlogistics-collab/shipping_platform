from datetime import datetime
import secrets

from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Claim, ClaimAuditLog
from app.forms import ClaimForm
from app.utils.claims_uploads import upload_claim_file_to_cloudinary
from app.utils.email_utils import send_claim_submitted_email
from app.models import next_counter_value, generate_claim_case_id

# ✅ If you already have to_jamaica in app.utils.time, use it
try:
    from app.utils.time import to_jamaica
except Exception:
    to_jamaica = None

customer_claims_bp = Blueprint("customer_claims", __name__, url_prefix="/customer/claims")


@customer_claims_bp.route("/", methods=["GET"])
@login_required
def list_claims():
    claims = (Claim.query
              .filter(Claim.user_id == current_user.id)
              .order_by(Claim.created_at.desc())
              .all())
    return render_template("customer/claims/list_claims.html", claims=claims)


@customer_claims_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_claim():
    form = ClaimForm()

    if form.validate_on_submit():
        try:
            # ✅ Upload evidence files
            invoice_url, invoice_public_id = upload_claim_file_to_cloudinary(
                form.invoice_file.data, folder="fafl/claims/invoices"
            )
            bank_url, bank_public_id = upload_claim_file_to_cloudinary(
                form.bank_statement_file.data, folder="fafl/claims/bank_statements"
            )

            seq = next_counter_value("claims_case_seq")
            case_id = generate_claim_case_id(seq, now=datetime.now(timezone.utc))

            # ✅ Build claim object
            claim = Claim(
                user_id=current_user.id,
                case_id=case_id,  # ✅ NEW: case id here
                house_awb=(form.house_awb.data or "").strip(),
                tracking_number=((form.tracking_number.data or "").strip() or None),
                item_value_jmd=form.item_value_jmd.data,
                description=((form.description.data or "").strip() or None),

                invoice_url=invoice_url,
                invoice_public_id=invoice_public_id,
                bank_statement_url=bank_url,
                bank_statement_public_id=bank_public_id,

                refund_method=form.refund_method.data,
                bank_account_name=((form.bank_account_name.data or "").strip() or None),
                bank_branch=((form.bank_branch.data or "").strip() or None),
                bank_account_number=((form.bank_account_number.data or "").strip() or None),
                bank_account_type=((form.bank_account_type.data or "").strip() or None),

                status="submitted",
                created_at=datetime.now(timezone.utc),
            )

            db.session.add(claim)
            db.session.flush()  # ensures claim.id exists

            # ✅ Audit log
            db.session.add(ClaimAuditLog(
                claim_id=claim.id,
                action="created",
                actor_user_id=current_user.id,
                message=f"Customer submitted a claim. Case ID: {claim.case_id}"
            ))

            db.session.commit()

            # ✅ Email (now includes claim.case_id if your template uses it)
            send_claim_submitted_email(
                user_email=current_user.email,
                full_name=current_user.full_name,
                claim=claim,
                recipient_user_id=current_user.id
            )

            flash("Claim submitted. Claims are processed within 5–10 business days after investigation.", "success")
            return redirect(url_for("customer_claims.view_claim", claim_id=claim.id))

        except Exception as e:
            db.session.rollback()
            flash(f"Could not submit claim: {e}", "danger")

    return render_template("customer/claims/create_claim.html", form=form)


@customer_claims_bp.route("/<int:claim_id>", methods=["GET"])
@login_required
def view_claim(claim_id):
    claim = Claim.query.filter_by(id=claim_id, user_id=current_user.id).first_or_404()
    logs = claim.audit_logs.order_by(ClaimAuditLog.created_at.desc()).all()
    return render_template("customer/claims/view_claim.html", claim=claim, logs=logs)