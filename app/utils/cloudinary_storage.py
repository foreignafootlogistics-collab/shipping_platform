# app/utils/cloudinary_storage.py

import os
from werkzeug.utils import secure_filename

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
    Cloudinary 'raw' URLs often come back without the file extension.
    Adding the extension helps browsers preview PDFs inline instead of forcing download.
    """
    if not url:
        return url

    ext = (ext or "").lower()
    if not ext.startswith("."):
        ext = f".{ext}" if ext else ""

    if resource_type == "raw" and ext:
        # If it already ends with .pdf/.xlsx etc, don't double-append
        if not url.lower().endswith(ext):
            url = url + ext

    return url


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
# Specific wrappers (optional but clean)
# ---------------------------------------
def upload_prealert_invoice(file_storage):
    return upload_file(file_storage, folder="fafl/prealerts")


def upload_package_attachment(file_storage):
    return upload_file(file_storage, folder="fafl/package_attachments")


def upload_invoice_image(file_storage):
    # keeps backward compatibility with your current code
    url, _, _ = upload_file(file_storage, folder="fafl/invoices")
    return url


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

