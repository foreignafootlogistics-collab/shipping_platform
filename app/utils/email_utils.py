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
try:
    from app.config import LOGO_URL
except ImportError:
    LOGO_URL = "https://www.foreignafoot.com/app/static/logo.png"  # fallback


def send_overseas_received_email(to_email, full_name, reg_number, packages):
    """
    Sends an email when Foreign A Foot Logistics Limited receives a new package overseas.
    Includes: House AWB, Rounded-up Weight, Tracking #, Description, Status
    AND a button/link for the customer to upload their invoice.
    """

    subject = f"Foreign A Foot Logistics Limited received a new package overseas for FAFL #{reg_number}"

    # URL where customer can upload invoices (adjust path if needed)
    base_url = DASHBOARD_URL.rstrip("/")  # e.g. https://www.foreignafoot.com
    upload_url = f"{base_url}/customer/packages"   # customer package page

    # Build table rows (HTML)
    rows_html = []
    for p in packages:
        house = getattr(p, "house_awb", None) if not isinstance(p, dict) else p.get("house_awb")
        weight = getattr(p, "weight", 0)        if not isinstance(p, dict) else p.get("weight", 0)
        tracking = getattr(p, "tracking_number", None) if not isinstance(p, dict) else p.get("tracking_number")
        desc = getattr(p, "description", None)  if not isinstance(p, dict) else p.get("description")
        status = getattr(p, "status", None)     if not isinstance(p, dict) else p.get("status")
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
        weight = getattr(p, "weight", 0)        if not isinstance(p, dict) else p.get("weight", 0)
        tracking = getattr(p, "tracking_number", None) if not isinstance(p, dict) else p.get("tracking_number")
        desc = getattr(p, "description", None)  if not isinstance(p, dict) else p.get("description")
        status = getattr(p, "status", None)     if not isinstance(p, dict) else p.get("status")
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
        "📍 Cedar Grove PassageFort Portmore",
        "🌐 www.foreignafoot.com",
        "✉️ foreignafootlogistics@gmail.com",
        "☎️ (876) 210-4291",
    ]
    plain_body = "\n".join(plain_lines)

    # -------------------------------
    # HTML version (with button)
    # -------------------------------
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

        <!-- 🔗 Upload invoice button -->
        <p style="margin-top:18px;">
          <a href="{upload_url}"
             style="display:inline-block; padding:10px 22px; background:#4a148c; color:#ffffff;
                    text-decoration:none; border-radius:6px; font-weight:600;">
            Upload / Add Your Invoice
          </a>
        </p>
        <p style="font-size:13px; color:#555; margin-top:6px;">
          Or visit
          <a href="{upload_url}" style="color:#4a148c; text-decoration:none;">{upload_url}</a>
          and locate this package by tracking number.
        </p>

        <hr style="margin:28px 0; border:none; border-top:1px solid #ddd;">
        <footer style="font-size:14px; color:#555;">
          <p style="margin:4px 0;">Warm regards,<br><strong>The Foreign A Foot Logistics Team</strong></p>
          <p style="margin:4px 0;">📍 Cedar Grove PassageFort Portmore</p>
          <p style="margin:4px 0;">🌐 <a href="https://www.foreignafoot.com"
                style="color:#4a148c;text-decoration:none;">www.foreignafoot.com</a></p>
          <p style="margin:4px 0;">✉️ <a href="mailto:foreignafootlogistics@gmail.com"
                style="color:#4a148c;text-decoration:none;">foreignafootlogistics@gmail.com</a></p>
          <p style="margin:4px 0;">☎️ (876) 210-4291</p>
        </footer>
      </div>
    </body>
    </html>
    """

    # ✅ use the send_email helper defined at the top of this file
    return send_email(to_email, subject, plain_body, html_body)

# ==========================================================
#  INVOICE EMAIL
# ==========================================================
def send_invoice_email(to_email, full_name, invoice_number, amount_due, invoice_link):
    """
    Sends a clean, branded FAFL invoice email without due date.
    """

    # -------- Plain text fallback --------
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

    # -------- HTML version --------
    html_body = f"""
<html>
  <body style="font-family: Inter, Arial, sans-serif; background:#f8f8fc; padding:0; margin:0;">

    <div style="max-width:640px; margin:20px auto; background:#ffffff; 
                border-radius:12px; padding:30px; box-shadow:0 4px 20px rgba(0,0,0,0.06);">

      <!-- Logo -->
      <div style="text-align:center; margin-bottom:20px;">
        <img src="{LOGO_URL}" alt="FAFL Logo" style="max-width:180px;">
      </div>

      <!-- Header -->
      <h2 style="text-align:center; color:#4a148c; margin-top:0;">
        Your Invoice is Ready
      </h2>

      <p style="color:#444; font-size:15px;">
        Hello <strong>{full_name}</strong>,<br><br>
        Your invoice from <strong>Foreign A Foot Logistics Limited</strong> has been generated and is now available.
        You may review the details below and click the button to view or download the full invoice.
      </p>

      <!-- Invoice Summary -->
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

      <!-- Button -->
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

      <!-- Footer -->
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
    )

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
def send_referral_email(to_email: str, referral_code: str, referrer_name: str) -> bool:
    """
    Sends a nice branded referral invite email.

    Uses the core send_email() SMTP helper so it works with your Gmail
    app password config.
    """
    base_url = (
        current_app.config.get("BASE_URL")
        if current_app else "https://www.foreignafoot.com"
    ).rstrip("/")

    register_link = f"{base_url}/register?ref={referral_code}"

    subject = "You've been invited to join Foreign A Foot Logistics!"

    # -------- Plain text (fallback) ----------
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

    # -------- HTML version ----------
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

    # Use your SMTP helper (works with Gmail app password)
    return send_email(to_email=to_email, subject=subject, plain_body=plain_body, html_body=html_body)


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
