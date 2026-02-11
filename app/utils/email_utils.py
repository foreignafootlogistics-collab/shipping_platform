import time
import os
from datetime import datetime
import smtplib
from math import ceil
from typing import Iterable

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from flask import current_app, render_template

# If you still want Flask-Mail for some cases:
try:
    from flask_mail import Message  # noqa: F401
    from app import mail  # noqa: F401  (kept for compatibility)
    _HAS_FLASK_MAIL = True
except Exception:
    _HAS_FLASK_MAIL = False

# ==========================================================
#  SMTP CREDENTIALS (Render / Production compatible)
# ==========================================================
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

EMAIL_ADDRESS = os.getenv("SMTP_USER")
EMAIL_PASSWORD = os.getenv("SMTP_PASS")
EMAIL_FROM = os.getenv("SMTP_FROM", EMAIL_ADDRESS)

if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
    print("❌ SMTP credentials missing. Check SMTP_USER / SMTP_PASS env vars.")

# ==========================================================
#  EMAIL CONFIG (use environment variables in production)
# ==========================================================
# ✅ PUBLIC URL (NO CID)
LOGO_URL = os.getenv("FAF_LOGO_URL", "https://app.faflcourier.com/static/logo.png")

# IMPORTANT: This should be your APP domain
DASHBOARD_URL = os.getenv("FAF_DASHBOARD_URL", "https://app.faflcourier.com").rstrip("/")
DELIVERY_URL = f"{DASHBOARD_URL}/customer/schedule-delivery"
TRANSACTIONS_URL = f"{DASHBOARD_URL}/customer/transactions/all"

# ==========================================================
#  BRAND HELPERS
# ==========================================================
_BRAND_WRAPPER_MARKER = "data-fafl-wrapper"

def _logo_img(height: int = 22) -> str:
    """
    Email-client-safe logo. Uses the `height` attribute (more reliable than CSS).
    """
    h = int(height)
    return (
        f'<img src="{LOGO_URL}" alt="Foreign A Foot Logistics" '
        f'height="{h}" style="height:{h}px;max-height:{h}px;width:auto;'
        f'display:block;border:0;outline:none;text-decoration:none;">'
    )

# Backwards-compatible alias (some templates may still call logo_img)
def logo_img(height: int = 22, margin_bottom: int = 12, center: bool = False) -> str:
    h = int(height)
    mb = int(margin_bottom)
    align = "margin-left:auto;margin-right:auto;" if center else ""
    return (
        f'<img src="{LOGO_URL}" alt="Foreign A Foot Logistics" '
        f'height="{h}" style="height:{h}px;max-height:{h}px;width:auto;'
        f'display:block;{align}border:0;outline:none;text-decoration:none;'
        f'margin:0 0 {mb}px 0;">'
    )

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


def pick_admin_recipient():
    """
    Pick an admin recipient safely:
    1) is_superadmin True (if column exists)
    2) role == 'admin'
    3) first user
    """
    from app.models import User

    admin = None

    if hasattr(User, "is_superadmin"):
        admin = User.query.filter(User.is_superadmin.is_(True)).order_by(User.id.asc()).first()

    if not admin and hasattr(User, "role"):
        admin = User.query.filter(User.role == "admin").order_by(User.id.asc()).first()

    if not admin:
        admin = User.query.order_by(User.id.asc()).first()

    return admin


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
def send_email_smtp(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    attachments: list[tuple[bytes, str, str]] | None = None,
    recipient_user_id: int | None = None,
    reply_to: str | None = None,
) -> bool:
    """
    Original SMTP sender (kept for fallback).
    html_body MUST be body-only; we wrap branding here.
    """

    def _wrap_with_branding(inner_html: str) -> str:
        inner_html = (inner_html or "").strip()
        if f'{_BRAND_WRAPPER_MARKER}="1"' in inner_html or _BRAND_WRAPPER_MARKER in inner_html:
            return inner_html

        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;font-size:15px;">
  <div style="width:100%;padding:24px 0;">
    <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;
                box-shadow:0 4px 12px rgba(0,0,0,0.05);" {_BRAND_WRAPPER_MARKER}="1">

      <!-- HEADER -->
      <div style="padding:18px 22px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #e5e7eb;">
        {_logo_img(22)}
        <div style="font-size:16px;font-weight:700;color:#4A148C;">
          Foreign A Foot Logistics Limited
        </div>
      </div>

      <!-- BODY -->
      <div style="padding:26px 24px;line-height:1.65;">
        {inner_html}
      </div>

      <!-- FOOTER -->
      <div style="background:#f5f2fb;padding:16px 22px;font-size:12.5px;color:#555;text-align:left;">

        <div style="display:flex;justify-content:flex-start;align-items:center;gap:10px;margin-bottom:8px;">
          {_logo_img(20)}
          <strong>Foreign A Foot Logistics Limited</strong>
        </div>

        <div style="margin-top:6px; line-height:1.6;">
          <img src="https://cdn-icons-png.flaticon.com/512/684/684908.png"
               alt="Location"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          Unit 7, Lot C22, Cedar Manor, Gregory Park, St. Catherine, Jamaica
        </div>

        <div style="margin-top:8px; line-height:1.8;">
          <img src="https://cdn-icons-png.flaticon.com/512/724/724664.png"
               alt="Phone"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="tel:18765607764" style="color:#4A148C;text-decoration:none;">(876) 560-7764</a><br>

          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png"
               alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18762104291" style="color:#4A148C;text-decoration:none;">
            WhatsApp: (876) 210-4291
          </a><br>

          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png"
               alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18765607764" style="color:#4A148C;text-decoration:none;">
            WhatsApp: (876) 560-7764
          </a><br>

          <img src="https://cdn-icons-png.flaticon.com/512/561/561127.png"
               alt="Email"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4A148C;text-decoration:none;">
            foreignafootlogistics@gmail.com
          </a><br>

          <img src="https://cdn-icons-png.flaticon.com/512/1006/1006771.png"
               alt="Website"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="{DASHBOARD_URL}" style="color:#4A148C;text-decoration:none;">
            https://app.faflcourier.com
          </a>
        </div>

        <div style="margin-top:12px;">
          <a href="{DASHBOARD_URL}"
             style="display:inline-block;background:#4A148C;color:#ffffff;text-decoration:none;
                    padding:10px 14px;border-radius:8px;font-weight:700;font-size:13px;">
            Open Customer Dashboard
          </a>
        </div>

      </div>
    </div>
  </div>
</body>
</html>""".strip()

    # Fail fast if creds missing
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("❌ Cannot send email: SMTP_USER / SMTP_PASS not set.")
        return False

    has_attachments = bool(attachments)
    msg = MIMEMultipart("mixed" if has_attachments else "alternative")

    msg["From"] = EMAIL_FROM or EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    # Plain + HTML (alternative part)
    if has_attachments:
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
    else:
        alt = msg

    alt.attach(MIMEText((plain_body or "").strip(), "plain", "utf-8"))

    if html_body:
        branded_html = _wrap_with_branding(html_body)
        alt.attach(MIMEText(branded_html, "html", "utf-8"))

    # Attachments
    if has_attachments:
        for a in attachments:
            if isinstance(a, dict):
                file_bytes = a.get("content", b"")
                filename = a.get("filename", "attachment")
                mimetype = a.get("mimetype", "application/octet-stream")
            else:
                file_bytes, filename, mimetype = a

            if not mimetype or "/" not in mimetype:
                mimetype = "application/octet-stream"

            _, subtype = mimetype.split("/", 1)
            part = MIMEApplication(file_bytes, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)

    # SEND (short retries, no 30-min sleeps)
    import ssl

    try:
        MAX_RETRIES = int(os.getenv("SMTP_MAX_RETRIES", "2"))
    except Exception:
        MAX_RETRIES = 2

    try:
        BACKOFF_BASE = float(os.getenv("SMTP_BACKOFF_BASE", "2"))
    except Exception:
        BACKOFF_BASE = 2.0

    try:
        BACKOFF_451 = float(os.getenv("SMTP_451_BACKOFF_SECONDS", "15"))
    except Exception:
        BACKOFF_451 = 15.0

    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = 30
            context = ssl.create_default_context()

            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout, context=context) as smtp:
                    smtp.ehlo()
                    smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                    smtp.send_message(msg)

            print(f"✅ Email sent to {to_email}")

            if recipient_user_id:
                log_email_to_messages(recipient_user_id, subject, (plain_body or "").strip())

            return True

        except smtplib.SMTPResponseException as e:
            last_err = f"{e.smtp_code} {e.smtp_error}"

            if int(e.smtp_code or 0) == 451:
                print(f"⏸️ SMTP 451 deferred for {to_email}. Backing off {BACKOFF_451}s then retrying...")
                time.sleep(BACKOFF_451)
            else:
                sleep_for = BACKOFF_BASE * attempt
                print(f"⚠️ SMTP error attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
                time.sleep(sleep_for)

        except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = str(e)
            sleep_for = BACKOFF_BASE * attempt
            print(f"⚠️ Connection issue attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
            time.sleep(sleep_for)

        except Exception as e:
            last_err = str(e)
            sleep_for = BACKOFF_BASE * attempt
            print(f"⚠️ Unknown error attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
            time.sleep(sleep_for)

    print(f"❌ Email sending failed to {to_email}: {last_err}")
    return False


def send_email(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    attachments: list[tuple[bytes, str, str]] | None = None,
    recipient_user_id: int | None = None,
    reply_to: str | None = None,
    force_new_connection: bool = False,  # kept for compat with your existing calls
) -> bool:
    """
    Router:
      - If USE_SENDGRID_API=1 -> SendGrid Web API (bulk-friendly)
      - else -> SMTP fallback
    Also logs to in-app Messages when recipient_user_id is provided.
    """

    use_api = (os.getenv("USE_SENDGRID_API", "0").strip().lower() in ("1", "true", "yes", "on"))

    # If attachments exist, prefer SMTP (unless you later add SendGrid attachments)
    has_attachments = bool(attachments)

    if use_api and not has_attachments:
        # Wrap branding here too so SendGrid emails look identical
        # Reuse SMTP wrapper via send_email_smtp's internal wrapper? We’ll do a small inline wrap:
        def _wrap_with_branding_for_api(inner_html: str) -> str:
            inner_html = (inner_html or "").strip()
            if f'{_BRAND_WRAPPER_MARKER}="1"' in inner_html or _BRAND_WRAPPER_MARKER in inner_html:
                return inner_html

            # Same wrapper as SMTP
            return send_email_smtp(
                to_email="noop@example.com",
                subject="noop",
                plain_body="noop",
                html_body=inner_html,
            ) and inner_html  # (won't be used)
        # ↑ ignore; we won't call smtp. Instead, do a direct wrap by calling your existing wrapper logic:
        # So: simplest = call the same HTML wrapper used in SMTP by duplicating it:
        # (To avoid complexity, we'll just use the SMTP sender for wrapping without sending)
        # Better: just wrap inline using the same HTML as in send_email_smtp.
        # For now: reuse the SMTP wrapper by duplicating it (keep consistent):
        # We'll just call the SMTP wrapper function by re-building it here:

        def _wrap(inner_html: str) -> str:
            inner_html = (inner_html or "").strip()
            if f'{_BRAND_WRAPPER_MARKER}="1"' in inner_html or _BRAND_WRAPPER_MARKER in inner_html:
                return inner_html
            return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;font-size:15px;">
  <div style="width:100%;padding:24px 0;">
    <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;
                box-shadow:0 4px 12px rgba(0,0,0,0.05);" {_BRAND_WRAPPER_MARKER}="1">
      <div style="padding:18px 22px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #e5e7eb;">
        {_logo_img(22)}
        <div style="font-size:16px;font-weight:700;color:#4A148C;">Foreign A Foot Logistics Limited</div>
      </div>
      <div style="padding:26px 24px;line-height:1.65;">{inner_html}</div>
      <div style="background:#f5f2fb;padding:16px 22px;font-size:12.5px;color:#555;text-align:left;">
        <div style="display:flex;justify-content:flex-start;align-items:center;gap:10px;margin-bottom:8px;">
          {_logo_img(20)} <strong>Foreign A Foot Logistics Limited</strong>
        </div>
        <div style="margin-top:6px; line-height:1.6;">
          <img src="https://cdn-icons-png.flaticon.com/512/684/684908.png" alt="Location"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          Unit 7, Lot C22, Cedar Manor, Gregory Park, St. Catherine, Jamaica
        </div>
        <div style="margin-top:8px; line-height:1.8;">
          <img src="https://cdn-icons-png.flaticon.com/512/724/724664.png" alt="Phone"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="tel:18765607764" style="color:#4A148C;text-decoration:none;">(876) 560-7764</a><br>
          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png" alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18762104291" style="color:#4A148C;text-decoration:none;">WhatsApp: (876) 210-4291</a><br>
          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png" alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18765607764" style="color:#4A148C;text-decoration:none;">WhatsApp: (876) 560-7764</a><br>
          <img src="https://cdn-icons-png.flaticon.com/512/561/561127.png" alt="Email"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4A148C;text-decoration:none;">foreignafootlogistics@gmail.com</a><br>
          <img src="https://cdn-icons-png.flaticon.com/512/1006/1006771.png" alt="Website"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="{DASHBOARD_URL}" style="color:#4A148C;text-decoration:none;">https://app.faflcourier.com</a>
        </div>
        <div style="margin-top:12px;">
          <a href="{DASHBOARD_URL}"
             style="display:inline-block;background:#4A148C;color:#ffffff;text-decoration:none;
                    padding:10px 14px;border-radius:8px;font-weight:700;font-size:13px;">
            Open Customer Dashboard
          </a>
        </div>
      </div>
    </div>
  </div>
</body></html>""".strip()

        wrapped_html = _wrap(html_body or "")

        ok = send_email_sendgrid_api(
            to_email=to_email,
            subject=subject,
            plain_body=(plain_body or "").strip(),
            html_body=wrapped_html,
            from_email=(EMAIL_FROM or EMAIL_ADDRESS or "support@faflcourier.com"),
            from_name="Foreign A Foot Logistics Limited",
            reply_to=(reply_to or EMAIL_FROM or EMAIL_ADDRESS or "support@faflcourier.com"),
            category="customer-portal",
        )

        if ok and recipient_user_id:
            log_email_to_messages(recipient_user_id, subject, (plain_body or "").strip())

        return ok

    # Fallback: SMTP (attachments supported here)
    return send_email_smtp(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=attachments,
        recipient_user_id=recipient_user_id,
        reply_to=reply_to,
    )



# ==========================================================
#  CORE EMAIL FUNCTION (SMTP)
#  ✅ Branding wrapper + utf-8 + attachments + reply-to
# ==========================================================
def send_email(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    attachments: list[tuple[bytes, str, str]] | None = None,
    recipient_user_id: int | None = None,   # logs to Messages when provided
    reply_to: str | None = None,
    force_new_connection: bool = False,
) -> bool:
    """
    Sends email via SMTP.

    ✅ IMPORTANT:
    - Pass html_body as BODY CONTENT ONLY (no <html>, no header/logo, no footer).
    - This function will wrap it with FAFL header + footer automatically.
    """

    def _wrap_with_branding(inner_html: str) -> str:
        inner_html = (inner_html or "").strip()

        # Don't double-wrap
        if f'{_BRAND_WRAPPER_MARKER}="1"' in inner_html or _BRAND_WRAPPER_MARKER in inner_html:
            return inner_html


        return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;color:#111827;font-size:15px;">
  <div style="width:100%;padding:24px 0;">
    <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;
                box-shadow:0 4px 12px rgba(0,0,0,0.05);" {_BRAND_WRAPPER_MARKER}="1">

      <!-- HEADER -->
      <div style="padding:18px 22px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #e5e7eb;">
        {_logo_img(22)}
        <div style="font-size:16px;font-weight:700;color:#4A148C;">
          Foreign A Foot Logistics Limited
        </div>
      </div>

      <!-- BODY -->
      <div style="padding:26px 24px;line-height:1.65;">
        {inner_html}
      </div>

      <!-- FOOTER -->
      <div style="background:#f5f2fb;padding:16px 22px;font-size:12.5px;color:#555;text-align:left;">

        <div style="display:flex;justify-content:flex-start;align-items:center;gap:10px;margin-bottom:8px;">
          {_logo_img(20)}
          <strong>Foreign A Foot Logistics Limited</strong>
        </div>

        <!-- Address -->
        <div style="margin-top:6px; line-height:1.6;">
          <img src="https://cdn-icons-png.flaticon.com/512/684/684908.png"
               alt="Location"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          Unit 7, Lot C22, Cedar Manor, Gregory Park, St. Catherine, Jamaica
        </div>

        <!-- Contact links -->
        <div style="margin-top:8px; line-height:1.8;">

          <img src="https://cdn-icons-png.flaticon.com/512/724/724664.png"
               alt="Phone"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="tel:18765607764"
             style="color:#4A148C;text-decoration:none;">
             (876) 560-7764
          </a>
          <br>

          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png"
               alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18762104291"
             style="color:#4A148C;text-decoration:none;">
             WhatsApp: (876) 210-4291
          </a>
          <br>

          <img src="https://cdn-icons-png.flaticon.com/512/733/733585.png"
               alt="WhatsApp"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="https://wa.me/18765607764"
             style="color:#4A148C;text-decoration:none;">
             WhatsApp: (876) 560-7764
          </a>
          <br>

          <img src="https://cdn-icons-png.flaticon.com/512/561/561127.png"
               alt="Email"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="mailto:foreignafootlogistics@gmail.com"
             style="color:#4A148C;text-decoration:none;">
            foreignafootlogistics@gmail.com
          </a>
          <br>

          <img src="https://cdn-icons-png.flaticon.com/512/1006/1006771.png"
               alt="Website"
               style="width:14px;height:14px;vertical-align:middle;margin-right:6px;">
          <a href="{DASHBOARD_URL}"
             style="color:#4A148C;text-decoration:none;">
            https://app.faflcourier.com
          </a>
        </div>

        <!-- Dashboard Button -->
        <div style="margin-top:12px;">
          <a href="{DASHBOARD_URL}"
             style="display:inline-block;background:#4A148C;color:#ffffff;text-decoration:none;
                    padding:10px 14px;border-radius:8px;font-weight:700;font-size:13px;">
            Open Customer Dashboard
          </a>
        </div>

      </div>    
  </div>
</body>
</html>""".strip()

    # Fail fast if creds missing
    if not EMAIL_ADDRESS or not EMAIL_PASSWORD:
        print("❌ Cannot send email: SMTP_USER / SMTP_PASS not set.")
        return False

    has_attachments = bool(attachments)
    msg = MIMEMultipart("mixed" if has_attachments else "alternative")

    msg["From"] = EMAIL_FROM or EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to

    # Plain + HTML (alternative part)
    if has_attachments:
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
    else:
        alt = msg

    alt.attach(MIMEText((plain_body or "").strip(), "plain", "utf-8"))

    if html_body:
        branded_html = _wrap_with_branding(html_body)
        alt.attach(MIMEText(branded_html, "html", "utf-8"))

    # Attachments
    if has_attachments:
        for a in attachments:
            if isinstance(a, dict):
                file_bytes = a.get("content", b"")
                filename = a.get("filename", "attachment")
                mimetype = a.get("mimetype", "application/octet-stream")
            else:
                file_bytes, filename, mimetype = a

            if not mimetype or "/" not in mimetype:
                mimetype = "application/octet-stream"

            _, subtype = mimetype.split("/", 1)
            part = MIMEApplication(file_bytes, _subtype=subtype)
            part.add_header("Content-Disposition", "attachment", filename=filename)
            msg.attach(part)
    
    import ssl

    # ✅ Keep retries small (bulk loops should throttle, not this function)
    try:
        MAX_RETRIES = int(os.getenv("SMTP_MAX_RETRIES", "2"))
    except Exception:
        MAX_RETRIES = 2

    try:
        BACKOFF_BASE = float(os.getenv("SMTP_BACKOFF_BASE", "2"))
    except Exception:
        BACKOFF_BASE = 2.0

    try:
        BACKOFF_451 = float(os.getenv("SMTP_451_BACKOFF_SECONDS", "15"))
    except Exception:
        BACKOFF_451 = 15.0

    last_err = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            timeout = 30
            context = ssl.create_default_context()

            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout, context=context) as smtp:
                    smtp.ehlo()
                    smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
                    smtp.send_message(msg)

            print(f"✅ Email sent to {to_email}")

            if recipient_user_id:
                log_email_to_messages(recipient_user_id, subject, (plain_body or "").strip())

            return True

        except smtplib.SMTPResponseException as e:
            last_err = f"{e.smtp_code} {e.smtp_error}"

            # ✅ Yahoo deferral / temp failure:
            # don't freeze the request for 30 minutes
            if int(e.smtp_code or 0) == 451:
                print(f"⏸️ SMTP 451 deferred for {to_email}. Backing off {BACKOFF_451}s then retrying...")
                time.sleep(BACKOFF_451)
            else:
                sleep_for = BACKOFF_BASE * attempt
                print(f"⚠️ SMTP error attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
                time.sleep(sleep_for)

        except (smtplib.SMTPServerDisconnected, ConnectionResetError, TimeoutError, OSError) as e:
            last_err = str(e)
            sleep_for = BACKOFF_BASE * attempt
            print(f"⚠️ Connection issue attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
            time.sleep(sleep_for)

        except Exception as e:
            last_err = str(e)
            sleep_for = BACKOFF_BASE * attempt
            print(f"⚠️ Unknown error attempt {attempt}/{MAX_RETRIES} to {to_email}: {last_err}. Sleeping {sleep_for}s")
            time.sleep(sleep_for)

    print(f"❌ Email sending failed to {to_email}: {last_err}")
    return False




# ==========================================================
#  WELCOME EMAIL (BODY ONLY)
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
3200 NW 112th Ave
KCDA-{reg_number} A
Doral, Florida 33172

Thank you for choosing us!

- Foreign A Foot Logistics Limited Team
""".strip()

    html_body = f"""
<h2 style="margin:0 0 10px 0;">Welcome, {full_name}!</h2>
<p style="margin:0 0 10px 0;">Your registration number is <b>{reg_number}</b>.</p>

<p style="margin:0 0 10px 0;"><b>U.S. Shipping Address:</b><br>
Air Standard<br>
{full_name}<br>
3200 NW 112th Ave<br>
KCDA-{reg_number} A<br>
Doral, Florida 33172</p>

<p style="margin:14px 0 0 0;">
  <a href="{DASHBOARD_URL}" style="background:#4A148C;color:#fff;padding:10px 18px;text-decoration:none;border-radius:6px;display:inline-block;">
    Login to Dashboard
  </a>
</p>
""".strip()

    return send_email(
        to_email=email,
        subject="Welcome to Foreign A Foot Logistics Limited!",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  PASSWORD RESET EMAIL (BODY ONLY)
# ==========================================================
def send_password_reset_email(to_email, full_name, reset_link, recipient_user_id=None):
    plain_body = f"""
Dear {full_name},

We received a request to reset your password.

🔗 Reset your password by clicking the link below:
{reset_link}

This link will expire in 10 minutes for your security.

If you didn’t request a password reset, simply ignore this email.
""".strip()

    html_body = f"""
<p style="margin:0 0 10px 0;">Dear {full_name},</p>
<p style="margin:0 0 12px 0;">We received a request to reset your password.</p>

<p style="margin:14px 0;">
  <a href="{reset_link}" style="background:#4A148C;color:#fff;padding:10px 18px;text-decoration:none;border-radius:6px;display:inline-block;">
    Reset Password
  </a>
</p>

<p style="margin:0;color:#6b7280;font-size:13px;">This link will expire in 10 minutes.</p>
<p style="margin:12px 0 0;color:#6b7280;font-size:13px;">If you didn’t request a password reset, you can ignore this email.</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject="Reset Your Password - Foreign A Foot Logistics",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  BULK MESSAGE EMAIL (BODY ONLY)
# ==========================================================
def send_bulk_message_email(to_email, full_name, subject, message_body, recipient_user_id=None):
    """
    Bulk message email (BODY ONLY).
    NOTE: send_email() wraps this with FAFL header + footer automatically.
    """
    subject = (subject or "").strip()
    message_body = (message_body or "").strip()

    plain_body = f"""
Dear {full_name},

{message_body}

Best regards,
Foreign A Foot Logistics Team
""".strip()

    safe_msg = message_body.replace("\n", "<br>")

    html_body = f"""
<p style="margin:0 0 12px 0; color:#111827;">
  <strong>Dear {full_name},</strong>
</p>

<div style="white-space:normal; line-height:1.6; color:#111827; margin-bottom:16px;">
  {safe_msg}
</div>

<p style="margin:0; color:#111827;">
  Best regards,<br>
  <strong>Foreign A Foot Logistics Team</strong>
</p>
""".strip()

    email_subject = subject or "Announcement"
    if "foreign a foot" not in email_subject.lower():
        email_subject = f"{email_subject} - Foreign A Foot Logistics"

    return send_email(
        to_email=to_email,
        subject=email_subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  PACKAGE OVERSEAS RECEIVED EMAIL (BODY ONLY)
# ==========================================================
def send_overseas_received_email(to_email, full_name, reg_number, packages, recipient_user_id=None):
    """
    Sends an email when FAFL receives a new package overseas.
    Includes a button/link for the customer to upload their invoice.
    """
    subject = f"Foreign A Foot Logistics Limited received a new package overseas for FAFL #{reg_number}"
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
        "Warm regards,",
        "The Foreign A Foot Logistics Team",
    ]
    plain_body = "\n".join(plain_lines)

    html_body = f"""
<p style="margin:0 0 10px 0;">Hello {full_name},</p>
<p style="margin:0 0 14px 0;">Great news – we’ve received a new package overseas for you. Package details:</p>

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

<p style="margin:16px 0 0 0; color:#111827;">
  Customs requires a proper invoice for all packages.<br>
  To avoid any delays, please upload or send your invoice as soon as possible.
</p>

<p style="margin:16px 0 0 0;">
  <a href="{upload_url}"
     style="display:inline-block; padding:10px 18px; background:#4a148c; color:#ffffff;
            text-decoration:none; border-radius:6px; font-weight:600;">
    Upload / Add Your Invoice
  </a>
</p>

<p style="font-size:13px; color:#6b7280; margin-top:10px;">
  Or visit <a href="{upload_url}" style="color:#4a148c; text-decoration:none;">{upload_url}</a>
  and locate this package by tracking number.
</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  INVOICE EMAIL (INLINE HTML DESIGN — LIGHT BACKGROUND)
# ==========================================================
def send_invoice_email(to_email, full_name, invoice, pdf_bytes=None, recipient_user_id=None):
    """
    Sends a clean, light-background invoice notification email with:
    - White background, black text
    - FAFL purple accents + yellow total
    - Table: AWB/BL | Merchant | Tracking # | Weight (rounded UP)
    - Big "Total Due"
    - NO attachment language
    - NO links
    - NO external HTML template
    - Mobile-safe (scroll wrapper + responsive stacked rows on small screens)
    - Black table outline + black grid lines
    """

    invoice = invoice or {}
    inv_no = invoice.get("number") or "—"
    packages = invoice.get("packages") or []

    # ----- total due -----
    total_due = invoice.get("total_due") or 0
    try:
        total_due_num = float(total_due)
    except (TypeError, ValueError):
        total_due_num = 0.0

    # ----- date (optional) -----
    inv_date = invoice.get("date")
    if hasattr(inv_date, "strftime"):
        inv_date_str = inv_date.strftime("%Y-%m-%d %H:%M")
    elif isinstance(inv_date, str) and inv_date:
        inv_date_str = inv_date
    else:
        inv_date_str = ""

    subject = f"📄 Invoice {inv_no} is Ready"
    pkg_count = len(packages)

    # ----- safe HTML escaping -----
    def esc(s):
        s = "" if s is None else str(s)
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("'", "&#39;")
        )

    # ----- build table rows (desktop) + stacked cells (mobile) -----
    row_html = []
    for p in packages:
        awb = esc(p.get("house_awb") or "-")
        merchant = esc(p.get("merchant") or "-")
        tracking = esc(p.get("tracking_number") or "-")

        try:
            w = float(p.get("weight") or 0)
        except (TypeError, ValueError):
            w = 0.0

        # ✅ ROUND UP (never round down)
        weight_display = str(int(ceil(w)))

        row_html.append(f"""
          <tr class="stack-row">
            <td class="cell" style="padding:12px 14px; border-top:1px solid #111111; border-right:1px solid #111111; color:#000000; font-size:14px; font-family:Arial, sans-serif;">
              <span class="stack-label" style="display:none;">AWB/BL</span>
              <span class="stack-val">{awb}</span>
            </td>
            <td class="cell" style="padding:12px 14px; border-top:1px solid #111111; border-right:1px solid #111111; color:#000000; font-size:14px; font-family:Arial, sans-serif;">
              <span class="stack-label" style="display:none;">Merchant</span>
              <span class="stack-val">{merchant}</span>
            </td>
            <td class="cell" style="padding:12px 14px; border-top:1px solid #111111; border-right:1px solid #111111; color:#000000; font-size:14px; font-family:Arial, sans-serif;">
              <span class="stack-label" style="display:none;">Tracking #</span>
              <span class="stack-val">{tracking}</span>
            </td>
            <td class="cell" style="padding:12px 14px; border-top:1px solid #111111; color:#000000; font-size:14px; font-family:Arial, sans-serif;">
              <span class="stack-label" style="display:none;">Weight</span>
              <span class="stack-val">{weight_display}</span>
            </td>
          </tr>
        """)

    rows_block = "\n".join(row_html) if row_html else """
      <tr>
        <td colspan="4" style="padding:14px; border-top:1px solid #111111; color:#111111; font-size:14px; font-family:Arial, sans-serif;">
          No packages found on this invoice.
        </td>
      </tr>
    """

    greeting_name = (full_name or "").strip() or "Customer"

    intro_line = (
        f"Foreign a Foot Logistics Limited has billed you for {pkg_count} package(s). "
        f"Please see details below."
    )

    # ----- INLINE HTML (WHITE BACKGROUND) -----
    html_body = f"""
    <div style="margin:0; padding:0; background:#f3f4f6;">
      <style>
        /* Mobile stacking (best effort in email clients) */
        @media only screen and (max-width: 600px) {{
          .mobile-hide {{ display:none !important; }}
          .stack-row td {{
            display:block !important;
            width:100% !important;
            box-sizing:border-box !important;
            border-right:none !important;
          }}
          .stack-label {{
            display:block !important;
            font-weight:700 !important;
            color:#4A148C !important;
            margin-bottom:4px !important;
            font-family:Arial, sans-serif !important;
          }}
          .stack-val {{
            display:block !important;
            margin-bottom:10px !important;
            font-family:Arial, sans-serif !important;
          }}
          .table-wrap {{
            overflow-x:visible !important;
          }}
          .tbl {{
            min-width:0 !important;
          }}
        }}
      </style>

      <table width="100%" cellpadding="0" cellspacing="0" style="padding:24px 0;">
        <tr>
          <td align="center">
            <table width="680" cellpadding="0" cellspacing="0"
                   style="max-width:680px; width:100%; background:#ffffff; border-radius:12px; overflow:hidden; box-shadow:0 4px 12px rgba(0,0,0,0.08);">

              <!-- HEADER (FAFL PURPLE) -->
              <tr>
                <td style="background:#4A148C; padding:16px 20px;">
                  <div style="font-family:Arial, sans-serif; font-size:16px; color:#ffffff; font-weight:700;">
                    Foreign A Foot Logistics Limited
                  </div>
                  <div style="font-family:Arial, sans-serif; font-size:12px; color:#efe6ff; margin-top:2px;">
                    Invoice Notification{(" • " + esc(inv_date_str)) if inv_date_str else ""}
                  </div>
                </td>
              </tr>

              <!-- BODY -->
              <tr>
                <td style="padding:22px 20px 10px 20px;">
                  <div style="font-family:Arial, sans-serif; font-size:22px; color:#000000; font-weight:700; margin-bottom:10px;">
                    Hi {esc(greeting_name)},
                  </div>

                  <div style="font-family:Arial, sans-serif; font-size:16px; color:#111111; line-height:1.55;">
                    {esc(intro_line)}
                  </div>

                  <div style="height:14px;"></div>

                  <!-- MOBILE SAFE WRAP (allows swipe if needed) -->
                  <div class="table-wrap" style="max-width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch;">
                    <table class="tbl" width="100%" cellpadding="0" cellspacing="0"
                           style="min-width:520px; border-collapse:collapse; background:#ffffff; border:2px solid #000000; border-radius:10px; overflow:hidden;">
                      <thead class="mobile-hide">
                        <tr style="background:#f5f2fb;">
                          <th align="left" style="padding:12px 14px; color:#4A148C; font-family:Arial, sans-serif; font-size:16px; font-weight:800; border-right:1px solid #000000; border-bottom:2px solid #000000;">
                            AWB/BL
                          </th>
                          <th align="left" style="padding:12px 14px; color:#4A148C; font-family:Arial, sans-serif; font-size:16px; font-weight:800; border-right:1px solid #000000; border-bottom:2px solid #000000;">
                            Merchant
                          </th>
                          <th align="left" style="padding:12px 14px; color:#4A148C; font-family:Arial, sans-serif; font-size:16px; font-weight:800; border-right:1px solid #000000; border-bottom:2px solid #000000;">
                            Tracking #
                          </th>
                          <th align="left" style="padding:12px 14px; color:#4A148C; font-family:Arial, sans-serif; font-size:16px; font-weight:800; border-bottom:2px solid #000000;">
                            Weight
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows_block}
                      </tbody>
                    </table>
                  </div>

                  <div style="height:18px;"></div>

                  <!-- TOTAL DUE (BIG + YELLOW) -->
                  <div style="font-family:Arial, sans-serif; font-size:18px; color:#000000; font-weight:700;">
                    Total Due:
                  </div>
                  <div style="font-family:Arial, sans-serif; font-size:54px; line-height:1.05; font-weight:900; color:#FFD400; margin-top:6px;">
                    ${total_due_num:,.2f}
                  </div>

                  <div style="height:14px;"></div>

                  <div style="font-family:Arial, sans-serif; font-size:13px; color:#111111;">
                    Thank you for shipping with us.
                  </div>
                </td>
              </tr>

              <!-- FOOTER STRIP -->
              <tr>
                <td style="padding:14px 20px; background:#f5f2fb; border-top:1px solid #000000;">
                  <div style="font-family:Arial, sans-serif; font-size:12px; color:#111111;">
                    © {datetime.utcnow().year} Foreign A Foot Logistics Limited
                  </div>
                </td>
              </tr>

            </table>
          </td>
        </tr>
      </table>
    </div>
    """

    # ----- Plain text fallback -----
    plain_lines = []
    plain_lines.append(f"Hi {greeting_name},\n")
    plain_lines.append(f"Foreign a Foot Logistics Limited has billed you for {pkg_count} package(s).")
    plain_lines.append("")
    plain_lines.append("AWB/BL | Merchant | Tracking # | Weight")
    for p in packages:
        awb = p.get("house_awb") or "-"
        merchant = p.get("merchant") or "-"
        tracking = p.get("tracking_number") or "-"
        try:
            w = float(p.get("weight") or 0)
        except Exception:
            w = 0.0
        plain_lines.append(f"{awb} | {merchant} | {tracking} | {int(ceil(w))}")  # ✅ round UP
    plain_lines.append("")
    plain_lines.append(f"Total Due: ${total_due_num:,.2f}")
    plain_lines.append("")
    plain_lines.append("— Foreign A Foot Logistics Limited")

    plain_body = "\n".join(plain_lines)

    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=[],  # explicitly none
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )



# ==========================================================
#  NEW MESSAGE EMAIL (BODY ONLY)
# ==========================================================
def send_new_message_email(user_email, user_name, message_subject, message_body, recipient_user_id=None):
    subject = f"New Message: {message_subject}"
    body = (
        f"Hello {user_name},\n\n"
        f"You have received a new message:\n\n"
        f"{message_body}\n\n"
        f"Please log in to your account to reply or view details."
    )

    safe_msg = (message_body or "").replace("\n", "<br>")

    html_body = f"""
<p style="margin:0 0 10px 0;">Hello {user_name},</p>
<p style="margin:0 0 12px 0;">
  You have received a new message:
</p>
<div style="margin:0 0 14px 0; line-height:1.6;">
  {safe_msg}
</div>
<p style="margin:0;">
  Please log in to your account to reply or view details.
</p>
""".strip()

    return send_email(
        to_email=user_email,
        subject=subject,
        plain_body=body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  REFERRAL EMAIL (BODY ONLY)
# ==========================================================
def send_referral_email(to_email: str, referral_code: str, referrer_name: str) -> bool:
    base = None
    try:
        base = current_app.config.get("BASE_URL") if current_app else None
    except Exception:
        base = None

    base_url = (base or DASHBOARD_URL).rstrip("/")
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
""".strip()

    html_body = f"""
<h2 style="margin:0 0 10px 0; color:#4a148c;">
  {referrer_name} invited you to Foreign A Foot Logistics 🚚✈️
</h2>

<p style="margin:0 0 10px 0; line-height:1.6;">
  Your friend <strong>{referrer_name}</strong> wants you to start shipping smarter with
  <strong>Foreign A Foot Logistics</strong>.
</p>

<p style="margin:0 0 14px 0; line-height:1.6;">
  Use their referral code at signup and you'll receive a <strong>$100 bonus credit</strong> on your account.
</p>

<div style="margin:16px 0; padding:14px; border-radius:10px; background:#f5f2ff; text-align:center;">
  <div style="font-size:12px; text-transform:uppercase; letter-spacing:0.08em; color:#6b21a8;">
    Your Referral Code
  </div>
  <div style="font-size:26px; font-weight:800; margin-top:6px; color:#4a148c;">
    {referral_code}
  </div>
</div>

<div style="text-align:center; margin:18px 0 10px 0;">
  <a href="{register_link}"
     style="display:inline-block; padding:12px 22px; background:#4A148C; color:#ffffff;
            text-decoration:none; border-radius:999px; font-weight:700;">
    Sign Up & Claim Your $100
  </a>
</div>

<p style="margin:0; font-size:13px; color:#6b7280;">
  Just enter the code above when creating your account.
</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  READY FOR PICK UP (BODY ONLY)
# ==========================================================
def send_ready_for_pickup_email(to_email: str, full_name: str, items: list[dict], recipient_user_id=None) -> bool:
    rows = "\n".join(
        f"- {it.get('tracking_number','-')} | {it.get('description','-')} | Amount Due: ${it.get('amount_due',0):.2f}"
        for it in items
    ) or "- (no details)"

    plain = (
        f"Hi {full_name},\n\n"
        f"The following package(s) are now READY FOR PICK UP:\n"
        f"{rows}\n\n"
        f"Prefer delivery? Schedule it here: {DELIVERY_URL}\n\n"
        f"Thanks,\nForeign A Foot Logistics"
    )

    html_list = "".join(
        f"<li>{it.get('tracking_number','-')} — {it.get('description','-')} — ${it.get('amount_due',0):.2f}</li>"
        for it in items
    ) or "<li>(no details)</li>"

    html = f"""
<p style="margin:0 0 10px 0;">Hi {full_name},</p>
<p style="margin:0 0 10px 0;">The following package(s) are now <b>READY FOR PICK UP</b>:</p>
<ul style="margin:0 0 14px 18px; padding:0;">
  {html_list}
</ul>
<p style="margin:0 0 14px 0;">
  <a href="{DELIVERY_URL}" style="background:#4A148C;color:#fff;padding:10px 16px;text-decoration:none;border-radius:6px;display:inline-block;">
    Schedule Delivery
  </a>
</p>
<p style="margin:0;">Thanks,<br>Foreign A Foot Logistics</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject="Your package is Ready for Pick Up",
        plain_body=plain,
        html_body=html,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  SHIPMENT INVOICE NOTICE (BODY ONLY)
# ==========================================================
def send_shipment_invoice_link_email(to_email: str, full_name: str, total_due: float, invoice_link: str, recipient_user_id=None) -> bool:
    plain = (
        f"Hi {full_name},\n\n"
        f"Your invoice for the recent shipment is ready.\n"
        f"Total Amount Due: ${total_due:.2f}\n\n"
        f"View/pay here: {invoice_link}\n\n"
        f"Thanks,\nForeign A Foot Logistics"
    )

    html = f"""
<p style="margin:0 0 10px 0;">Hi {full_name},</p>
<p style="margin:0 0 10px 0;">Your invoice for the recent shipment is ready.</p>
<p style="margin:0 0 14px 0;"><b>Total Amount Due:</b> ${total_due:.2f}</p>
<p style="margin:0 0 14px 0;">
  <a href="{invoice_link}" style="background:#4A148C;color:#fff;padding:10px 16px;text-decoration:none;border-radius:6px;display:inline-block;">
    View / Pay Invoice
  </a>
</p>
<p style="margin:0;">Thanks,<br>Foreign A Foot Logistics</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject="Your Shipment Invoice",
        plain_body=plain,
        html_body=html,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


# ==========================================================
#  COMPOSE READY PICKUP (subject, plain, html BODY ONLY)
# ==========================================================
def compose_ready_pickup_email(full_name: str, packages: Iterable[dict]):
    """
    packages: iterable of dicts with keys:
      shipper/vendor, house_awb, tracking_number, weight
    Returns: (subject, plain_body, html_body_body_only)
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
  <td style="padding:8px; border:1px solid #eee; color:#16a34a; font-weight:700;">
    Ready for Pickup/Delivery
  </td>
</tr>
""")

    plain_body = (
        f"Hi {full_name},\n\n"
        "Good news—your package(s) with Foreign A Foot Logistics Limited "
        "are ready for pickup or delivery.\n\n"
        "Below is a quick summary:\n\n"
        + ("\n".join(rows_txt) if rows_txt else "(No package details)\n")
        + "\n"
        "Pickup Location:\n"
        "Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine\n"
        "Hours: Mon–Sat, 9:00 AM – 6:00 PM\n"
        "Contact: (876) 560-7764 | foreignafootlogistics@gmail.com\n\n"
        "🚚 Prefer delivery?\n"
        f"Log in and schedule delivery here:\n{DELIVERY_URL}\n\n"
        "Thanks for shipping with us,\n"
        "Foreign A Foot Logistics Limited\n"
    )

    html_body = f"""
<h2 style="color:#4a148c; margin:0 0 12px 0;">Great news—your package(s) are ready!</h2>

<p style="margin:0 0 10px 0;">We’ve prepared the following item(s) for pickup or delivery:</p>

<table style="width:100%; border-collapse:collapse; margin:12px 0;">
  <thead>
    <tr style="background:#f3ecff; color:#4a148c;">
      <th style="padding:8px; border:1px solid #eee; text-align:left;">Shipper</th>
      <th style="padding:8px; border:1px solid #eee; text-align:left;">Airway Bill</th>
      <th style="padding:8px; border:1px solid #eee; text-align:left;">Tracking #</th>
      <th style="padding:8px; border:1px solid #eee; text-align:center;">Weight (lbs)</th>
      <th style="padding:8px; border:1px solid #eee; text-align:left;">Status</th>
    </tr>
  </thead>
  <tbody>
    {''.join(rows_html) if rows_html else '<tr><td colspan="5" style="padding:8px; border:1px solid #eee;">No package details</td></tr>'}
  </tbody>
</table>

<div style="margin-top:14px;">
  <p style="margin:0 0 6px 0;"><strong>Pickup Location:</strong> Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine</p>
  <p style="margin:0 0 6px 0;"><strong>Hours:</strong> Mon–Sat, 9:00 AM – 6:00 PM</p>
  <p style="margin:0;"><strong>Contact:</strong> (876) 560-7764 · foreignafootlogistics@gmail.com</p>
</div>

<div style="margin-top:18px; text-align:center;">
  <a href="{DELIVERY_URL}"
     style="display:inline-block; background:#4a148c; color:#ffffff; padding:12px 20px;
            text-decoration:none; border-radius:6px; font-weight:700;">
    🚚 Schedule Delivery
  </a>
</div>
""".strip()

    return subject, plain_body, html_body


# ==========================================================
#  INVOICE REQUEST (BODY ONLY)
# ==========================================================
def send_invoice_request_email(to_email, full_name, packages, recipient_user_id=None):
    """
    Email: "Please provide invoices for the following packages"
    packages: list[dict] or list[ORM] with keys/attrs:
      shipper/vendor, house_awb, tracking_number, weight, status
    """
    first_name = (full_name or "Customer").split()[0]
    subject = "Invoice Request - Foreign A Foot Logistics Limited"
    upload_url = f"{DASHBOARD_URL}/customer/packages"

    def getp(p, key, default=None):
        if isinstance(p, dict):
            return p.get(key, default)
        return getattr(p, key, default)

    blocks_txt = []
    blocks_html = []

    for p in packages:
        shipper = getp(p, "shipper") or getp(p, "vendor") or "-"
        house = getp(p, "house_awb") or getp(p, "house") or "-"
        tracking = getp(p, "tracking_number") or getp(p, "tracking") or "-"
        weight = getp(p, "weight") or 0
        status = getp(p, "status") or "At Overseas Warehouse"

        blocks_txt.append(
            f"Shipper: {shipper}\n"
            f"Airway Bill: {house}\n"
            f"Tracking Number: {tracking}\n"
            f"Weight: {weight}\n"
            f"Status: {status}\n"
        )

        blocks_html.append(f"""
<div style="margin:14px 0; padding:12px; border:1px solid #e5e7eb; border-radius:10px; background:#ffffff;">
  <div><b>Shipper:</b> {shipper}</div>
  <div><b>Airway Bill:</b> {house}</div>
  <div><b>Tracking Number:</b> {tracking}</div>
  <div><b>Weight:</b> {weight}</div>
  <div><b>Status:</b> {status}</div>
</div>
""")

    plain_body = (
        f"Hi {first_name},\n\n"
        f"Please provide Foreign A Foot Logistics Limited with invoices for the following packages.\n\n"
        + "\n\n".join(blocks_txt)
        + "\n\n"
        "Customs requires a proper invoice for all packages. Packages without a proper invoice may result in delays and/or additional storage costs.\n\n"
        f"Provide Invoice(s) Now: {upload_url}\n"
    )

    html_body = f"""
<p style="margin:0 0 12px 0;">Hi {first_name},</p>

<p style="margin:0 0 14px 0;">
  Please provide <b>Foreign A Foot Logistics Limited</b> with invoices for the following packages:
</p>

{''.join(blocks_html)}

<p style="margin:14px 0 16px 0; color:#111827;">
  <b>Customs requires a proper invoice for all packages.</b>
  Packages without a proper invoice may result in delays and/or additional storage costs.
</p>

<p style="margin:0;">
  <a href="{upload_url}"
     style="display:inline-block; background:#ef4444; color:#fff; text-decoration:none;
            padding:12px 18px; font-weight:800; border-radius:8px;">
    Provide Invoice(s) Now
  </a>
</p>
""".strip()

    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        reply_to=EMAIL_FROM or EMAIL_ADDRESS,
    )


import os
import requests

def send_email_sendgrid_api(
    to_email: str,
    subject: str,
    plain_body: str,
    html_body: str | None = None,
    from_email: str | None = None,
    from_name: str = "Foreign A Foot Logistics Limited",
    reply_to: str = "support@faflcourier.com",
    category: str = "customer-portal",
):
    """
    Send email using SendGrid Web API (bulk-friendly).
    Notes:
      - Uses SENDGRID_API_KEY (preferred) or SMTP_PASS fallback.
      - Adds List-Unsubscribe (mailto) header.
      - Disables click tracking (often helps deliverability for reset-link campaigns).
    Returns:
      - True on success
      - False on failure (prints reason)
    """

    api_key = (os.getenv("SENDGRID_API_KEY") or os.getenv("SMTP_PASS") or "").strip()
    if not api_key:
        print("❌ SendGrid API key missing. Set SENDGRID_API_KEY (or SMTP_PASS fallback).")
        return False

    to_email = (to_email or "").strip().lower()
    if not to_email:
        return False

    resolved_from_email = (
        (from_email or "").strip()
        or (os.getenv("SMTP_FROM") or "").strip()
        or (os.getenv("SMTP_USER") or "").strip()
        or "support@faflcourier.com"
    )

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    list_unsub = f"<mailto:{reply_to}?subject=Unsubscribe>"

    payload = {
        "personalizations": [
            {
                "to": [{"email": to_email}],
                "subject": (subject or "").strip(),
                "custom_args": {"app": "fafl", "category": category},
            }
        ],
        "from": {"email": resolved_from_email, "name": from_name},
        "reply_to": {"email": reply_to, "name": "FAFL Support"},
        "headers": {"List-Unsubscribe": list_unsub},
        "categories": [category],
        "tracking_settings": {
            "click_tracking": {"enable": False, "enable_text": False},
            "open_tracking": {"enable": True},
        },
        "content": [
            {"type": "text/plain", "value": (plain_body or "").strip()},
        ],
    }

    if html_body:
        payload["content"].append({"type": "text/html", "value": (html_body or "").strip()})

    try:
        r = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers=headers,
            json=payload,
            timeout=30,
        )

        if r.status_code in (200, 202):
            print(f"✅ SendGrid API sent to {to_email}")
            return True

        # IMPORTANT: if auth fails, DO NOT keep retrying in your bulk script
        if r.status_code in (401, 403):
            print(f"❌ SendGrid AUTH error {r.status_code}: {r.text}")
            return False

        print(f"❌ SendGrid API error {r.status_code}: {r.text}")
        return False

    except Exception as e:
        print(f"❌ SendGrid API exception for {to_email}: {e}")
        return False
