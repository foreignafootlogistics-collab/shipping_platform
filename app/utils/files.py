# app/utils/files.py

# Example: Excel uploads only
EXCEL_ALLOWED_EXTENSIONS = {"xlsx"}

def allowed_file(filename: str, allowed: set[str] = EXCEL_ALLOWED_EXTENSIONS) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in allowed
