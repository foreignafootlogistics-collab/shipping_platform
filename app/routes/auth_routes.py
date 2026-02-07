# app/routes/auth_routes.py
from datetime import datetime

import bcrypt
from flask import (
    Blueprint,
    render_template,
    redirect,
    url_for,
    session,
    flash,
    current_app,
    request,
)
from flask_login import login_user, current_user, logout_user
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadSignature
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.forms import RegisterForm, LoginForm  # Import your FlaskForm classes
from app.utils import email_utils
from app.utils import next_registration_number
from app.utils import apply_referral_bonus, update_wallet
from app.models import User, Message as DBMessage



auth_bp = Blueprint("auth", __name__, template_folder="../templates")

# -------------------------------------------------
# In-app system messaging helpers
# -------------------------------------------------

def _system_sender_user():
    """
    Pick a sender for system messages.
    Prefer an admin user. Fallback to the first user.
    """
    admin = (
        User.query
        .filter((User.role == "admin") | (User.is_admin.is_(True)))
        .order_by(User.id.asc())
        .first()
    )
    return admin or User.query.order_by(User.id.asc()).first()


def _log_in_app_message(recipient_id: int, subject: str, body: str):
    sender = _system_sender_user()
    if not sender:
        return

    msg = DBMessage(
        sender_id=sender.id,
        recipient_id=recipient_id,
        subject=(subject or "").strip() or "Message",
        body=(body or "").strip(),
        is_read=False,
        created_at=datetime.utcnow(),
    )
    db.session.add(msg)

# ------------------------
# Register
# ------------------------
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    form = RegisterForm()

    # Try to get referral code from URL query param on GET
    referrer_code = None
    if request.method == "GET":
        referrer_code = (request.args.get("ref") or "").strip() or None

    if referrer_code and hasattr(form, "referrer_code"):
        form.referrer_code.data = referrer_code

    if form.validate_on_submit():
        # --- Check if user agreed to Terms & Privacy Policy ---
        terms_checked = request.form.get("termsCheck")
        if not terms_checked:
            flash(
                "You must agree to the Terms & Conditions and Privacy Policy to register.",
                "danger",
            )
            return render_template("auth/register.html", form=form)

        full_name = form.full_name.data.strip()
        email = form.email.data.strip()
        trn = form.trn.data.strip()
        mobile = form.mobile.data.strip()
        password = form.password.data.strip()

        # Override referral code from form if provided
        if hasattr(form, "referrer_code") and form.referrer_code.data:
            referrer_code = form.referrer_code.data.strip() or None

        # Hash password and KEEP as bytes (matches LargeBinary column)
        hashed_pw = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt())

        registration_number = next_registration_number()
        now_dt = datetime.utcnow()
        role = "customer"

        # --- Duplicate checks (email, TRN) via SQLAlchemy ---
        if User.query.filter_by(email=email).first():
            flash("Email already exists", "danger")
            return render_template("auth/register.html", form=form)

        if User.query.filter_by(trn=trn).first():
            flash("TRN already exists", "danger")
            return render_template("auth/register.html", form=form)

        # --- Resolve referrer (if any) ---
        referrer_id = None
        if referrer_code:
            ref_user = User.query.filter_by(referral_code=referrer_code).first()
            if ref_user:
                referrer_id = ref_user.id
            else:
                flash("Invalid referral code provided.", "warning")
                referrer_code = None  # treat as invalid

        # --- Generate a unique referral code for this new user ---
        new_ref_code = User.generate_referral_code(full_name)
        for _ in range(10):
            if not User.query.filter_by(referral_code=new_ref_code).first():
                break
            new_ref_code = User.generate_referral_code(full_name)


        # --- Create the user record via SQLAlchemy ---
        user = User(
            full_name=full_name,
            email=email,
            trn=trn,
            mobile=mobile,
            password=hashed_pw,  # bytes
            registration_number=registration_number,
            date_registered=now_dt,
            created_at=now_dt,
            role=role,
            wallet_balance=0,
            referrer_id=referrer_id,
            referral_code=new_ref_code,
        )

        try:
            db.session.add(user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("An unexpected database error occurred.", "danger")
            return render_template("auth/register.html", form=form)

        user_id = user.id

        # -------------------------
        # Log welcome message in-app ✅ (shows in Customer Messages + Admin View User)
        # -------------------------
        welcome_subject = "Welcome to FAFL Courier"
        welcome_body = (
            f"Hi {full_name},\n\n"
            f"Welcome to Foreign A Foot Logistics!\n\n"
            f"Your customer code is: {registration_number}\n\n"
            f"If you need help, message us anytime from your dashboard.\n\n"
            f"— FAFL Team"
        )

        try:
            _log_in_app_message(user.id, welcome_subject, welcome_body)
            db.session.commit()
        except Exception:
            db.session.rollback()

        # Apply referral bonus logic (if valid code + referrer)
        if referrer_code and referrer_id:
            try:
                apply_referral_bonus(user_id, referrer_code)
            except Exception as e:
                print(f"Error applying referral bonus: {e}")

        # Send welcome email
        try:
            email_utils.send_welcome_email(
                email=email, full_name=full_name, reg_number=registration_number
            )
        except Exception as e:
            print(f"Error sending welcome email: {e}")

        # Auto-login using Flask-Login
        login_user(user)
        session["role"] = role

        try:
            user.last_login = datetime.utcnow()
            db.session.commit()
        except Exception:
            db.session.rollback()

        flash("Registration successful! Welcome aboard.", "success")
        return redirect(url_for("customer.customer_dashboard"))

    return render_template("auth/register.html", form=form)


# ------------------------
# Login
# ------------------------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # If already logged in, send them where they belong
    if current_user.is_authenticated:
        if getattr(current_user, "role", "") == "admin":
            return redirect(url_for("admin.dashboard"))
        return redirect(url_for("customer.customer_dashboard"))

    form = LoginForm()

    if form.validate_on_submit():
        email = (form.email.data or "").strip()
        password_plain = form.password.data or ""
        password_bytes = password_plain.encode("utf-8")

        # Fetch user via SQLAlchemy
        user = User.query.filter_by(email=email).first()

        if user and user.password:
            stored_password = user.password

            # normalize memoryview → bytes if needed
            if isinstance(stored_password, memoryview):
                stored_password = stored_password.tobytes()

            # if it was (incorrectly) stored as a string, turn it into bytes
            if isinstance(stored_password, str):
                stored_password = stored_password.encode("utf-8")

            try:
                if bcrypt.checkpw(password_bytes, stored_password):
                    login_user(user)
                    session["role"] = getattr(user, "role", "customer")

                    try:
                        user.last_login = datetime.utcnow()
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                    # If you REALLY ever log admins in through here:
                    if getattr(user, "role", "") == "admin":
                        # correct endpoint name for the admin dashboard:
                        return redirect(url_for("admin.dashboard"))

                    # normal customers go to customer dashboard
                    return redirect(url_for("customer.customer_dashboard"))
            except Exception:
                # any bcrypt error falls through to invalid flash
                pass

        flash("Invalid email or password", "danger")

    return render_template("auth/login.html", form=form)

# ------------------------
# Logout
# ------------------------
@auth_bp.route("/logout")
def logout():
    logout_user()
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("auth.login"))


# ------------------------
# Forgot Password
# ------------------------
@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        if not email:
            flash("Please enter your email address.", "danger")
            return redirect(url_for("auth.forgot_password"))


        user = User.query.filter_by(email=email).first()
        if not user:
            flash("If that email exists, we sent a password reset link.", "success")
            return redirect(url_for("auth.login"))

        full_name = user.full_name or ""

        # Generate secure token (10 min expiry)
        serializer = URLSafeTimedSerializer(current_app.secret_key)
        token = serializer.dumps(email, salt="reset-password-salt")

        reset_link = url_for("auth.reset_password", token=token, _external=True)

        # Send password reset email
        try:
            email_utils.send_password_reset_email(
                to_email=email, full_name=full_name, reset_link=reset_link
            )
        except Exception as e:
            print(f"Error sending password reset email: {e}")

        flash("Password reset link sent to your email.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html")


# ------------------------
# Reset Password
# ------------------------
@auth_bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    serializer = URLSafeTimedSerializer(current_app.secret_key)

    try:
        email = serializer.loads(
            token, salt="reset-password-salt", max_age=600  # 10 minutes
        )
    except (SignatureExpired, BadSignature):
        flash("Reset link is invalid or has expired.", "danger")
        return redirect(url_for("auth.forgot_password"))

    user = User.query.filter_by(email=email).first()
    if not user:
        flash("No account found for this reset link.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""


        # Validate password match
        if new_password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("auth/reset_password.html", token=token)

        # Hash new password (bytes)
        hashed_pw = bcrypt.hashpw(
            new_password.encode("utf-8"), bcrypt.gensalt()
        )

        # Update via SQLAlchemy
        user.password = hashed_pw
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            flash("Error updating password. Please try again.", "danger")
            print(f"Error updating password: {e}")
            return render_template("auth/reset_password.html", token=token)

        flash("Password updated successfully. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/reset_password.html", token=token)
