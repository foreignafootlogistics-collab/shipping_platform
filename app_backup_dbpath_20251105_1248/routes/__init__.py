from .admin_routes import admin_bp
from .customer_routes import customer_bp
from .auth_routes import auth_bp
from .accounts_profiles_routes import accounts_bp

__all__ = ['admin_bp', 'customer_bp', 'auth_bp', 'accounts_bp']