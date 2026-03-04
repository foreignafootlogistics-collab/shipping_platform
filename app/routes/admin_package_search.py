from flask import Blueprint, render_template, request
from app.routes.admin_auth_routes import admin_required
from app.models import PackageSearchCase
from app.extensions import db

admin_search_bp = Blueprint("admin_search", __name__, url_prefix="/admin/search")

@admin_search_bp.route("/", methods=["GET"])
@admin_required
def queue():
    status = (request.args.get("status") or "submitted").strip()

    unread_search_count = PackageSearchCase.query.filter(
        PackageSearchCase.status == "submitted",
        PackageSearchCase.is_read == False
    ).count()

    q = PackageSearchCase.query
    if status != "all":
        q = q.filter(PackageSearchCase.status == status)

    cases = q.order_by(PackageSearchCase.created_at.desc()).all()

    return render_template(
        "admin/package_search/queue.html",
        cases=cases,
        unread_search_count=unread_search_count,
        status=request.args.get("status", "submitted"),
    )

@admin_search_bp.route("/<int:case_id>", methods=["GET"])
@admin_required
def review(case_id):
    case = PackageSearchCase.query.get_or_404(case_id)

    if not case.is_read:
        case.is_read = True
        db.session.commit()

    return render_template("admin/package_search/review.html", case=case, form=form, logs=logs)