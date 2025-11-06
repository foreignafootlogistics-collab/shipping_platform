# app/routes/admin_auth_routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.orm import load_only
from app.models import User
from app.forms import AdminLoginForm
from functools import wraps
import bcrypt
from werkzeug.security import check_password_hash

admin_auth_bp = Blueprint('admin_auth', __name__, url_prefix='/admin_auth')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in as admin to access this page.", "danger")
            return redirect(url_for('admin_auth.admin_login'))
        if getattr(current_user, 'role', None) != 'admin':
            flash("Unauthorized access.", "danger")
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

def _verify_password(stored_pw, provided_plain: str) -> bool:
    """
    Accepts either:
    - bcrypt bytes or bcrypt string ($2a/$2b/$2y)
    - werkzeug pbkdf2 string ("pbkdf2:sha256:...")
    """
    if stored_pw is None:
        return False

    # Normalize memoryview → bytes
    if isinstance(stored_pw, memoryview):
        stored_pw = stored_pw.tobytes()

    # If DB holds bytes (bcrypt)
    if isinstance(stored_pw, bytes):
        try:
            return bcrypt.checkpw(provided_plain.encode("utf-8"), stored_pw)
        except Exception:
            return False

    # If DB holds string
    if isinstance(stored_pw, str):
        # bcrypt hash stored as text?
        if stored_pw.startswith(("$2a$", "$2b$", "$2y$")):
            try:
                return bcrypt.checkpw(provided_plain.encode("utf-8"), stored_pw.encode("utf-8"))
            except Exception:
                return False
        # otherwise treat as Werkzeug pbkdf2 hash
        try:
            return check_password_hash(stored_pw, provided_plain)
        except Exception:
            return False

    # Unknown type
    return False

@admin_auth_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    # Already logged in?
    if current_user.is_authenticated and getattr(current_user, 'role', None) == 'admin':
        return redirect(url_for('admin.dashboard'))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        password_plain = form.password.data or ""

        try:
            admin = (
                User.query.options(load_only(User.id, User.email, User.password, User.role))
                .filter(User.email == email, User.role == 'admin')
                .first()
            )

            if not admin or not admin.password:
                flash("Invalid email or password.", "danger")
                return render_template('auth/admin_login.html', form=form)

            if _verify_password(admin.password, password_plain):
                login_user(admin, remember=False)  # pass the model instance
                session['admin_id'] = admin.id
                session['role'] = 'admin'
                return redirect(url_for('admin.dashboard'))

            flash("Invalid email or password.", "danger")
        except Exception as e:
            # Temporary: surface the real error to help us finish the migration
            flash(f"Login error: {e}", "danger")

    return render_template('auth/admin_login.html', form=form)

@admin_auth_bp.route('/logout')
@admin_required
def admin_logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin_auth.admin_login'))
