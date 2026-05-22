from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask import request


db = SQLAlchemy()
csrf = CSRFProtect()


def get_client_ip():
    return (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


limiter = Limiter(
    key_func=get_client_ip,
    default_limits=[],
    storage_uri="memory://",
)