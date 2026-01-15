import os, jwt
from functools import wraps
from flask import request, jsonify
from app.models import User

JWT_SECRET = os.getenv("JWT_SECRET", os.getenv("SECRET_KEY", "dev_secret"))

def jwt_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"ok": False, "error": "Missing token"}), 401
        token = auth.replace("Bearer ", "").strip()
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            user = User.query.get(payload["sub"])
            if not user:
                return jsonify({"ok": False, "error": "User not found"}), 401
            request.current_user = user
        except jwt.ExpiredSignatureError:
            return jsonify({"ok": False, "error": "Token expired"}), 401
        except Exception:
            return jsonify({"ok": False, "error": "Invalid token"}), 401
        return fn(*args, **kwargs)
    return wrapper
