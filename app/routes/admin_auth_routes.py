from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash
import bcrypt
from functools import wraps

from app.models import User
from app.forms import AdminLoginForm

admin_auth_bp = Blueprint("admin_auth", __name__, url_prefix="/admin_auth")

# ---------- Single admin_required ----------
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in as admin to access this page.", "danger")
            return redirect(url_for("admin_auth.admin_login"))
        if (getattr(current_user, "role", None) != "admin") and not getattr(current_user, "is_admin", False):
            flash("Unauthorized access.", "danger")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated_function


def _check_password(user, plain: str) -> bool:
    """
    Accept bcrypt (bytes or str), Werkzeug password_hash, or plain text (legacy).
    """
    # 1) If the model has a helper, prefer it.
    if hasattr(user, "check_password"):
        try:
            return bool(user.check_password(plain))
        except Exception:
            pass

    # Pull potential fields
    pw_attr = getattr(user, "password", None)
    ph_attr = getattr(user, "password_hash", None)

    # 2) bcrypt stored in .password (bytes or str)
    try:
        if isinstance(pw_attr, (bytes, bytearray)) and pw_attr.startswith(b"$2"):
            return bcrypt.checkpw(plain.encode("utf-8"), pw_attr)
        if isinstance(pw_attr, str) and pw_attr.startswith("$2"):
            return bcrypt.checkpw(plain.encode("utf-8"), pw_attr.encode("utf-8"))
    except Exception:
        pass

    # 3) Werkzeug-style hash in password_hash or password
    try:
        if isinstance(ph_attr, str) and "$" in ph_attr:
            return check_password_hash(ph_attr, plain)
        if isinstance(pw_attr, str) and "$" in pw_attr:
            return check_password_hash(pw_attr, plain)
    except Exception:
        pass

    # 4) Last-resort plain-text compare (legacy)
    try:
        if isinstance(pw_attr, str) and "$" not in pw_attr:
            return pw_attr == plain
    except Exception:
        pass

    return False


# ---------- Admin Login ----------
@admin_auth_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    from sqlalchemy.orm import load_only
    from app.extensions import db

    # already logged in?
    if current_user.is_authenticated and getattr(current_user, 'role', None) == 'admin':
        return redirect(url_for('admin.dashboard'))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        raw_pw = (form.password.data or "").encode('utf-8')

        try:
            # Only load the columns we NEED to avoid any weird column/type issues
            admin = (
                User.query.options(load_only(User.id, User.email, User.password, User.role))
                .filter(User.email == email, User.role == 'admin')
                .first()
            )

            if not admin or not admin.password:
                flash("Invalid email or password.", "danger")
                return render_template('auth/admin_login.html', form=form)

            stored = admin.password
            # allow either bytes or str in DB
            if isinstance(stored, str):
                stored = stored.encode('utf-8')

            if bcrypt.checkpw(raw_pw, stored):
                # IMPORTANT: we must pass the actual model instance to login_user
                login_user(admin, remember=False)
                session['admin_id'] = admin.id
                session['role'] = 'admin'
                return redirect(url_for('admin.dashboard'))

            flash("Invalid email or password.", "danger")
        except Exception as e:
            # TEMP: surface the real error so we can see it
            # (Replace with logging only after we fix it)
            flash(f"Login error: {e}", "danger")

    return render_template('auth/admin_login.html', form=form)

# ---------- Admin Logout ----------
@admin_auth_bp.route("/logout")
@admin_required
def admin_logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin_auth.admin_login"))
