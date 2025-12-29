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

def require_role(*roles):
    """
    Allow access if current_user.role is in roles
    OR user.is_superadmin is True.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            # 1) Must be logged in
            if not current_user.is_authenticated:
                return redirect(url_for('admin_auth.admin_login'))

            # 2) Superadmin can do everything
            if getattr(current_user, "is_superadmin", False):
                return view_func(*args, **kwargs)

            # 3) Check role
            user_role = getattr(current_user, "role", None)
            if user_role not in roles:
                flash("You do not have permission to access this section.", "danger")
                return redirect(url_for('admin.dashboard'))

            return view_func(*args, **kwargs)
        return wrapped_view
    return decorator
 
def admin_required(view_func=None, roles=None):
    """
    Usage examples:
      @admin_required
      def some_view(): ...

      @admin_required()
      def some_view(): ...

      @admin_required(roles=['finance', 'operations'])
      def finance_view(): ...
    """

    def decorator(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            # 1) Must be logged in
            if not current_user.is_authenticated:
                flash("Please log in as admin to access this page.", "danger")
                return redirect(url_for('admin_auth.admin_login'))

            # 2) Must be an admin-type account
            if not getattr(current_user, "is_admin", False):
                flash("Unauthorized access.", "danger")
                return redirect(url_for('auth.login'))

            # 3) Optional role check
            if roles:
                # Superadmin always allowed
                if getattr(current_user, "is_superadmin", False):
                    return fn(*args, **kwargs)

                user_role = (getattr(current_user, "role", "") or "").lower()
                role_list = [r.lower() for r in roles]

                if user_role not in role_list:
                    flash("You do not have permission to access this section.", "danger")
                    return redirect(url_for('admin.dashboard'))

            # All good
            return fn(*args, **kwargs)

        return wrapped

    # If used as @admin_required (no parentheses)
    if callable(view_func):
        return decorator(view_func)

    # If used as @admin_required(...) with arguments
    return decorator

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
    # Already logged in as an admin-type user?
    if current_user.is_authenticated and getattr(current_user, 'is_admin', False):
        return redirect(url_for('admin.dashboard'))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        password_plain = form.password.data or ""

        try:
            admin = (
                User.query.options(load_only(User.id, User.email, User.password, User.role, User.is_admin, User.is_superadmin))
                .filter(User.email == email, User.is_admin == True)
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
