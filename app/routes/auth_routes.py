from flask import Blueprint, render_template, redirect, url_for, session, flash, current_app, request
import sqlite3
import bcrypt
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from app.forms import RegisterForm, LoginForm  # Import your FlaskForm classes
from app.utils import email_utils
from app.utils import next_registration_number
from app.utils import apply_referral_bonus, update_wallet
from flask_login import login_user, current_user
from app.models import User  # your SQLAlchemy model that implements UserMixin
from app.config import get_db_connection

def generate_referral_code(length=6):
    import random, string
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


auth_bp = Blueprint('auth', __name__, template_folder='../templates')
DB_PATH = 'shipping_platform.db'


# ------------------------
# Register
# ------------------------
@auth_bp.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()

    # Try to get referral code from form or URL query param
    referrer_code = None
    if request.method == 'GET':
        referrer_code = request.args.get('ref', '').strip() or None

    if form.validate_on_submit():
        # --- Check if user agreed to Terms & Privacy Policy ---
        terms_checked = request.form.get('termsCheck')
        if not terms_checked:
            flash('You must agree to the Terms & Conditions and Privacy Policy to register.', 'danger')
            return render_template('auth/register.html', form=form)

        full_name = form.full_name.data.strip()
        email = form.email.data.strip()
        trn = form.trn.data.strip()
        mobile = form.mobile.data.strip()
        password = form.password.data.strip()

        # Override referral code from form if provided
        if hasattr(form, 'referrer_code') and form.referrer_code.data:
            referrer_code = form.referrer_code.data.strip()

        hashed_pw = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())

        registration_number = next_registration_number()
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        date_registered = created_at
        role = "customer"

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()

        # Prevent duplicate email
        c.execute("SELECT id FROM users WHERE email = ?", (email,))
        if c.fetchone():
            flash("Email already exists", "danger")
            conn.close()
            return render_template('auth/register.html', form=form)

        # Prevent duplicate TRN
        c.execute("SELECT id FROM users WHERE trn = ?", (trn,))
        if c.fetchone():
            flash("TRN already exists", "danger")
            conn.close()
            return render_template('auth/register.html', form=form)

        # Find referrer_id if code given (validate code)
        referrer_id = None
        if referrer_code:
            c.execute("SELECT id FROM users WHERE referral_code = ?", (referrer_code,))
            ref = c.fetchone()
            if ref:
                referrer_id = ref["id"]
            else:
                flash("Invalid referral code provided.", "warning")

        try:
            # Insert user with wallet balance 0 initially
            c.execute("""
                INSERT INTO users (
                    full_name, email, trn, mobile, password,
                    registration_number, date_registered, created_at, role,
                    wallet_balance, referrer_id, referral_code
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                full_name, email, trn, mobile, hashed_pw,
                registration_number, date_registered, created_at, role,
                0,  # wallet starts at 0
                referrer_id,
                generate_referral_code()
            ))
            conn.commit()
            user_id = c.lastrowid

        except sqlite3.IntegrityError:
            flash("An unexpected database error occurred.", "danger")
            conn.close()
            return render_template('auth/register.html', form=form)
        finally:
            conn.close()

        # Apply referral bonus logic
        if referrer_code and referrer_id:
            try:
                apply_referral_bonus(user_id, referrer_code)
            except Exception as e:
                print(f"Error applying referral bonus: {e}")

        # Send welcome email
        email_utils.send_welcome_email(
            email=email,
            full_name=full_name,
            reg_number=registration_number
        )

        # Auto-login
        session['user_id'] = user_id
        session['role'] = role

        flash("Registration successful! Welcome aboard.", "success")
        return redirect(url_for('customer.customer_dashboard'))

    return render_template('auth/register.html', form=form)
# ------------------------
# Login
# ------------------------

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    
    if form.validate_on_submit():
        email = form.email.data.strip()
        password_bytes = form.password.data.encode('utf-8')

        # Use helper instead of sqlite3.connect(DB_PATH)
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, password, role FROM users WHERE email = ?", (email,))
        user_data = c.fetchone()
        conn.close()

        if user_data:
            stored_password = user_data['password']  # Row object behaves like a dict
            if isinstance(stored_password, str):
                stored_password = stored_password.encode('utf-8')

            if bcrypt.checkpw(password_bytes, stored_password):
                # Use SQLAlchemy User model for Flask-Login
                user = User.query.get(user_data['id'])
                if user:
                    login_user(user)
                    session['role'] = user_data['role']

                    if user_data['role'] == 'admin':
                        return redirect(url_for('admin.admin_dashboard'))
                    else:
                        return redirect(url_for('customer.customer_dashboard'))

        flash("Invalid email or password", "danger")

    return render_template('auth/login.html', form=form)

# ------------------------
# Logout
# ------------------------
@auth_bp.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for('auth.login'))


# ------------------------
# Forgot Password
# ------------------------
@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form['email'].strip()

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT full_name FROM users WHERE email = ?", (email,))
        user = c.fetchone()
        conn.close()

        if not user:
            flash("No account found with that email.", "danger")
            return render_template('auth/forgot_password.html')

        full_name = user[0]

        # Generate secure token (10 min expiry)
        serializer = URLSafeTimedSerializer(current_app.secret_key)
        token = serializer.dumps(email, salt='reset-password-salt')

        reset_link = url_for('auth.reset_password', token=token, _external=True)

        # Send password reset email
        email_utils.send_password_reset_email(
            to_email=email,
            full_name=full_name,
            reset_link=reset_link
        )

        flash("Password reset link sent to your email.", "success")
        return redirect(url_for('auth.login'))

    return render_template('auth/forgot_password.html')


# ------------------------
# Reset Password
# ------------------------
@auth_bp.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    serializer = URLSafeTimedSerializer(current_app.secret_key)

    try:
        email = serializer.loads(token, salt='reset-password-salt', max_age=600)  # 10 min
    except (SignatureExpired, BadSignature):
        flash("Reset link is invalid or has expired.", "danger")
        return redirect(url_for('auth.forgot_password'))

    if request.method == 'POST':
        new_password = request.form['password']
        confirm_password = request.form.get('confirm_password')

        # Validate password match
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template('auth/reset_password.html', token=token)

        # Hash new password
        hashed_pw = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt())

        # Update database
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("UPDATE users SET password = ? WHERE email = ?", (hashed_pw, email))
        conn.commit()
        conn.close()

        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', token=token)
