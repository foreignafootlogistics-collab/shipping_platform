# app/utils/files.py
import os

ALLOWED_EXTENSIONS = {
    "pdf",
    "png", "jpg", "jpeg",
    "xls", "xlsx",
    "webp"
}

IMAGE_EXTENSIONS = {"png", "jpg", "jpeg"}
RAW_EXTENSIONS = {"pdf", "xls", "xlsx"}

def get_ext(filename: str) -> str:
    return (os.path.splitext(filename or "")[1].lower().lstrip("."))

def allowed_file(filename: str) -> bool:
    ext = get_ext(filename)
    return ext in ALLOWED_EXTENSIONS

