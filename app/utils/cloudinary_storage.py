# app/utils/cloudinary_storage.py
import cloudinary
import cloudinary.uploader

def init_cloudinary(app):
    cloudinary.config(
        cloud_name=app.config.get("CLOUDINARY_CLOUD_NAME"),
        api_key=app.config.get("CLOUDINARY_API_KEY"),
        api_secret=app.config.get("CLOUDINARY_API_SECRET"),
        secure=True,
    )

def upload_invoice_image(file_storage, public_id=None) -> str:
    res = cloudinary.uploader.upload(
        file_storage,
        folder="fafl/invoices",
        public_id=public_id,
        overwrite=True,
        resource_type="auto",  # ✅ supports pdf/jpg/png
    )
    return res["secure_url"]
