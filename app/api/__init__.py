from flask import Blueprint

api_bp = Blueprint("mobile_api", __name__, url_prefix="/api")

from . import auth, me, utils  # import submodules

