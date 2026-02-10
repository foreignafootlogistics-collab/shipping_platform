from flask import Blueprint, current_app, send_from_directory, abort
import os
from werkzeug.utils import safe_join

from app.routes.admin_auth_routes import admin_required  # ✅ use your existing decorator

uploads_bp = Blueprint("uploads", __name__)  # no url_prefix

@uploads_bp.route("/uploads/invoices/<path:filename>")
def invoice_file(filename):
    folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not folder:
        current_app.logger.error("INVOICE_UPLOAD_FOLDER not configured")
        abort(404)

    # normalize
    filename = filename.replace("\\", "/")

    # basic safety
    if ".." in filename or filename.startswith("/"):
        abort(400)

    # safer path join
    try:
        full_path = safe_join(folder, filename)
    except Exception:
        abort(400)

    if not os.path.isfile(full_path):
        current_app.logger.warning(f"Invoice not found on disk: {full_path}")
        abort(404)

    return send_from_directory(folder, filename, as_attachment=False)
