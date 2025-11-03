import sqlite3
from app.config import DB_PATH
from .registration import next_registration_number
from .wallet import apply_referral_bonus
from .wallet import update_wallet

ALLOWED_EXTENSIONS = {'xlsx'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS