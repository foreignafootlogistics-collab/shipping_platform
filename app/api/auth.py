from flask import request, jsonify
from datetime import datetime, timedelta
import os, jwt, bcrypt

from app.models import User
from app.extensions import db
from . import api_bp

JWT_SECRET = os.getenv("JWT_SECRET", os.getenv("SECRET_KEY", "dev_secret"))
JWT_EXP_MIN = int(os.getenv("JWT_EXP_MIN", "60"))

def make_token(user_id: int):
    payload = {
        "sub": user_id,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(minutes=JWT_EXP_MIN),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

@api_bp.post("/auth/login")
def api_login():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password_plain = data.get("password") or ""

    if not email or not password_plain:
        return jsonify({"ok": False, "error": "Email and password are required."}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.password:
        return jsonify({"ok": False, "error": "Invalid email or password."}), 401

    stored_password = user.password
    if isinstance(stored_password, memoryview):
        stored_password = stored_password.tobytes()
    if isinstance(stored_password, str):
        stored_password = stored_password.encode("utf-8")

    try:
        ok = bcrypt.checkpw(password_plain.encode("utf-8"), stored_password)
    except Exception:
        ok = False

    if not ok:
        return jsonify({"ok": False, "error": "Invalid email or password."}), 401

    # optional: update last_login like your web route
    try:
        user.last_login = datetime.utcnow()
        db.session.commit()
    except Exception:
        db.session.rollback()

    token = make_token(user.id)

    return jsonify({
        "ok": True,
        "token": token,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": getattr(user, "full_name", None),
            "role": getattr(user, "role", "customer"),
            "registration_number": getattr(user, "registration_number", None),
        }
    })
