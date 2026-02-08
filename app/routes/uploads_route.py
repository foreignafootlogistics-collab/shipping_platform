from flask import Blueprint, current_app, send_from_directory, abort
import os
from werkzeug.utils import safe_join

from app.routes.admin_auth_routes import admin_required  # ✅ use your existing decorator

uploads_bp = Blueprint("uploads", __name__)  # no url_prefix

@uploads_bp.route("/uploads/invoices/<path:filename>")
def invoice_file(filename):
    folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not folder:
        abort(404)

    # basic safety: don't allow weird traversal
    filename = filename.replace("\\", "/")
    if ".." in filename or filename.startswith("/"):
        abort(400)

    full_path = os.path.join(folder, filename)
    if not os.path.isfile(full_path):
        abort(404)

    return send_from_directory(folder, filename)