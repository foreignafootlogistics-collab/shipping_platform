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
    if current_user.is_authenticated and getattr(current_user, 'role', None) == 'admin':
        return redirect(url_for('admin.dashboard'))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        raw_password = (form.password.data or "")
        password_bytes = raw_password.encode("utf-8")

        admin = User.query.filter_by(email=email, role='admin').first()

        def _ok(msg):
            current_app.logger.info(f"[ADMIN LOGIN] {msg}")

        def _warn(msg):
            current_app.logger.warning(f"[ADMIN LOGIN] {msg}")

        if not admin:
            _warn(f"no admin for email={email}")
            flash("Invalid email or password.", "danger")
            return render_template('auth/admin_login.html', form=form)

        verified = False

        try:
            # 1) bcrypt bytes in `password`
            if not verified and hasattr(admin, "password") and admin.password:
                stored = admin.password
                if isinstance(stored, str):
                    stored = stored.encode("utf-8")
                try:
                    import bcrypt
                    if bcrypt.checkpw(password_bytes, stored):
                        verified = True
                        _ok("bcrypt ok via admin.password")
                except Exception as e:
                    _warn(f"bcrypt check failed: {e}")

            # 2) Werkzeug hash in `password_hash`
            if not verified and hasattr(admin, "password_hash") and admin.password_hash:
                try:
                    from werkzeug.security import check_password_hash
                    if check_password_hash(admin.password_hash, raw_password):
                        verified = True
                        _ok("werkzeug ok via admin.password_hash")
                except Exception as e:
                    _warn(f"werkzeug check failed: {e}")

            # 3) Plain text fallback (rare, legacy)
            if not verified and hasattr(admin, "password") and isinstance(admin.password, str):
                if admin.password == raw_password:
                    verified = True
                    _ok("plain-text fallback matched (legacy!)")

        except Exception as e:
            _warn(f"unexpected error while verifying password: {e}")

        if verified:
            login_user(admin)
            session['admin_id'] = admin.id
            session['role'] = 'admin'
            flash("Admin login successful!", "success")
            return redirect(url_for('admin.dashboard'))

        flash("Invalid email or password.", "danger")

    return render_template('auth/admin_login.html', form=form)

# ---------- Admin Logout ----------
@admin_auth_bp.route("/logout")
@admin_required
def admin_logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("admin_auth.admin_login"))
