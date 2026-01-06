import os
from datetime import datetime
import smtplib
from math import ceil
from typing import Iterable
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from flask import current_app

# If you still want Flask-Mail for some cases:
try:
    from flask_mail import Message
    from app import mail  # only used in send_referral_email fallback (kept for compatibility)
    _HAS_FLASK_MAIL = True
except Exception:
    _HAS_FLASK_MAIL = False

# ==========================================================
#  SMTP CREDENTIALS (Render / Production compatible)
# ==========================================================
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))

EMAIL_ADDRESS = os.getenv("SMTP_USER")
EMAIL_PASSWORD = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("SMTP_FROM", EMAIL_ADDRESS)

if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
    print("❌ SMTP credentials missing. Check SMTP_USER / SMTP_PASS env vars.")

# ==========================================================
#  EMAIL CONFIG (use environment variables in production)
# ==========================================================
LOGO_URL = os.getenv("FAF_LOGO_URL", "https://app.faflcourier.com/static/logo.png")
# IMPORTANT: This should be your APP domain (not www.foreignafoot.com)
DASHBOARD_URL = os.getenv("FAF_DASHBOARD_URL", "https://app.faflcourier.com").rstrip("/")


# ==========================================================
#  INTERNAL: LOG EMAILS INTO IN-APP MESSAGES
# ==========================================================
def _get_admin_sender_id() -> int | None:
    """
    Returns an admin user id to use as sender_id when logging emails into DB Message table.
    Tries: is_superadmin, role=='admin', is_admin (if exists), then first user.
    """
    try:
        from app.models import User

        # Prefer superadmin
        if hasattr(User, "is_superadmin"):
            u = User.query.filter(User.is_superadmin.is_(True)).order_by(User.id.asc()).first()
            if u:
                return u.id

        # Prefer role-based admin
        if hasattr(User, "role"):
            u = User.query.filter(User.role == "admin").order_by(User.id.asc()).first()
            if u:
                return u.id

        # Fallback: boolean is_admin
        if hasattr(User, "is_admin"):
            u = User.query.filter(User.is_admin.is_(True)).order_by(User.id.asc()).first()
            if u:
                return u.id

        # Last resort
        u = User.query.order_by(User.id.asc()).first()
        return u.id if u else None

    except Exception:
        return None


def log_email_to_messages(
    recipient_user_id: int,
    subject: str,
    plain_body: str,
    sender_user_id: int | None = None,
) -> None:
    """
    Save the email into the same DB Message system so it shows in:
    - Customer messages inbox
    - Admin view user messages
    Never breaks email sending.
    """
    try:
        from app.extensions import db
        from app.models import Message as DBMessage

        sender_id = sender_user_id or _get_admin_sender_id()
        if not sender_id:
            return  # no admin/system sender found; skip

        m = DBMessage(
            sender_id=int(sender_id),
            recipient_id=int(recipient_user_id),
            subject=(subject or "").strip()[:255],
            body=(plain_body or "").strip(),
            created_at=datetime.utcnow(),
            is_read=False,
        )
        db.session.add(m)
        db.session.commit()

    except Exception:
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass


# ==========================================================
#  CORE EMAIL FUNCTION (SMTP)
# ==========================================================
def send_email(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    attachments: list[tuple[bytes, str, str]] | None = None,
    recipient_user_id: int | None = None,   # ✅ logs to Messages when provided
) -> bool:
    """
    attachments: list of (file_bytes, filename, mimetype)
      e.g. [(pdf_bytes, "Invoice.pdf", "application/pdf")]
    """

    # ✅ Fail fast if creds missing
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("❌ Cannot send email: SMTP_USER / SMTP_PASS not set.")
        return False

    msg = MIMEMultipart("mixed")
    msg["From"] = EMAIL_FROM or EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject

    # Body (plain + optional html)
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(plain_body or "", "plain"))
    if html_body:
        body.attach(MIMEText(html_body, "html"))
    msg.attach(body)

    # Attachments (optional)
    if attachments:
        for file_bytes, filename, mimetype in attachments:
            _maintype, subtype = (mimetype.split("/", 1) + ["octet-stream"])[:2]
            part = MIMEApplication(file_bytes, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    # SEND
    try:
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                smtp.send_message(msg)

        print(f"✅ Email sent to {to_email}")

        # ✅ Mirror into in-app Messages
        if recipient_user_id:
            log_email_to_messages(recipient_user_id, subject, plain_body)

        return True

    except Exception as e:
        print(f"❌ Email sending failed to {to_email}: {e}")
        return False


# ==========================================================
#  WELCOME EMAIL
# ==========================================================
def send_welcome_email(email, full_name, reg_number, recipient_user_id=None):
    plain_body = f"""
Dear {full_name},

Welcome to Foreign A Foot Logistics Limited – your trusted partner in shipping and logistics!

Your account has been successfully created.
Registration Number: {reg_number}

You can now log in at {DASHBOARD_URL} using your email and password.

📦 Your U.S. Shipping Address:
Air Standard
{full_name}
4652 N Hiatus Rd
{reg_number} A
Sunrise, Florida 33351

Thank you for choosing us!

- Foreign A Foot Logistics Limited Team
"""
    html_body = f"""
<html>
<body style="font-family: Arial, sans-serif;">
  <div style="background:#f9f9f9;padding:20px;">
    <div style="max-width:600px;margin:auto;background:#fff;padding:20px;border-radius:8px;">
      <img src="{LOGO_URL}" alt="Logo" style="max-width:180px;">
      <h2>Welcome, {full_name}!</h2>
      <p>Your registration number is <b>{reg_number}</b>.</p>
      <p><b>U.S. Shipping Address:</b><br>
      Air Standard<br>
      {full_name}<br>
      4652 N Hiatus Rd<br>
      {reg_number} A<br>
      Sunrise, Florida 33351</p>
      <p>
        <a href="{DASHBOARD_URL}" style="background:#5c3d91;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px;">
          Login to Dashboard
        </a>
      </p>
    </div>
  </div>
</body>
</html>
"""
    return send_email(
        to_email=email,
        subject="Welcome to Foreign A Foot Logistics Limited! �✈️",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  PASSWORD RESET EMAIL
# ==========================================================
def send_password_reset_email(to_email, full_name, reset_link, recipient_user_id=None):
    plain_body = f"""
Dear {full_name},

We received a request to reset your password.

🔗 Reset your password by clicking the link below:
{reset_link}

This link will expire in 10 minutes for your security.

If you didn’t request a password reset, simply ignore this email.
"""
    html_body = f"""
<html>
<body>
  <p>Dear {full_name},</p>
  <p>We received a request to reset your password.</p>
  <p>
    <a href="{reset_link}" style="background:#5c3d91;color:#fff;padding:10px 20px;text-decoration:none;">
      Reset Password
    </a>
  </p>
  <p>This link will expire in 10 minutes.</p>
</body>
</html>
"""
    return send_email(
        to_email=to_email,
        subject="Reset Your Password - Foreign A Foot Logistics",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  BULK MESSAGE EMAIL
# ==========================================================
def send_bulk_message_email(to_email, full_name, subject, message_body, recipient_user_id=None):
    plain_body = f"""
Dear {full_name},

{message_body}

Thanks,
Foreign A Foot Logistics Team
"""
    html_body = f"""
<html>
<body style="font-family: Arial, sans-serif;">
  <div style="background:#f9f9f9; padding:20px;">
    <div style="background:#fff; padding:30px; border-radius:8px;">
      <h3>Dear {full_name},</h3>
      <p>{(message_body or "").replace("\n", "<br>")}</p>
      <p>Best regards,<br>Foreign A Foot Logistics Team</p>
    </div>
  </div>
</body>
</html>
"""
    return send_email(
        to_email=to_email,
        subject=f"{subject} - Foreign A Foot Logistics",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  PACKAGE OVERSEAS RECEIVED EMAIL
# ==========================================================
def send_overseas_received_email(to_email, full_name, reg_number, packages, recipient_user_id=None):
    """
    Sends an email when FAFL receives a new package overseas.
    Includes a button/link for the customer to upload their invoice.
    """
    subject = f"Foreign A Foot Logistics Limited received a new package overseas for FAFL #{reg_number}"

    # Always points to APP domain packages page
    upload_url = f"{DASHBOARD_URL}/customer/packages"

    # Build table rows (HTML)
    rows_html = []
    for p in packages:
        house = getattr(p, "house_awb", None) if not isinstance(p, dict) else p.get("house_awb")
        weight = getattr(p, "weight", 0) if not isinstance(p, dict) else p.get("weight", 0)
        tracking = getattr(p, "tracking_number", None) if not isinstance(p, dict) else p.get("tracking_number")
        desc = getattr(p, "description", None) if not isinstance(p, dict) else p.get("description")
        status = getattr(p, "status", None) if not isinstance(p, dict) else p.get("status")
        rounded = ceil(weight or 0)

        rows_html.append(f"""
          <tr>
            <td style="padding:8px;border:1px solid #eee;">{house or '-'}</td>
            <td style="padding:8px;border:1px solid #eee; text-align:center;">{rounded}</td>
            <td style="padding:8px;border:1px solid #eee;">{tracking or '-'}</td>
            <td style="padding:8px;border:1px solid #eee;">{desc or '-'}</td>
            <td style="padding:8px;border:1px solid #eee; color:#d97706; font-weight:bold;">{status or 'Overseas'}</td>
          </tr>
        """)

    # Plain-text fallback
    plain_lines = [
        f"Dear {full_name},",
        "",
        f"Great news — we’ve received a new package overseas for FAFL #{reg_number}.",
        "",
        "Package Details:",
    ]

    for p in packages:
        house = getattr(p, "house_awb", None) if not isinstance(p, dict) else p.get("house_awb")
        weight = getattr(p, "weight", 0) if not isinstance(p, dict) else p.get("weight", 0)
        tracking = getattr(p, "tracking_number", None) if not isinstance(p, dict) else p.get("tracking_number")
        desc = getattr(p, "description", None) if not isinstance(p, dict) else p.get("description")
        status = getattr(p, "status", None) if not isinstance(p, dict) else p.get("status")

        plain_lines.append(
            f"- House AWB: {house or '-'}, "
            f"Rounded Weight (lbs): {ceil(weight or 0)}, "
            f"Tracking #: {tracking or '-'}, "
            f"Description: {desc or '-'}, "
            f"Status: {status or 'Overseas'}"
        )

    plain_lines += [
        "",
        "Customs requires a proper invoice for all packages.",
        "To avoid delays, please upload or send your invoice as soon as possible.",
        "",
        f"Upload your invoice here: {upload_url}",
        "",
        "Thank you for choosing Foreign A Foot Logistics Limited — your trusted logistics partner!",
        "",
        "Warm regards,",
        "The Foreign A Foot Logistics Team",
        "📍 Cedar Grove Passage Fort, Portmore",
        "🌐 www.faflcourier.com",
        "✉️ foreignafootlogistics@gmail.com",
        "☎️ (876) 560-7764",
    ]
    plain_body = "\n".join(plain_lines)

    html_body = f"""
<html>
<body style="font-family:Inter,Arial,sans-serif; line-height:1.6; color:#222;">
  <div style="max-width:700px;margin:0 auto;padding:16px;">
    <img src="{LOGO_URL}" alt="Foreign A Foot Logistics" style="max-width:180px; margin-bottom:16px;">
    <p>Hello {full_name},</p>
    <p>Great news – we’ve received a new package overseas for you. Package details:</p>

    <table cellpadding="0" cellspacing="0" style="border-collapse:collapse; width:100%; margin:16px 0;">
      <thead>
        <tr style="background:#f5f2fb; color:#4a148c;">
          <th style="padding:8px; text-align:left; border:1px solid #eee;">House AWB/Control #</th>
          <th style="padding:8px; text-align:center; border:1px solid #eee;">Rounded Weight (lbs)</th>
          <th style="padding:8px; text-align:left; border:1px solid #eee;">Tracking #</th>
          <th style="padding:8px; text-align:left; border:1px solid #eee;">Description</th>
          <th style="padding:8px; text-align:left; border:1px solid #eee;">Status</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows_html)}
      </tbody>
    </table>

    <p style="margin-top:20px; color:#333;">
      Customs requires a proper invoice for all packages.<br>
      To avoid any delays, please upload or send your invoice as soon as possible.
    </p>

    <p style="margin-top:18px;">
      <a href="{upload_url}"
         style="display:inline-block; padding:10px 22px; background:#4a148c; color:#ffffff;
                text-decoration:none; border-radius:6px; font-weight:600;">
        Upload / Add Your Invoice
      </a>
    </p>

    <p style="font-size:13px; color:#555; margin-top:6px;">
      Or visit <a href="{upload_url}" style="color:#4a148c; text-decoration:none;">{upload_url}</a>
      and locate this package by tracking number.
    </p>

    <hr style="margin:28px 0; border:none; border-top:1px solid #ddd;">
    <footer style="font-size:14px; color:#555;">
      <p style="margin:4px 0;">Warm regards,<br><strong>The Foreign A Foot Logistics Team</strong></p>
      <p style="margin:4px 0;">📍 Cedar Grove Passage Fort, Portmore</p>
      <p style="margin:4px 0;">🌐 <a href="https://www.faflcourier.com" style="color:#4a148c;text-decoration:none;">www.faflcourier.com</a></p>
      <p style="margin:4px 0;">✉️ <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4a148c;text-decoration:none;">foreignafootlogistics@gmail.com</a></p>
      <p style="margin:4px 0;">☎️ (876) 560-7764</p>
    </footer>
  </div>
</body>
</html>
"""
    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  INVOICE EMAIL
# ==========================================================
def send_invoice_email(to_email, full_name, invoice_number, amount_due, invoice_link, recipient_user_id=None):
    """
    Sends a clean, branded FAFL invoice email.
    """
    plain_body = f"""
Hello {full_name},

Your invoice from Foreign A Foot Logistics Limited is now available.

Invoice Number: {invoice_number}
Amount Due: JMD {amount_due:,.2f}

You may view or download your invoice here:
{invoice_link}

Thank you for shipping with us!
Foreign A Foot Logistics Limited
(876) 210-4291
"""

    html_body = f"""
<html>
  <body style="font-family: Inter, Arial, sans-serif; background:#f8f8fc; padding:0; margin:0;">
    <div style="max-width:640px; margin:20px auto; background:#ffffff;
                border-radius:12px; padding:30px; box-shadow:0 4px 20px rgba(0,0,0,0.06);">

      <div style="text-align:center; margin-bottom:20px;">
        <img src="{LOGO_URL}" alt="FAFL Logo" style="max-width:180px;">
      </div>

      <h2 style="text-align:center; color:#4a148c; margin-top:0;">
        Your Invoice is Ready
      </h2>

      <p style="color:#444; font-size:15px;">
        Hello <strong>{full_name}</strong>,<br><br>
        Your invoice from <strong>Foreign A Foot Logistics Limited</strong> has been generated and is now available.
        You may review the details below and click the button to view or download the full invoice.
      </p>

      <div style="
        border:1px solid #e5e0f0;
        border-radius:10px;
        padding:18px 22px;
        background:#faf7ff;
        margin:25px 0;
      ">
        <p style="margin:8px 0; font-size:15px;">
          <strong style="color:#4a148c;">Invoice Number:</strong><br>
          {invoice_number}
        </p>

        <p style="margin:8px 0; font-size:15px;">
          <strong style="color:#4a148c;">Amount Due:</strong><br>
          <span style="font-size:22px; font-weight:700; color:#4a148c;">
            JMD {amount_due:,.2f}
          </span>
        </p>
      </div>

      <div style="text-align:center; margin:25px 0 10px;">
        <a href="{invoice_link}"
           style="background:#4a148c; color:#ffffff; padding:14px 28px;
                  text-decoration:none; border-radius:6px; font-weight:600;
                  display:inline-block; font-size:16px;">
          View Invoice
        </a>
      </div>

      <p style="color:#666; font-size:14px; margin-top:20px; text-align:center;">
        Please contact us if you have any questions about this invoice.
      </p>

      <hr style="border:none; border-top:1px solid #e4e4e4; margin:30px 0;">

      <div style="text-align:center; color:#777; font-size:13px;">
        <p style="margin:4px 0;"><strong>Foreign A Foot Logistics Limited</strong></p>
        <p style="margin:4px 0;">Cedar Grove, Passage Fort, Portmore</p>
        <p style="margin:4px 0;">
          ✉️ <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4a148c; text-decoration:none;">
                foreignafootlogistics@gmail.com
              </a>
        </p>
        <p style="margin:4px 0;">☎️ (876) 210-4291</p>
      </div>
    </div>
  </body>
</html>
"""
    return send_email(
        to_email=to_email,
        subject=f"📄 Your Invoice #{invoice_number} is Ready",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  NEW MESSAGE EMAIL
# ==========================================================
def send_new_message_email(user_email, user_name, message_subject, message_body, recipient_user_id=None):
    subject = f"New Message: {message_subject}"
    body = (
        f"Hello {user_name},\n\n"
        f"You have received a new message:\n\n"
        f"{message_body}\n\n"
        f"Please log in to your account to reply or view details."
    )
    return send_email(
        to_email=user_email,
        subject=subject,
        plain_body=body,
        recipient_user_id=recipient_user_id,
    )


# ==========================================================
#  REFERRAL EMAIL
# ==========================================================
def send_referral_email(to_email: str, referral_code: str, referrer_name: str) -> bool:
    # ✅ Safe BASE_URL fallback (won't crash if missing)
    base = None
    try:
        base = current_app.config.get("BASE_URL") if current_app else None
    except Exception:
        base = None

    base_url = (base or "https://www.faflcourier.com").rstrip("/")
    register_link = f"{base_url}/register?ref={referral_code}"
    subject = "You've been invited to join Foreign A Foot Logistics!"

    plain_body = f"""
Hi there,

Your friend {referrer_name} has invited you to join Foreign A Foot Logistics!

Use their referral code during registration to get a $100 signup bonus.

Referral code: {referral_code}

Sign up here:
{register_link}

Thanks,
Foreign A Foot Logistics Team
"""

    html_body = f"""
<html>
  <body style="font-family: Inter, Arial, sans-serif; background:#f4f4f7; margin:0; padding:0;">
    <div style="max-width:640px; margin:0 auto; padding:24px;">
      <div style="background:#ffffff; border-radius:12px; padding:24px; box-shadow:0 4px 12px rgba(0,0,0,0.05);">

        <h2 style="margin-top:0; color:#4a148c;">
          {referrer_name} invited you to Foreign A Foot Logistics 🚚✈️
        </h2>

        <p style="color:#333333; line-height:1.6;">
          Your friend <strong>{referrer_name}</strong> wants you to start shipping smarter with
          <strong>Foreign A Foot Logistics</strong>.
        </p>

        <p style="color:#333333; line-height:1.6;">
          Use their referral code at signup and you'll receive a
          <strong>$100 bonus credit</strong> on your account.
        </p>

        <div style="margin:20px 0; padding:16px; border-radius:10px; background:#f5f2ff; text-align:center;">
          <div style="font-size:0.9rem; text-transform:uppercase; letter-spacing:0.08em; color:#6b21a8;">
            Your Referral Code
          </div>
          <div style="font-size:1.8rem; font-weight:700; margin-top:6px; color:#4a148c;">
            {referral_code}
          </div>
        </div>

        <div style="text-align:center; margin-bottom:24px;">
          <a href="{register_link}"
             style="display:inline-block; padding:12px 26px; background:#6f42c1; color:#ffffff;
                    text-decoration:none; border-radius:999px; font-weight:600;">
            Sign Up & Claim Your $100
          </a>
        </div>

        <p style="font-size:0.9rem; color:#555; line-height:1.5;">
          Just enter the code above when creating your account.
        </p>

        <hr style="border:none; border-top:1px solid #e5e5e5; margin:24px 0;">

        <p style="font-size:0.8rem; color:#777;">
          Foreign A Foot Logistics Limited<br>
          Cedar Grove Passage Fort, Portmore<br>
          <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4a148c; text-decoration:none;">
            foreignafootlogistics@gmail.com
          </a> · (876) 210-4291
        </p>
      </div>
    </div>
  </body>
</html>
"""
    return send_email(to_email=to_email, subject=subject, plain_body=plain_body, html_body=html_body)


# --- Ready for Pick Up (no attachments) -------------------
def send_ready_for_pickup_email(to_email: str, full_name: str, items: list[dict], recipient_user_id=None) -> bool:
    rows = "\n".join(
        f"- {it.get('tracking_number','-')} | {it.get('description','-')} | Amount Due: ${it.get('amount_due',0):.2f}"
        for it in items
    ) or "- (no details)"

    plain = (
        f"Hi {full_name},\n\n"
        f"The following package(s) are now READY FOR PICK UP:\n"
        f"{rows}\n\n"
        f"Thanks,\nForeign A Foot Logistics"
    )

    html = f"""
<html><body>
  <p>Hi {full_name},</p>
  <p>The following package(s) are now <b>READY FOR PICK UP</b>:</p>
  <ul>
    {''.join(f"<li>{it.get('tracking_number','-')} — {it.get('description','-')} — ${it.get('amount_due',0):.2f}</li>" for it in items)}
  </ul>
  <p>Thanks,<br>Foreign A Foot Logistics</p>
</body></html>
"""
    return send_email(
        to_email=to_email,
        subject="Your package is Ready for Pick Up",
        plain_body=plain,
        html_body=html,
        recipient_user_id=recipient_user_id,
    )


# --- Shipment invoice notice (no attachments; link/button only) ----
def send_shipment_invoice_link_email(to_email: str, full_name: str, total_due: float, invoice_link: str, recipient_user_id=None) -> bool:
    plain = (
        f"Hi {full_name},\n\n"
        f"Your invoice for the recent shipment is ready.\n"
        f"Total Amount Due: ${total_due:.2f}\n\n"
        f"View/pay here: {invoice_link}\n\n"
        f"Thanks,\nForeign A Foot Logistics"
    )

    html = f"""
<html><body>
  <p>Hi {full_name},</p>
  <p>Your invoice for the recent shipment is ready.</p>
  <p><b>Total Amount Due:</b> ${total_due:.2f}</p>
  <p>
    <a href="{invoice_link}" style="background:#5c3d91;color:#fff;padding:10px 16px;text-decoration:none;border-radius:4px;">
      View / Pay Invoice
    </a>
  </p>
  <p>Thanks,<br>Foreign A Foot Logistics</p>
</body></html>
"""
    return send_email(
        to_email=to_email,
        subject="Your Shipment Invoice",
        plain_body=plain,
        html_body=html,
        recipient_user_id=recipient_user_id,
    )


def compose_ready_pickup_email(full_name: str, packages: Iterable[dict]):
    """
    packages: iterable of dicts with keys:
      shipper, house_awb, tracking_number, weight
    Returns: (subject, plain_body, html_body)
    """
    subject = "Your package(s) are ready for pickup 🎉"

    rows_txt = []
    rows_html = []
    for p in packages:
        shipper = (p.get("shipper") or p.get("vendor") or "—")
        awb = p.get("house_awb") or p.get("house") or "—"
        track = p.get("tracking_number") or p.get("tracking") or "—"
        w_raw = p.get("weight") or 0
        w_up = ceil(float(w_raw) or 0)

        rows_txt.append(
            f"Shipper: {shipper}\n"
            f"Airway Bill: {awb}\n"
            f"Tracking Number: {track}\n"
            f"Weight (lbs): {w_up}\n"
            f"Status: Ready For Pickup/Delivery\n"
        )

        rows_html.append(f"""
<tr>
  <td style="padding:8px; border:1px solid #eee;">{shipper}</td>
  <td style="padding:8px; border:1px solid #eee;">{awb}</td>
  <td style="padding:8px; border:1px solid #eee;">{track}</td>
  <td style="padding:8px; border:1px solid #eee; text-align:center;">{w_up}</td>
  <td style="padding:8px; border:1px solid #eee; color:#16a34a; font-weight:600;">Ready for Pickup/Delivery</td>
</tr>
""")

    plain_body = (
        f"Hi {full_name},\n\n"
        f"Good news—your package(s) with Foreign A Foot Logistics Limited are ready for pickup/delivery.\n\n"
        f"Below is a quick summary:\n\n"
        + ("\n".join(rows_txt) if rows_txt else "(No package details)\n")
        + "\n"
        "Pickup Location: Cedar Grove, Passage Fort, Portmore\n"
        "Hours: Mon–Sat, 9:00 AM – 6:00 PM\n"
        "Contact: (876) 560-7764 | foreignafootlogistics@gmail.com\n\n"
        "If you need delivery, just reply to this email and we’ll arrange it.\n\n"
        "Thanks for shipping with us,\n"
        "Foreign A Foot Logistics Limited\n"
    )

    html_body = f"""
<html>
  <body style="font-family:Arial, sans-serif; color:#222;">
    <div style="max-width:680px; margin:0 auto; padding:16px;">
      <h2 style="color:#4a148c; margin:0 0 12px;">Great news—your package(s) are ready!</h2>
      <p>We’ve prepared the following item(s) for pickup/delivery:</p>
      <table style="width:100%; border-collapse:collapse; margin:12px 0;">
        <thead>
          <tr style="background:#f3ecff; color:#4a148c;">
            <th style="text-align:left; padding:8px; border:1px solid #eee;">Shipper</th>
            <th style="text-align:left; padding:8px; border:1px solid #eee;">Airway Bill</th>
            <th style="text-align:left; padding:8px; border:1px solid #eee;">Tracking #</th>
            <th style="text-align:center; padding:8px; border:1px solid #eee;">Weight (lbs)</th>
            <th style="text-align:left; padding:8px; border:1px solid #eee;">Status</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows_html) if rows_html else '<tr><td colspan="5" style="padding:8px; border:1px solid #eee;">No package details</td></tr>'}
        </tbody>
      </table>

      <div style="margin-top:16px;">
        <p style="margin:4px 0;"><strong>Pickup Location:</strong> Cedar Grove, Passage Fort, Portmore</p>
        <p style="margin:4px 0;"><strong>Hours:</strong> Mon–Sat, 9:00 AM – 6:00 PM</p>
        <p style="margin:4px 0;"><strong>Contact:</strong> (876) 560-7764 · foreignafootlogistics@gmail.com</p>
      </div>

      <p style="margin-top:16px;">Prefer delivery? Reply to this email and we’ll set it up.</p>

      <p style="margin-top:12px; color:#555;">
        Thanks for choosing <strong>Foreign A Foot Logistics Limited</strong>—we appreciate your business!
      </p>
    </div>
  </body>
</html>
"""
    return subject, plain_body, html_body

