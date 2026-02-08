import cloudinary
import cloudinary.uploader

def init_cloudinary(app):
    """
    Configure Cloudinary using environment variables loaded into app.config
    """
    cloudinary.config(
        cloud_name=app.config.get("CLOUDINARY_CLOUD_NAME"),
        api_key=app.config.get("CLOUDINARY_API_KEY"),
        api_secret=app.config.get("CLOUDINARY_API_SECRET"),
        secure=True,
    )

def upload_invoice_image(file_storage, public_id=None) -> str:
    """
    Upload a Flask/Werkzeug FileStorage to Cloudinary and return the secure URL.
    """
    res = cloudinary.uploader.upload(
        file_storage,
        folder="fafl/invoices",
        public_id=public_id,
        overwrite=True,
        resource_type="image",
    )
    return res["secure_url"]
