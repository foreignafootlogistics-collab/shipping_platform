# app/utils/claims_uploads.py
import uuid
from werkzeug.utils import secure_filename

ALLOWED_EXTENSIONS = {"pdf", "jpg", "jpeg", "png"}

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def upload_claim_file_to_cloudinary(file_storage, folder: str):
    """
    Returns: (url, public_id)
    """
    import cloudinary.uploader

    filename = secure_filename(file_storage.filename or "")
    if not filename:
        raise ValueError("Missing filename")

    if not allowed_file(filename):
        raise ValueError("Only PDF/JPG/PNG allowed")

    public_id = f"{uuid.uuid4().hex}_{filename.rsplit('.', 1)[0]}"
    res = cloudinary.uploader.upload(
        file_storage,
        folder=folder,
        public_id=public_id,
        resource_type="auto",
    )
    return (res.get("secure_url"), res.get("public_id"))