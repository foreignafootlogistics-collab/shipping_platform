# app/utils/cloudinary_storage.py

import os
from werkzeug.utils import secure_filename
from urllib.parse import urlsplit, urlunsplit
import cloudinary
import cloudinary.uploader



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

def _ensure_extension_in_url(url: str | None, ext: str, resource_type: str) -> str | None:
    """
    Cloudinary 'raw' URLs sometimes come back without extension.
    Add the extension to the PATH (before ?query/#fragment) so browsers preview inline.
    """
    if not url:
        return url

    ext = (ext or "").lower().strip()
    if ext and not ext.startswith("."):
        ext = f".{ext}"

    # Only needed for raw uploads (pdf/xlsx/etc)
    if resource_type != "raw" or not ext:
        return url

    parts = urlsplit(url)
    path = parts.path or ""

    # If the path already ends with .pdf/.xlsx etc, don't add again
    if path.lower().endswith(ext):
        return url

    # Add extension to the PATH only (NOT after querystring)
    new_path = path + ext
    return urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, parts.fragment))

def _fix_bad_ext_url(u: str) -> str:
    """
    Fix URLs like: .../file?x=1.pdf  -> .../file.pdf?x=1
    Only applies if it looks like a bad append.
    """
    if not u:
        return u

    parts = urlsplit(u)
    if not parts.query:
        return u  # no query, nothing to fix

    # if query ends with ".pdf" etc, it's likely wrong
    for ext in (".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls"):
        if parts.query.lower().endswith(ext):
            # remove ext from query and append to path
            new_query = parts.query[: -len(ext)]
            new_path = (parts.path or "") + ext
            return urlunsplit((parts.scheme, parts.netloc, new_path, new_query, parts.fragment))

    return u



# ---------------------------------------
# Generic Upload (Images + PDF + Excel)
# ---------------------------------------
def upload_file(file_storage, folder="fafl/uploads"):
    """
    Uploads ANY supported file type to Cloudinary.
    Returns: (url, public_id, resource_type)

    - images -> resource_type=image
    - pdf/xls/xlsx/etc -> resource_type=raw
    - ensures extension is present in URL for raw files (important for inline viewing)
    """
    if not file_storage:
        return None, None, None

    filename = secure_filename(file_storage.filename or "")
    ext = os.path.splitext(filename)[1].lower()  # ".pdf", ".jpg", etc

    resource_type = "image" if ext in (".jpg", ".jpeg", ".png") else "raw"

    result = cloudinary.uploader.upload(
        file_storage,
        folder=folder,
        resource_type=resource_type,
        type="upload",
        access_mode="public",
        use_filename=True,
        unique_filename=True,
        overwrite=False,
    )

    url = result.get("secure_url") or result.get("url")
    url = _ensure_extension_in_url(url, ext, resource_type)

    return (
        url,
        result.get("public_id"),
        result.get("resource_type"),
    )


# ---------------------------------------
# Specific wrappers
# ---------------------------------------
def upload_prealert_invoice(file_storage):
    # Returns full metadata
    return upload_file(file_storage, folder="fafl/prealerts")


def upload_package_attachment(file_storage):
    # Returns full metadata
    return upload_file(file_storage, folder="fafl/package_attachments")


# ✅ BACKWARD COMPAT: old code expects URL only
def upload_invoice_image(file_storage):
    url, _, _ = upload_file(file_storage, folder="fafl/invoices")
    return url


# ✅ NEW: for your PackageAttachment columns (file_url, cloud_public_id, cloud_resource_type)
def upload_invoice_image_meta(file_storage):
    return upload_file(file_storage, folder="fafl/invoices")


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
