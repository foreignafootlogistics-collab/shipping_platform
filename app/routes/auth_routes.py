# app/routes/auth_routes.py
from datetime import datetime

import bcrypt
import requests
import os
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

from app.extensions import db, limiter
from app.forms import RegisterForm, LoginForm
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
@limiter.limit(
    "10 per hour",
    methods=["POST"],
    error_message=(
        "Too many registration attempts. "
        "Please wait a few minutes and try again."
    ),
)
def register():
    form = RegisterForm()

    # Get the real visitor IP when running behind Cloudflare/Render.
    client_ip = (
        request.headers.get("CF-Connecting-IP")
        or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )

    user_agent = request.headers.get("User-Agent", "unknown")

    turnstile_site_key = os.getenv("TURNSTILE_SITE_KEY")
    turnstile_secret_key = os.getenv("TURNSTILE_SECRET_KEY")

    # Referral code can be supplied in the registration URL:
    # /register?ref=ABCDEFGH
    referrer_code = None

    if request.method == "GET":
        referrer_code = (request.args.get("ref") or "").strip() or None

        if referrer_code and hasattr(form, "referrer_code"):
            form.referrer_code.data = referrer_code

    is_valid_submit = form.validate_on_submit()

    # Log validation failures without printing passwords or the full form.
    if request.method == "POST" and not is_valid_submit:
        safe_errors = {
            field_name: errors
            for field_name, errors in form.errors.items()
            if field_name not in {"password", "confirm_password"}
        }

        current_app.logger.warning(
            "[REGISTER VALIDATION FAILED] "
            f"IP={client_ip} | "
            f"UA={user_agent} | "
            f"errors={safe_errors}"
        )

    if is_valid_submit:
        # ---------------------------------
        # Honeypot spam protection
        # ---------------------------------
        honeypot = (request.form.get("company_website") or "").strip()

        if honeypot:
            current_app.logger.warning(
                "[REGISTER BLOCKED] "
                f"Honeypot triggered | IP={client_ip} | UA={user_agent}"
            )

            flash(
                "Registration could not be completed. Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        # ---------------------------------
        # Cloudflare Turnstile verification
        # ---------------------------------
        turnstile_response = (
            request.form.get("cf-turnstile-response") or ""
        ).strip()

        if not turnstile_secret_key:
            current_app.logger.error(
                "[REGISTER ERROR] TURNSTILE_SECRET_KEY is not configured."
            )

            flash(
                "Security verification is temporarily unavailable. "
                "Please contact support.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        if not turnstile_response:
            current_app.logger.warning(
                "[REGISTER BLOCKED] "
                f"Turnstile response missing | "
                f"IP={client_ip} | UA={user_agent}"
            )

            flash(
                "Please complete the security verification.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        try:
            verification = requests.post(
                "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                data={
                    "secret": turnstile_secret_key,
                    "response": turnstile_response,
                    "remoteip": client_ip,
                },
                timeout=10,
            )

            verification.raise_for_status()
            verification_result = verification.json()

        except (requests.RequestException, ValueError) as error:
            current_app.logger.exception(
                "[REGISTER ERROR] "
                f"Turnstile verification unavailable | "
                f"IP={client_ip} | error={error}"
            )

            flash(
                "Security verification is temporarily unavailable. "
                "Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        if not verification_result.get("success"):
            error_codes = verification_result.get("error-codes", [])

            current_app.logger.warning(
                "[REGISTER BLOCKED] "
                f"Turnstile failed | "
                f"IP={client_ip} | "
                f"UA={user_agent} | "
                f"errors={error_codes}"
            )

            flash(
                "Security verification failed. Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        # ---------------------------------
        # Terms and Privacy confirmation
        # ---------------------------------
        terms_checked = request.form.get("termsCheck")

        if not terms_checked:
            flash(
                "You must agree to the Terms & Conditions and "
                "Privacy Policy to register.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        # ---------------------------------
        # Normalize submitted information
        # ---------------------------------
        full_name = (form.full_name.data or "").strip()
        email = (form.email.data or "").strip().lower()
        trn = (form.trn.data or "").strip()
        mobile = (form.mobile.data or "").strip()
        password = form.password.data or ""

        if hasattr(form, "referrer_code") and form.referrer_code.data:
            referrer_code = (
                form.referrer_code.data or ""
            ).strip() or None

        # ---------------------------------
        # Check existing customer details
        # ---------------------------------
        existing_email = User.query.filter(
            db.func.lower(User.email) == email
        ).first()

        if existing_email:
            flash(
                "An account already exists with this email address.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        if mobile and User.query.filter_by(mobile=mobile).first():
            flash(
                "An account already exists with this phone number.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        if trn and User.query.filter_by(trn=trn).first():
            flash(
                "An account already exists with this TRN.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        # ---------------------------------
        # Resolve the referring customer
        # ---------------------------------
        referrer_id = None

        if referrer_code:
            ref_user = User.query.filter_by(
                referral_code=referrer_code
            ).first()

            if ref_user:
                referrer_id = ref_user.id
            else:
                flash(
                    "The referral code was not found. "
                    "Registration will continue without it.",
                    "warning",
                )
                referrer_code = None

        # ---------------------------------
        # Generate a unique referral code
        # ---------------------------------
        new_ref_code = None

        for _ in range(10):
            candidate_code = User.generate_referral_code()

            code_exists = User.query.filter_by(
                referral_code=candidate_code
            ).first()

            if not code_exists:
                new_ref_code = candidate_code
                break

        if not new_ref_code:
            current_app.logger.error(
                "[REGISTER ERROR] "
                f"Unable to generate unique referral code | "
                f"email={email} | IP={client_ip}"
            )

            flash(
                "We could not complete your registration. "
                "Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        # Perform these operations after validation and duplicate checks.
        hashed_password = bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(),
        )

        try:
            registration_number = next_registration_number()

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "[REGISTER ERROR] "
                f"Registration number generation failed | "
                f"email={email} | IP={client_ip} | error={error}"
            )

            flash(
                "We could not generate your customer number. "
                "Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        now_dt = datetime.utcnow()
        role = "customer"

        # ---------------------------------
        # Create the customer account
        # ---------------------------------
        user = User(
            full_name=full_name,
            email=email,
            trn=trn or None,
            mobile=mobile or None,
            password=hashed_password,
            registration_number=registration_number,
            date_registered=now_dt.strftime("%Y-%m-%d"),
            created_at=now_dt,
            role=role,
            wallet_balance=0,
            referrer_id=referrer_id,
            referral_code=new_ref_code,
        )

        try:
            db.session.add(user)
            db.session.commit()

        except IntegrityError as error:
            db.session.rollback()

            current_app.logger.warning(
                "[REGISTER CONFLICT] "
                f"Database rejected duplicate information | "
                f"email={email} | "
                f"IP={client_ip} | "
                f"error={error.orig}"
            )

            flash(
                "An account already exists with one of the details "
                "provided. Please check the email address, phone "
                "number and TRN.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "[REGISTER ERROR] "
                f"Customer creation failed | "
                f"email={email} | "
                f"IP={client_ip} | "
                f"error={error}"
            )

            flash(
                "An unexpected error occurred while creating your "
                "account. Please try again.",
                "danger",
            )

            return render_template(
                "auth/register.html",
                form=form,
                turnstile_site_key=turnstile_site_key,
            )

        user_id = user.id

        current_app.logger.info(
            "[REGISTER SUCCESS] "
            f"user_id={user_id} | "
            f"registration_number={registration_number} | "
            f"email={email} | "
            f"IP={client_ip}"
        )

        # ---------------------------------
        # Create the in-app welcome message
        # ---------------------------------
        welcome_subject = "Welcome to FAFL Courier"

        welcome_body = (
            f"Hi {full_name},\n\n"
            "Welcome to Foreign A Foot Logistics!\n\n"
            f"Your customer code is: {registration_number}\n\n"
            "If you need help, message us anytime from your "
            "dashboard.\n\n"
            "— FAFL Team"
        )

        try:
            _log_in_app_message(
                user.id,
                welcome_subject,
                welcome_body,
            )
            db.session.commit()

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "[REGISTER WARNING] "
                f"Welcome message could not be saved | "
                f"user_id={user_id} | error={error}"
            )

        # ---------------------------------
        # Apply referral benefits
        # ---------------------------------
        if referrer_code and referrer_id:
            try:
                apply_referral_bonus(
                    user_id,
                    referrer_code,
                )

            except Exception as error:
                db.session.rollback()

                current_app.logger.exception(
                    "[REGISTER WARNING] "
                    f"Referral bonus failed | "
                    f"user_id={user_id} | "
                    f"referrer_id={referrer_id} | "
                    f"error={error}"
                )

        # ---------------------------------
        # Send welcome email
        # ---------------------------------
        try:
            email_utils.send_welcome_email(
                email=email,
                full_name=full_name,
                reg_number=registration_number,
            )

        except Exception as error:
            current_app.logger.exception(
                "[REGISTER WARNING] "
                f"Welcome email failed | "
                f"user_id={user_id} | "
                f"email={email} | "
                f"error={error}"
            )

        # ---------------------------------
        # Send tax-exemption email
        # ---------------------------------
        try:
            email_utils.send_tax_exemption_email(
                user_email=email,
                full_name=full_name,
                recipient_user_id=user_id,
            )

        except Exception as error:
            current_app.logger.exception(
                "[REGISTER WARNING] "
                f"Tax-exemption email failed | "
                f"user_id={user_id} | "
                f"email={email} | "
                f"error={error}"
            )

        # ---------------------------------
        # Sign in the newly registered user
        # ---------------------------------
        login_user(user)
        session["role"] = role

        try:
            user.last_login = datetime.utcnow()
            db.session.commit()

        except Exception as error:
            db.session.rollback()

            current_app.logger.exception(
                "[REGISTER WARNING] "
                f"last_login update failed | "
                f"user_id={user_id} | error={error}"
            )

        flash(
            "Registration successful! Welcome aboard.",
            "success",
        )

        return redirect(
            url_for("customer.customer_dashboard")
        )

    return render_template(
        "auth/register.html",
        form=form,
        turnstile_site_key=turnstile_site_key,
    )

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
