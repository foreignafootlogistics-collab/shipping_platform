from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user
from app.extensions import db
from app.forms import PackageSearchForm
from app.models import PackageSearchCase, Package, generate_search_case_id, normalize_tracking
from app.utils.claims_uploads import upload_claim_file_to_cloudinary
from app.utils.email_utils import send_package_search_submitted_email

customer_search_bp = Blueprint("customer_search", __name__, url_prefix="/customer/search")

@customer_search_bp.route("/new", methods=["GET", "POST"])
@login_required
def create_search_case():
    form = PackageSearchForm()

    if form.validate_on_submit():
        try:
            tn = normalize_tracking(form.tracking_number.data)

            # ✅ 1) Check warehouse first
            existing_pkg = Package.query.filter(Package.tracking_number == tn).first()
            if existing_pkg:
                flash(
                    f"✅ We found this tracking number already in our system (Package #{existing_pkg.id}). "
                    "Please check your Packages page. If you still need help, contact support.",
                    "warning"
                )
                return redirect(url_for("customer.view_packages"))

            # ✅ 2) Prevent duplicate open search requests
            existing_case = (
                PackageSearchCase.query
                .filter(
                    PackageSearchCase.user_id == current_user.id,
                    PackageSearchCase.tracking_number == tn,
                    PackageSearchCase.status.in_(["submitted", "searching", "found"])
                )
                .first()
            )
            if existing_case:
                flash(
                    f"⚠️ You already submitted a search request for this tracking number "
                    f"({existing_case.case_id}). Please wait for an update.",
                    "info"
                )
                return redirect(url_for("customer_search.list_cases"))

            # upload proof
            proof_url, proof_public_id = upload_claim_file_to_cloudinary(
                form.proof_file.data, folder="fafl/search_cases/proof"
            )

            case = PackageSearchCase(
                case_id=generate_search_case_id(),
                user_id=current_user.id,
                tracking_number=tn,
                delivered_date=form.delivered_date.data,
                proof_url=proof_url,
                proof_public_id=proof_public_id,
                notes=(form.notes.data or "").strip() or None,
                status="submitted",
            )

            db.session.add(case)
            db.session.commit()

            send_package_search_submitted_email(case=case)

            flash("Search request submitted. Our overseas team will investigate.", "success")
            return redirect(url_for("customer_search.list_cases"))

        except Exception as e:
            db.session.rollback()
            flash(f"Could not submit search request: {e}", "danger")

    return render_template("customer/package_search/new.html", form=form)

@customer_search_bp.route("/", methods=["GET"])
@login_required
def list_cases():
    cases = (PackageSearchCase.query
             .filter(PackageSearchCase.user_id == current_user.id)
             .order_by(PackageSearchCase.created_at.desc())
             .all())
    return render_template("customer/package_search/list.html", cases=cases)


@customer_search_bp.route("/<int:case_id>", methods=["GET"])
@login_required
def view_case(case_id):
    case = (PackageSearchCase.query
            .filter(PackageSearchCase.id == case_id,
                    PackageSearchCase.user_id == current_user.id)
            .first_or_404())
    return render_template("customer/package_search/view.html", case=case)