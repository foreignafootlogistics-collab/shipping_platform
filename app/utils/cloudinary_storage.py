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
    Does NOT print secrets.
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

    for ext in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".xlsx", ".xls"):
        if parts.query.lower().endswith(ext):
            new_query = parts.query[: -len(ext)]
            new_path = (parts.path or "") + ext
            return urlunsplit(
                (parts.scheme, parts.netloc, new_path, new_query, parts.fragment)
            )

    return u


# ---------------------------------------
# Generic Upload (Images + PDF + Excel)
# ---------------------------------------
def upload_file(file_storage, folder="fafl/uploads"):
    """
    Upload any supported file type to Cloudinary.
    Returns: (url, public_id, resource_type)

    Important:
    - Images use Cloudinary resource_type="image".
    - PDFs/Excel/other docs use resource_type="raw".
    - For raw uploads, DO NOT manually append .pdf/.xlsx to Cloudinary secure_url.
      Cloudinary raw URLs often work without the extension and can 404 if mutated.
    """
    if not file_storage:
        return None, None, None

    filename = secure_filename(file_storage.filename or "")
    ext = os.path.splitext(filename)[1].lower()
    base = os.path.splitext(filename)[0]

    is_image = ext in (".jpg", ".jpeg", ".png", ".webp")
    resource_type = "image" if is_image else "raw"

    clean_base = secure_filename(base) or "file"
    unique = uuid.uuid4().hex[:10]
    public_id = f"{folder}/{clean_base}_{unique}"

    result = cloudinary.uploader.upload(
        file_storage,
        resource_type=resource_type,
        type="upload",
        access_mode="public",
        public_id=public_id,
        use_filename=False,
        unique_filename=False,
        overwrite=False,
    )

    # Keep Cloudinary's returned URL exactly.
    # Do NOT append file extensions here. That caused raw PDF URLs to 404.
    url = result.get("secure_url") or result.get("url")

    try:
        current_app.logger.warning(
            "[CLOUD UPLOAD] folder=%s filename=%s ext=%s resource_type=%s saved_url=%s public_id=%s format=%s",
            folder,
            filename,
            ext,
            resource_type,
            url,
            result.get("public_id"),
            result.get("format"),
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


# Backward compat: old code expects URL only
def upload_invoice_image(file_storage):
    url, _, _ = upload_file(file_storage, folder="fafl/invoices")
    return url


# New: for columns file_url, cloud_public_id, cloud_resource_type
def upload_invoice_image_meta(file_storage):
    return upload_file(file_storage, folder="fafl/invoices")


# ---------------------------------------
# Serve Prealert Invoice
# ---------------------------------------
def serve_prealert_invoice_file(pa, *, download_name_prefix="prealert", as_attachment=False):
    """
    Serve a Prealert invoice inline or as download.

    Remote URL:
    - Try to stream through Flask so filename/content type can be controlled.
    - If Cloudinary returns 401/403, fallback to redirect.
    """
    u = (getattr(pa, "invoice_filename", "") or "").strip()
    if not u:
        current_app.logger.warning(
            "[PREALERT INVOICE] Missing invoice_filename pa_id=%s",
            getattr(pa, "id", None),
        )
        abort(404)

    num = getattr(pa, "prealert_number", None) or getattr(pa, "id", None)
    safe_name = secure_filename(f"{download_name_prefix}_{num}_invoice") or "prealert_invoice"

    if u.startswith("//"):
        u = "https:" + u
    elif "cloudinary.com" in u and not u.startswith(("http://", "https://")):
        u = "https://" + u

    if u.startswith(("http://", "https://")):
        u = _fix_bad_ext_url(u)
        current_app.logger.warning("[PREALERT INVOICE] Fetch remote url=%s", u)

        try:
            r = requests.get(u, stream=True, timeout=30)
        except Exception as e:
            current_app.logger.exception("[PREALERT INVOICE] requests.get failed: %s", e)
            abort(502)

        if r.status_code in (401, 403):
            current_app.logger.warning(
                "[PREALERT INVOICE] Remote status=%s -> redirect fallback url=%s",
                r.status_code,
                u,
            )
            return redirect(u)

        if r.status_code != 200:
            current_app.logger.warning(
                "[PREALERT INVOICE] Remote status=%s url=%s",
                r.status_code,
                u,
            )
            abort(404)

        it = r.iter_content(chunk_size=8192)
        first = next(it, b"") or b""

        def sniff(first_bytes: bytes):
            fb = first_bytes[:8]

            if fb.startswith(b"%PDF-"):
                return "application/pdf", ".pdf"

            if fb.startswith(b"\xFF\xD8\xFF"):
                return "image/jpeg", ".jpg"

            if fb.startswith(b"\x89PNG\r\n\x1a\n"):
                return "image/png", ".png"

            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            ct = ct or "application/octet-stream"

            if ct == "application/pdf":
                return ct, ".pdf"
            if ct == "image/jpeg":
                return ct, ".jpg"
            if ct == "image/png":
                return ct, ".png"

            return ct, ""

        content_type, ext = sniff(first)

        if ext and not safe_name.lower().endswith(ext):
            safe_name = safe_name + ext

        def generate():
            if first:
                yield first
            for chunk in it:
                if chunk:
                    yield chunk

        disp = "attachment" if as_attachment else "inline"

        resp = Response(stream_with_context(generate()), mimetype=content_type)
        resp.headers["Content-Type"] = content_type
        resp.headers["Content-Disposition"] = f'{disp}; filename="{safe_name}"'
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    upload_folder = current_app.config.get("INVOICE_UPLOAD_FOLDER")
    if not upload_folder:
        abort(500)

    fp = os.path.join(upload_folder, u)
    if not os.path.exists(fp):
        abort(404)

    guessed_type, _ = mimetypes.guess_type(fp)

    resp = send_from_directory(
        upload_folder,
        u,
        as_attachment=as_attachment,
        download_name=safe_name,
    )

    if guessed_type:
        resp.headers["Content-Type"] = guessed_type

    disp = "attachment" if as_attachment else "inline"
    resp.headers["Content-Disposition"] = f'{disp}; filename="{safe_name}"'
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