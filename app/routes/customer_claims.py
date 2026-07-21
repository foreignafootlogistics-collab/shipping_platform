from datetime import datetime, timezone
import secrets

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Claim, ClaimAuditLog, Package
from app.forms import ClaimForm
from app.utils.claims_uploads import upload_claim_file_to_cloudinary
from app.utils.email_utils import send_claim_submitted_email
from app.utils.claims import (
    get_eligible_claim_package,
    get_eligible_claim_packages,
)
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


@customer_claims_bp.route(
    "/new",
    methods=["GET", "POST"],
)
@login_required
def create_claim():
    form = ClaimForm()

    if form.validate_on_submit():
        try:
            # ---------------------------------
            # Validate the selected package
            # ---------------------------------
            try:
                package_id = int(
                    request.form.get("package_id") or 0
                )
            except (TypeError, ValueError):
                package_id = 0

            selected_package = get_eligible_claim_package(
                user_id=current_user.id,
                package_id=package_id,
            )

            if not selected_package:
                flash(
                    "Select an eligible package belonging "
                    "to your account.",
                    "danger",
                )

                return redirect(
                    url_for(
                        "customer_claims.create_claim"
                    )
                )

            # Use identifiers stored on the package.
            # Do not trust editable browser fields.
            house_awb = (
                selected_package.house_awb or ""
            ).strip()

            tracking_number = (
                selected_package.tracking_number or ""
            ).strip()

            if not house_awb:
                flash(
                    "The selected package does not have "
                    "a House AWB. Please contact FAFL.",
                    "danger",
                )

                return redirect(
                    url_for(
                        "customer_claims.create_claim"
                    )
                )

            if not tracking_number:
                flash(
                    "The selected package does not have "
                    "a tracking number. Please contact FAFL.",
                    "danger",
                )

                return redirect(
                    url_for(
                        "customer_claims.create_claim"
                    )
                )

            # ---------------------------------
            # Upload required evidence
            # ---------------------------------
            invoice_url, invoice_public_id = (
                upload_claim_file_to_cloudinary(
                    form.invoice_file.data,
                    folder="fafl/claims/invoices",
                )
            )

            (
                bank_statement_url,
                bank_statement_public_id,
            ) = upload_claim_file_to_cloudinary(
                form.bank_statement_file.data,
                folder=(
                    "fafl/claims/bank_statements"
                ),
            )

            # ---------------------------------
            # Create the claim
            # ---------------------------------
            claim = Claim(
                user_id=current_user.id,
                package_id=selected_package.id,
                case_id=generate_claim_case_id(),

                house_awb=house_awb,
                tracking_number=tracking_number,

                item_value_jmd=(
                    form.item_value_jmd.data
                ),

                description=(
                    (
                        form.description.data
                        or ""
                    ).strip()
                    or None
                ),

                invoice_url=invoice_url,
                invoice_public_id=(
                    invoice_public_id
                ),

                bank_statement_url=(
                    bank_statement_url
                ),
                bank_statement_public_id=(
                    bank_statement_public_id
                ),

                refund_method=(
                    form.refund_method.data
                ),

                bank_account_name=(
                    (
                        form.bank_account_name.data
                        or ""
                    ).strip()
                    or None
                ),

                bank_branch=(
                    (
                        form.bank_branch.data
                        or ""
                    ).strip()
                    or None
                ),

                bank_account_number=(
                    (
                        form.bank_account_number.data
                        or ""
                    ).strip()
                    or None
                ),

                bank_account_type=(
                    (
                        form.bank_account_type.data
                        or ""
                    ).strip()
                    or None
                ),

                status="submitted",
                created_at=datetime.now(
                    timezone.utc
                ),
                updated_at=datetime.now(
                    timezone.utc
                ),
            )

            db.session.add(claim)
            db.session.flush()

            db.session.add(
                ClaimAuditLog(
                    claim_id=claim.id,
                    action="created",
                    from_status=None,
                    to_status="submitted",
                    actor_user_id=current_user.id,
                    message=(
                        "Customer submitted claim "
                        f"{claim.case_id} for package "
                        f"#{selected_package.id}."
                    ),
                )
            )

            db.session.commit()

            # Email failure must not undo a claim that
            # has already been successfully recorded.
            try:
                send_claim_submitted_email(
                    user_email=current_user.email,
                    full_name=current_user.full_name,
                    claim=claim,
                    recipient_user_id=(
                        current_user.id
                    ),
                )

            except Exception as email_error:
                current_app.logger.exception(
                    "Claim %s was created, but its "
                    "confirmation email failed: %s",
                    claim.case_id,
                    email_error,
                )

            flash(
                "Claim submitted successfully. "
                "Investigations normally take 5–10 "
                "business days after review begins.",
                "success",
            )

            return redirect(
                url_for(
                    "customer_claims.view_claim",
                    claim_id=claim.id,
                )
            )

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "Customer claim submission failed "
                "for user %s",
                current_user.id,
            )

            flash(
                f"Could not submit claim: {error}",
                "danger",
            )

    eligible_packages = (
        get_eligible_claim_packages(
            current_user.id
        )
    )

    return render_template(
        "customer/claims/create_claim.html",
        form=form,
        eligible_packages=eligible_packages,
    )

@customer_claims_bp.route("/<int:claim_id>", methods=["GET"])
@login_required
def view_claim(claim_id):
    claim = Claim.query.filter_by(id=claim_id, user_id=current_user.id).first_or_404()
    logs = claim.audit_logs.order_by(ClaimAuditLog.created_at.desc()).all()
    return render_template("customer/claims/view_claim.html", claim=claim, logs=logs)