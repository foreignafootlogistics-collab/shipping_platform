from functools import wraps
from flask import session, redirect, url_for, flash
from flask_login import current_user

# ---------- Admin decorator ----------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Check if user is logged in and is admin
        if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
            flash("You need admin access to view this page.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

# ---------- Customer decorator ----------
def customer_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or getattr(current_user, "is_admin", False):
            flash("Access denied. Customer only area.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function
