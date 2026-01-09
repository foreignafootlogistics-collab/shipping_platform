import os
from datetime import datetime
import smtplib
from math import ceil
from typing import Iterable
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from flask import current_app
from flask import render_template
from email.message import EmailMessage

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
DELIVERY_URL = "https://app.faflcourier.com/customer/schedule-delivery"
TRANSACTIONS_URL = "https://app.faflcourier.com/customer/transactions/all"

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
          Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine<br>
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
  <td style="padding:8px; border:1px solid #eee; color:#16a34a; font-weight:600;">
    Ready for Pickup/Delivery
  </td>
</tr>
""")

    # -------------------------
    # Plain-text email
    # -------------------------
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

    # -------------------------
    # HTML email
    # -------------------------
    html_body = f"""
<html>
  <body style="font-family:Arial, sans-serif; color:#222;">
    <div style="max-width:680px; margin:0 auto; padding:16px;">
      <h2 style="color:#4a148c; margin-bottom:12px;">
        Great news—your package(s) are ready!
      </h2>

      <p>We’ve prepared the following item(s) for pickup or delivery:</p>

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
          {''.join(rows_html) if rows_html else
            '<tr><td colspan="5" style="padding:8px; border:1px solid #eee;">No package details</td></tr>'}
        </tbody>
      </table>

      <div style="margin-top:16px;">
        <p><strong>Pickup Location:</strong> Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine</p>
        <p><strong>Hours:</strong> Mon–Sat, 9:00 AM – 6:00 PM</p>
        <p><strong>Contact:</strong> (876) 560-7764 · foreignafootlogistics@gmail.com</p>
      </div>

      <div style="margin-top:20px; text-align:center;">
        <a href="{DELIVERY_URL}"
           style="
             display:inline-block;
             background:#4a148c;
             color:#ffffff;
             padding:12px 20px;
             text-decoration:none;
             border-radius:6px;
             font-weight:600;
           ">
          🚚 Schedule Delivery
        </a>
      </div>

      <p style="margin-top:20px; color:#555;">
        Thanks for choosing <strong>Foreign A Foot Logistics Limited</strong>.
      </p>
    </div>
  </body>
</html>
"""

    return subject, plain_body, html_body

def send_invoice_request_email(to_email, full_name, packages, recipient_user_id=None):
    """
    Email: "Please provide invoices for the following packages"
    Matches your screenshot style: simple blocks + customs note + red button.
    packages: list[dict] or list[ORM] with keys/attrs:
      shipper/vendor, house_awb, tracking_number, weight, status
    """
    first_name = (full_name or "Customer").split()[0]
    subject = "Invoice Request - Foreign A Foot Logistics Limited"

    upload_url = f"{DASHBOARD_URL}/customer/packages"

    # -------- helpers to read dict OR ORM ----------
    def getp(p, key, default=None):
        if isinstance(p, dict):
            return p.get(key, default)
        return getattr(p, key, default)

    # Build PLAIN blocks
    blocks_txt = []
    for p in packages:
        shipper  = getp(p, "shipper") or getp(p, "vendor") or "-"
        house    = getp(p, "house_awb") or getp(p, "house") or "-"
        tracking = getp(p, "tracking_number") or getp(p, "tracking") or "-"
        weight   = getp(p, "weight") or 0
        status   = getp(p, "status") or "At Overseas Warehouse"

        blocks_txt.append(
            f"Shipper: {shipper}\n"
            f"Airway Bill: {house}\n"
            f"Tracking Number: {tracking}\n"
            f"Weight: {int(weight) if str(weight).isdigit() else weight}\n"
            f"Status: {status}\n"
        )

    plain_body = (
        f"Hi {first_name},\n\n"
        f"Please provide Foreign a Foot Logistics Limited with invoices for the following packages.\n\n"
        + "\n\n".join(blocks_txt)
        + "\n\n"
        "Customs requires a proper invoice for all packages. Packages without a proper invoices will result in delays and/or additional storage costs.\n\n"
        f"Provide Invoice(s) Now: {upload_url}\n"
    )

    # Build HTML blocks (screenshot-style)
    blocks_html = []
    for p in packages:
        shipper  = getp(p, "shipper") or getp(p, "vendor") or "-"
        house    = getp(p, "house_awb") or getp(p, "house") or "-"
        tracking = getp(p, "tracking_number") or getp(p, "tracking") or "-"
        weight   = getp(p, "weight") or 0
        status   = getp(p, "status") or "At Overseas Warehouse"

        blocks_html.append(f"""
          <div style="margin:18px 0;">
            <div><b>Shipper:</b> {shipper}</div>
            <div><b>Airway Bill:</b> {house}</div>
            <div><b>Tracking Number:</b> {tracking}</div>
            <div><b>Weight:</b> {weight}</div>
            <div><b>Status:</b> {status}</div>
          </div>
        """)

    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; color:#111; background:#fff; margin:0; padding:0;">
    <div style="max-width:900px; padding:24px 28px;">
      <p style="margin:0 0 14px;">Hi {first_name},</p>

      <p style="margin:0 0 18px;">
        Please provide Foreign a Foot Logistics Limited with invoices for the following packages.
      </p>

      {''.join(blocks_html)}

      <p style="margin:18px 0 22px;">
        <b>Customs requires a proper invoice for all packages. Packages without a proper invoices will result in delays and/or additional storage costs.</b>
      </p>

      <a href="{upload_url}"
         style="display:inline-block; background:#ef4444; color:#fff; text-decoration:none;
                padding:12px 18px; font-weight:700; border-radius:2px;">
        Provide Invoice(s) Now
      </a>

      <div style="margin-top:28px;">
        <img src="{LOGO_URL}" alt="FAFL" style="width:26px; height:auto;">
      </div>
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

