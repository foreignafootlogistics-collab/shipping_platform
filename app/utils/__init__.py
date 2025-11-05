# app/utils/__init__.py
# DB-agnostic helpers (no direct sqlite imports or DB file paths)

from .registration import next_registration_number
from .wallet import apply_referral_bonus, update_wallet

ALLOWED_EXTENSIONS = {'xlsx'}

def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS
