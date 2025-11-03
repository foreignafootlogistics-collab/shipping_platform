import os
import smtplib
from math import ceil
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import current_app
from app.config import LOGO_URL
from math import ceil
from typing import Iterable


# If you still want Flask-Mail for some cases:
try:
    from flask_mail import Message
    from app import mail  # only used in send_referral_email fallback
    _HAS_FLASK_MAIL = True
except Exception:
    _HAS_FLASK_MAIL = False

# ==========================================================
#  EMAIL CONFIG (use environment variables in production)
# ==========================================================
EMAIL_ADDRESS = os.getenv("FAF_EMAIL", "foreignafootlogistics@gmail.com")
EMAIL_PASSWORD = os.getenv("FAF_EMAIL_PASSWORD", "psudlguoqqkoiapu")
LOGO_URL = os.getenv("FAF_LOGO_URL", "https://yourdomain.com/static/faf_logo.png")  # update
DASHBOARD_URL = os.getenv("FAF_DASHBOARD_URL", "https://www.foreignafoot.com")

# ==========================================================
#  CORE EMAIL FUNCTION (SMTP - Gmail)
# ==========================================================
def send_email(to_email: str, subject: str, plain_body: str, html_body: str | None = None) -> bool:
    """
    Send an email via Gmail SMTP with optional HTML content.
    Always includes a plaintext fallback for deliverability.
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = EMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject

    # Plain text (required)
    msg.attach(MIMEText(plain_body, "plain"))

    # HTML (optional)
    if html_body:
        msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.send_message(msg)
        print(f"✅ Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"❌ Email sending failed to {to_email}: {e}")
        return False

# ==========================================================
#  WELCOME EMAIL
# ==========================================================
def send_welcome_email(email, full_name, reg_number):
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
            <p><a href="{DASHBOARD_URL}" style="background:#5c3d91;color:#fff;padding:10px 20px;text-decoration:none;border-radius:4px;">Login to Dashboard</a></p>
        </div>
    </div>
</body>
</html>
"""
    return send_email(email, "Welcome to Foreign A Foot Logistics Limited! 🚛✈️", plain_body, html_body)

# ==========================================================
#  PASSWORD RESET EMAIL
# ==========================================================
def send_password_reset_email(to_email, full_name, reset_link):
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
    <p><a href="{reset_link}" style="background:#5c3d91;color:#fff;padding:10px 20px;text-decoration:none;">Reset Password</a></p>
    <p>This link will expire in 10 minutes.</p>
</body>
</html>
"""
    return send_email(to_email, "Reset Your Password - Foreign A Foot Logistics", plain_body, html_body)

# ==========================================================
#  BULK MESSAGE EMAIL
# ==========================================================
def send_bulk_message_email(to_email, full_name, subject, message_body):
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
            <p>{message_body.replace("\n", "<br>")}</p>
            <p>Best regards,<br>Foreign A Foot Logistics Team</p>
        </div>
    </div>
</body>
</html>
"""
    return send_email(to_email, f"{subject} - Foreign A Foot Logistics", plain_body, html_body)

# ==========================================================
#  PACKAGE UPLOAD EMAIL (status summary)
# ==========================================================
def send_package_upload_email(recipient_email, first_name, packages):
    rows = ""
    for p in packages:
        style = "color:#000;"
        note = ""
        status = (p.get('status') or '').lower()
        if status == "overseas":
            style = "color:#d97706; font-weight:bold;"
            note = "<br><small>⚠️ Please upload a proper invoice to avoid customs delays.</small>"
        elif status == "ready for pick up":
            style = "color:#16a34a; font-weight:bold;"
            note = "<br><small>✅ Your package is ready for pick up.</small>"
        rows += f"""
        <tr>
            <td style="border:1px solid #ddd; padding:8px;">{p.get('description','-')}</td>
            <td style="border:1px solid #ddd; padding:8px;">{p.get('tracking_number','-')}</td>
            <td style="border:1px solid #ddd; padding:8px;">{p.get('house_awb','N/A')}</td>
            <td style="border:1px solid #ddd; padding:8px;">{p.get('weight',0)} lbs</td>
            <td style="border:1px solid #ddd; padding:8px; {style}">{p.get('status','-')}{note}</td>
        </tr>
        """
    plain_body = "You have new package updates. Please log in to view."
    html_body = f"""
<html>
<body style="font-family: Arial, sans-serif; background:#f9f9f9; padding:20px;">
    <div style="max-width:600px; margin:auto; background:#fff; padding:20px; border-radius:8px;">
        <h2>Hello {first_name},</h2>
        <p>Here are your latest package updates:</p>
        <table style="width:100%; border-collapse: collapse;">
            <tr style="background:#5c3d91; color:#fff;">
                <th>Description</th><th>Tracking #</th><th>House AWB</th><th>Weight</th><th>Status</th>
            </tr>
            {rows}
        </table>
    </div>
</body>
</html>
"""
    return send_email(recipient_email, "📦 New Package Notification - Foreign A Foot Logistics", plain_body, html_body)

# ==========================================================
#  INVOICE EMAIL
# ==========================================================
def send_invoice_email(to_email, full_name, invoice_number, amount_due, due_date, invoice_link):
    plain_body = f"""
Dear {full_name},

Here is your invoice:

Invoice #: {invoice_number}
Amount Due: ${amount_due} JMD
Due Date: {due_date}

You can view/download your invoice here:
{invoice_link}
"""
    html_body = f"""
<html>
<body>
    <p>Dear {full_name},</p>
    <p>Here is your invoice:</p>
    <p><b>Invoice #:</b> {invoice_number}</p>
    <p><b>Amount Due:</b> ${amount_due} JMD</p>
    <p><b>Due Date:</b> {due_date}</p>
    <p><a href="{invoice_link}" style="background:#5c3d91;color:#fff;padding:10px 20px;text-decoration:none;">View Invoice</a></p>
</body>
</html>
"""
    return send_email(to_email, f"📄 Invoice #{invoice_number} - Foreign A Foot Logistics", plain_body, html_body)

# ==========================================================
#  NEW MESSAGE EMAIL (bugfix: use plain_body kwarg)
# ==========================================================
def send_new_message_email(user_email, user_name, message_subject, message_body):
    subject = f"New Message: {message_subject}"
    body = (
        f"Hello {user_name},\n\n"
        f"You have received a new message:\n\n"
        f"{message_body}\n\n"
        f"Please log in to your account to reply or view details."
    )
    return send_email(to_email=user_email, subject=subject, plain_body=body)

# ==========================================================
#  REFERRAL EMAIL (use current_app safely; Flask-Mail if available)
# ==========================================================
def send_referral_email(to_email, referral_code, referrer_name):
    base_url = (current_app.config.get("BASE_URL") if current_app else "https://www.foreignafoot.com").rstrip("/")
    sender = (current_app.config.get("MAIL_DEFAULT_SENDER") if current_app else EMAIL_ADDRESS)

    subject = "You've been invited to join Foreign A Foot Logistics!"
    body = f"""
Hi there,

Your friend {referrer_name} has invited you to join Foreign A Foot Logistics!

Use their referral code during registration to get a $100 signup bonus.

Your referral code: {referral_code}

Register here: {base_url}/register?ref={referral_code}

Thanks,
Foreign A Foot Logistics Team
"""

    # Prefer Flask-Mail if configured, otherwise fallback to SMTP
    if _HAS_FLASK_MAIL and current_app:
        try:
            msg = Message(subject, sender=sender, recipients=[to_email])
            msg.body = body
            mail.send(msg)
            return True
        except Exception:
            pass  # fallback to SMTP below

    return send_email(to_email=to_email, subject=subject, plain_body=body)

# ==========================================================
#  OVERSEAS PACKAGE RECEIVED (NEW for bulk action)
#  Includes: House AWB, Rounded-up Weight, Tracking #, Description
#  Subject: Foreign A Foot Logistics Limited received a new package overseas for FAFL #<reg_number>
# ==========================================================
from math import ceil

try:
    from app.config import LOGO_URL
except ImportError:
    LOGO_URL = "https://www.foreignafoot.com/app/static/logo.png"  # fallback

def send_overseas_received_email(to_email, full_name, reg_number, packages):
    """
    Sends an email when Foreign A Foot Logistics Limited receives a new package overseas.
    Includes: House AWB, Rounded-up Weight, Tracking #, Description, and Status.
    """

    subject = f"Foreign A Foot Logistics Limited received a new package overseas for FAFL #{reg_number}"

    # Build table rows
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

    # -------------------------------
    # Plain-text fallback
    # -------------------------------
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
            f"- House AWB: {house or '-'}, Rounded Weight (lbs): {ceil(weight or 0)}, "
            f"Tracking #: {tracking or '-'}, Description: {desc or '-'}, Status: {status or 'Overseas'}"
        )

    plain_lines += [
        "",
        "Please note that Customs requires a proper invoice for all packages.",
        "To avoid any delays, kindly upload or send your invoice as soon as possible.",
        "",
        "Thank you for choosing Foreign A Foot Logistics Limited — your trusted logistics partner!",
        "",
        "Warm regards,",
        "The Foreign A Foot Logistics Team",
        "📍 Cedar Grove PassageFort Portmore",
        "🌐 www.foreignafoot.com",
        "✉️ foreignafootlogistics@gmail.com",
        "☎️ (876) 123-4567",
    ]
    plain_body = "\n".join(plain_lines)

    # -------------------------------
    # HTML version
    # -------------------------------
    html_body = f"""
    <html>
    <body style="font-family:Inter,Arial,sans-serif; line-height:1.6; color:#222;">
      <div style="max-width:700px;margin:0 auto;padding:16px;">
        <img src="{LOGO_URL}" alt="Foreign A Foot Logistics" style="max-width:180px; margin-bottom:16px;">
        <p>Hello {full_name},</p>
        <p>Great news- we’ve received a new package overseas for you. Package Details:</p>

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
          Please note that Customs requires a proper invoice for all packages.<br>
          To avoid any delays, kindly upload or send your invoice as soon as possible.
        </p>

        <p style="margin-top:24px; font-weight:500;">
          Thank you for choosing Foreign A Foot Logistics Limited — your trusted logistics partner!
        </p>

        <hr style="margin:28px 0; border:none; border-top:1px solid #ddd;">
        <footer style="font-size:14px; color:#555;">
          <p style="margin:4px 0;">Warm regards,<br><strong>The Foreign A Foot Logistics Team</strong></p>
          <p style="margin:4px 0;">📍 Cedar Grove PassageFort Portmore</p>
          <p style="margin:4px 0;">🌐 <a href="https://www.foreignafoot.com" style="color:#4a148c;text-decoration:none;">www.foreignafoot.com</a></p>
          <p style="margin:4px 0;">✉️ <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4a148c;text-decoration:none;">foreignafootlogistics@gmail.com</a>
          <p style="margin:4px 0;">☎️ (876) 210-4291</p>
        </footer>
      </div>
    </body>
    </html>
    """

    from app.utils.email_utils import send_email  # internal import to avoid circulars
    return send_email(to_email, subject, plain_body, html_body)

# --- Ready for Pick Up (no attachments) -------------------
def send_ready_for_pickup_email(to_email: str, full_name: str, items: list[dict]) -> bool:
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
    return send_email(to_email, "Your package is Ready for Pick Up", plain, html)

# --- Shipment invoice notice (no attachments; link/button only) ----
def send_shipment_invoice_link_email(to_email: str, full_name: str, total_due: float, invoice_link: str) -> bool:
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
      <p><a href="{invoice_link}" style="background:#5c3d91;color:#fff;padding:10px 16px;text-decoration:none;border-radius:4px;">
        View / Pay Invoice
      </a></p>
      <p>Thanks,<br>Foreign A Foot Logistics</p>
    </body></html>
    """
    return send_email(to_email, "Your Shipment Invoice", plain, html)

def compose_ready_pickup_email(full_name: str, packages: Iterable[dict]):
    """
    packages: iterable of dicts with keys:
      shipper, house_awb, tracking_number, weight
    Returns: (subject, plain_body, html_body)
    """
    subject = "Your package(s) are ready for pickup 🎉"

    # Normalize/round and build rows
    rows_txt = []
    rows_html = []
    for p in packages:
        shipper = (p.get("shipper") or p.get("vendor") or "—")
        awb     = p.get("house_awb") or p.get("house") or "—"
        track   = p.get("tracking_number") or p.get("tracking") or "—"
        w_raw   = p.get("weight") or 0
        w_up    = ceil(float(w_raw) or 0)

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

    # Plain
    plain_body = (
        f"Hi {full_name},\n\n"
        f"Good news—your package(s) with Foreign A Foot Logistics Limited are ready for pickup/delivery.\n\n"
        f"Below is a quick summary:\n\n"
        + ("\n".join(rows_txt) if rows_txt else "(No package details)\n")
        + "\n"
        "Pickup Location: Cedar Grove, Passage Fort, Portmore\n"
        "Hours: Mon–Sat, 9:00 AM – 6:00 PM\n"
        "Contact: (876) 210-4291 | foreignafootlogistics@gmail.com\n\n"
        "If you need delivery, just reply to this email and we’ll arrange it.\n\n"
        "Thanks for shipping with us,\n"
        "Foreign A Foot Logistics Limited\n"
    )

    # HTML
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
            <p style="margin:4px 0;"><strong>Contact:</strong> (876) 210-4291 · foreignafootlogistics@gmail.com</p>
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
