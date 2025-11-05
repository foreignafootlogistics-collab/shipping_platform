from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_user, logout_user, login_required, current_user
from app.models import User
from app.forms import AdminLoginForm
import bcrypt
from functools import wraps

admin_auth_bp = Blueprint('admin_auth', __name__, url_prefix='/admin_auth')

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in as admin to access this page.", "danger")
            return redirect(url_for('admin_auth.admin_login'))
        if getattr(current_user, 'role', None) != 'admin':
            flash("Unauthorized access.", "danger")
            # Redirect customers to their login or dashboard
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return decorated_function

# ---------- Admin Login ----------
@admin_auth_bp.route('/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated and getattr(current_user, 'role', None) == 'admin':
        return redirect(url_for('admin.dashboard'))

    form = AdminLoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip()
        password_bytes = form.password.data.encode('utf-8')

        # Query admin user by role='admin'
        admin = User.query.filter_by(email=email, role='admin').first()

        if admin and admin.password:
            stored_password = admin.password
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')

            if bcrypt.checkpw(password_bytes, stored_password):
                login_user(admin)
                session['admin_id'] = admin.id 
                session['role'] = 'admin'
                flash("Admin login successful!", "success")
                return redirect(url_for('admin.dashboard'))

        flash("Invalid email or password.", "danger")

    return render_template('auth/admin_login.html', form=form)


# ---------- Admin Logout ----------
@admin_auth_bp.route('/logout')
@admin_required
def admin_logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('admin_auth.admin_login'))


# ---------- Admin Required Decorator ----------
def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            flash("Please log in to access the admin dashboard.", "danger")
            return redirect(url_for('admin_auth.admin_login'))
        if getattr(current_user, 'role', None) != 'admin':
            flash("Unauthorized access.", "danger")
            return redirect(url_for('admin_auth.admin_login'))
        return f(*args, **kwargs)
    return decorated_function
