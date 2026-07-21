import uuid

from werkzeug.utils import secure_filename


ALLOWED_EXTENSIONS = {
    "pdf",
    "jpg",
    "jpeg",
    "png",
    "webp",
}


def allowed_file(filename: str) -> bool:
    """
    Return True when the filename has an allowed extension.
    """
    if not filename or "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()

    return extension in ALLOWED_EXTENSIONS


def upload_claim_file_to_cloudinary(
    file_storage,
    folder: str,
):
    """
    Upload claim evidence to Cloudinary.

    Returns:
        tuple[str, str]: secure URL and Cloudinary public ID
    """
    if not file_storage:
        raise ValueError("Evidence file is required.")

    filename = secure_filename(
        file_storage.filename or ""
    )

    if not filename:
        raise ValueError(
            "The selected evidence file has no filename."
        )

    if not allowed_file(filename):
        raise ValueError(
            "Only PDF, JPG, JPEG, PNG, and WEBP files are allowed."
        )

    name_without_extension = filename.rsplit(
        ".",
        1,
    )[0]

    public_id = (
        f"{uuid.uuid4().hex}_"
        f"{name_without_extension}"
    )

    import cloudinary.uploader

    result = cloudinary.uploader.upload(
        file_storage,
        folder=folder,
        public_id=public_id,
        resource_type="auto",
    )

    secure_url = result.get("secure_url")
    uploaded_public_id = result.get("public_id")

    if not secure_url or not uploaded_public_id:
        raise ValueError(
            "Cloudinary did not return the uploaded file details."
        )

    return secure_url, uploaded_public_id