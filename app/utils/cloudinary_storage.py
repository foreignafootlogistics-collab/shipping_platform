# app/utils/cloudinary_storage.py

import os
import uuid
import mimetypes
import requests
from urllib.parse import urlsplit, urlunsplit

import cloudinary
import cloudinary.uploader

from werkzeug.utils import secure_filename
from flask import (
    current_app, abort, Response, stream_with_context,
    send_from_directory, redirect
)


def init_cloudinary(app) -> bool:
    """
    Configure Cloudinary from app.config.
    Returns True if all required values exist, otherwise False.
    (Does NOT print secrets)
    """
    cloud_name = app.config.get("CLOUDINARY_CLOUD_NAME")
    api_key = app.config.get("CLOUDINARY_API_KEY")
    api_secret = app.config.get("CLOUDINARY_API_SECRET")

    if not (cloud_name and api_key and api_secret):
        return False

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return True


# -----------------------------
# URL helpers
# -----------------------------
def _fix_bad_ext_url(u: str) -> str:
    """
    Fix URLs like:
      .../file?x=1.pdf  -> .../file.pdf?x=1
    """
    if not u:
        return u

    parts = urlsplit(u)
    if not parts.query:
        return u

    for ext in (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls"):
        if parts.query.lower().endswith(ext):
            new_query = parts.query[: -len(ext)]
            new_path = (parts.path or "") + ext
            return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, parts.fragment))

    return u


def _ensure_extension_in_url(url: str | None, ext: str, resource_type: str) -> str | None:
    """
    ⚠️ NOTE:
    We do NOT use this for Cloudinary secure_url anymore because mutating Cloudinary URLs
    can lead to 404s for raw uploads. Kept only for backwards compat if something else references it.
    """
    if not url:
        return url

    ext = (ext or "").lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    if resource_type != "raw" or not ext:
        return url

    parts = urlsplit(url)
    path = parts.path or ""
    if path.lower().endswith(ext):
        return url

    new_path = path + ext
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))


# ---------------------------------------
# Generic Upload (Images + PDF + Excel)
# ---------------------------------------
def upload_file(file_storage, folder="fafl/uploads"):
    """
    Upload ANY supported file type to Cloudinary.
    Returns: (url, public_id, resource_type)

    ✅ Fix: for RAW uploads (pdf/xls/xlsx), ensure public_id has NO extension.
    """
    if not file_storage:
        return None, None, None

    filename = secure_filename(file_storage.filename or "")
    ext = os.path.splitext(filename)[1].lower()  # ".pdf", ".jpg", etc
    base = os.path.splitext(filename)[0]         # "invoice_jl18nf" (no ext)

    is_image = ext in (".jpg", ".jpeg", ".png")
    resource_type = "image" if is_image else "raw"

    # ✅ Critical: public_id must NOT include extension
    # Keep folder structure, add unique suffix to prevent collisions
    clean_base = secure_filename(base) or "file"
    unique = uuid.uuid4().hex[:10]
    public_id = f"{folder}/{clean_base}_{unique}"

    result = cloudinary.uploader.upload(
        file_storage,
        resource_type=resource_type,
        type="upload",
        access_mode="public",

        # ✅ we control the public_id ourselves
        public_id=public_id,
        use_filename=False,
        unique_filename=False,
        overwrite=False,
    )

    url = result.get("secure_url") or result.get("url")

    # Debug (Render logs)
    try:
        from flask import current_app
        current_app.logger.warning(
            "[CLOUD UPLOAD] folder=%s filename=%s ext=%s resource_type=%s secure_url=%s public_id=%s format=%s",
            folder, filename, ext, resource_type,
            result.get("secure_url"), result.get("public_id"), result.get("format")
        )
    except Exception:
        pass

    return url, result.get("public_id"), result.get("resource_type")


# ---------------------------------------
# Specific wrappers
# ---------------------------------------
def upload_prealert_invoice(file_storage):
    return upload_file(file_storage, folder="fafl/prealerts")


def upload_package_attachment(file_storage):
    return upload_file(file_storage, folder="fafl/package_attachments")


# ✅ BACKWARD COMPAT: old code expects URL only
def upload_invoice_image(file_storage):
    url, _, _ = upload_file(file_storage, folder="fafl/invoices")
    return url


# ✅ NEW: for your PackageAttachment columns (file_url, cloud_public_id, cloud_resource_type)
def upload_invoice_image_meta(file_storage):
    return upload_file(file_storage, folder="fafl/invoices")


# ---------------------------------------
# Serve Prealert Invoice (inline)
# ---------------------------------------
def serve_prealert_invoice_file(pa, *, download_name_prefix="prealert"):
    """
    Serve a Prealert invoice inline.
    - If invoice_filename is a remote URL (Cloudinary), try streaming.
      If Cloudinary returns non-200, fallback to redirect (browser fetches directly).
    - Else, serve from local INVOICE_UPLOAD_FOLDER.
    """
    u = (getattr(pa, "invoice_filename", "") or "").strip()
    if not u:
        current_app.logger.warning(
            "[PREALERT INVOICE] Missing invoice_filename for pa_id=%s",
            getattr(pa, "id", None)
        )
        abort(404)

    num = getattr(pa, "prealert_number", None) or getattr(pa, "id", None)
    safe_name = secure_filename(f"{download_name_prefix}_{num}_invoice") or "prealert_invoice"

    # Normalize Cloudinary shorthand
    if u.startswith("//"):
        u = "https:" + u
    elif "cloudinary.com" in u and not u.startswith(("http://", "https://")):
        u = "https://" + u

    # -------------------------
    # Remote URL (Cloudinary)
    # -------------------------
    if u.startswith(("http://", "https://")):
        u = _fix_bad_ext_url(u)

        current_app.logger.warning("[PREALERT INVOICE] Fetch remote url=%s", u)

        try:
            r = requests.get(u, stream=True, timeout=30, allow_redirects=True)
        except Exception as e:
            current_app.logger.exception("[PREALERT INVOICE] requests failed: %s", e)
            return redirect(u)

        if r.status_code != 200:
            current_app.logger.warning(
                "[PREALERT INVOICE] Remote status=%s -> redirect fallback. url=%s",
                r.status_code, u
            )
            return redirect(u)

        lower = u.lower()
        if lower.endswith(".pdf"):
            content_type = "application/pdf"
            if not safe_name.lower().endswith(".pdf"):
                safe_name += ".pdf"
        elif lower.endswith((".jpg", ".jpeg")):
            content_type = "image/jpeg"
            if not safe_name.lower().endswith((".jpg", ".jpeg")):
                safe_name += ".jpg"
        elif lower.endswith(".png"):
            content_type = "image/png"
            if not safe_name.lower().endswith(".png"):
                safe_name += ".png"
        else:
            content_type = r.headers.get("Content-Type") or "application/octet-stream"

        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk

        resp = Response(stream_with_context(generate()), mimetype=content_type)
        resp.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    # -------------------------
    # Local disk fallback
    # -------------------------
    upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not upload_folder:
        abort(500)

    fp = os.path.join(upload_folder, u)
    if not os.path.exists(fp):
        abort(404)

    guessed_type, _ = mimetypes.guess_type(fp)
    resp = send_from_directory(upload_folder, u, as_attachment=False)
    if guessed_type:
        resp.headers["Content-Type"] = guessed_type

    resp.headers["Content-Disposition"] = f'inline; filename="{safe_name}"'
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ---------------------------------------
# Delete from Cloudinary
# ---------------------------------------
def delete_cloudinary_file(public_id, resource_type="raw"):
    if not public_id:
        return False
    try:
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        return True
    except Exception:
        return False

