from flask import jsonify, request
from . import api_bp
from .utils import jwt_required

@api_bp.get("/me")
@jwt_required
def api_me():
    u = request.current_user
    return jsonify({
        "ok": True,
        "user": {
            "id": u.id,
            "email": u.email,
            "full_name": getattr(u, "full_name", None),
            "role": getattr(u, "role", "customer"),
            "registration_number": getattr(u, "registration_number", None),
        }
    })
