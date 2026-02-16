# app/utils/cloudinary_storage.py

import cloudinary
import cloudinary.uploader
from werkzeug.utils import secure_filename
import os


def init_cloudinary(app):
    cloud_name = app.config.get("CLOUDINARY_CLOUD_NAME")
    api_key = app.config.get("CLOUDINARY_API_KEY")
    api_secret = app.config.get("CLOUDINARY_API_SECRET")

    # Return False if anything missing (no printing secrets)
    if not (cloud_name and api_key and api_secret):
        return False

    cloudinary.config(
        cloud_name=cloud_name,
        api_key=api_key,
        api_secret=api_secret,
        secure=True,
    )
    return True


# ---------------------------------------
# Generic Upload (Images + PDF + Excel)
# ---------------------------------------
def upload_file(file_storage, folder="fafl/uploads"):
    """
    Uploads ANY supported file type to Cloudinary.
    Returns: secure_url, public_id, resource_type
    """

    if not file_storage:
        return None, None, None

    filename = secure_filename(file_storage.filename or "")
    ext = os.path.splitext(filename)[1].lower()

    # image files → image, everything else (pdf/xls/xlsx) → raw
    resource_type = "image" if ext in [".jpg", ".jpeg", ".png"] else "raw"

    result = cloudinary.uploader.upload(
        file_storage,
        folder=folder,
        resource_type=resource_type,

        # ✅ IMPORTANT: make it publicly accessible (prevents 401)
        type="upload",
        access_mode="public",

        use_filename=True,
        unique_filename=True,
        overwrite=False,
    )

    return (
        result.get("secure_url") or result.get("url"),
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

