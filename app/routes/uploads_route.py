from flask import Blueprint, current_app, send_from_directory, abort
from werkzeug.utils import safe_join

from app.routes.admin_auth_routes import admin_required  # ✅ use your existing decorator

uploads_bp = Blueprint("uploads", __name__)  # no url_prefix

@uploads_bp.route("/uploads/invoices/<path:filename>")
@admin_required()
def invoice_file(filename):
    folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not folder:
        abort(404)

    safe_path = safe_join(folder, filename)
    if not safe_path:
        abort(404)

    return send_from_directory(folder, filename)
