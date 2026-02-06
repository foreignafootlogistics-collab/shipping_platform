import os
from datetime import datetime, timezone
import smtplib
from math import ceil
from typing import Iterable
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from flask import current_app
from flask import render_template
from markupsafe import escape


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
#  FAFL BRANDED BASE EMAIL (SINGLE SOURCE OF TRUTH)
# ==========================================================
def render_fafl_email(full_name: str, main_message: str,
                      action_url: str | None = None,
                      action_text: str | None = None) -> str:
    """
    Returns a fully branded HTML email using your FAFL template
    — logo beside header (no purple bar) and logo again in footer.
    """
    return f"""
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body style="margin:0; padding:0; background:#f3f4f6; font-family:Arial, Helvetica, sans-serif; color:#111827;">

  <div style="width:100%; padding:24px 0;">
    <div style="max-width:640px; margin:0 auto; background:#ffffff; border-radius:12px;
                overflow:hidden; box-shadow:0 4px 12px rgba(0,0,0,0.05);">

      <!-- HEADER (logo + title on same line) -->
      <div style="padding:20px 24px; display:flex; align-items:center; gap:14px; border-bottom:1px solid #e5e7eb;">
        <img src="{LOGO_URL}" alt="Foreign A Foot Logistics"
             style="height:48px; width:auto; display:block;">

        <div style="font-size:18px; font-weight:700; color:#4A148C;">
          Foreign A Foot Logistics Limited
        </div>
      </div>

      <!-- BODY -->
      <div style="padding:24px 28px; line-height:1.6;">
        <div style="font-size:20px; font-weight:700; color:#4A148C; margin-bottom:12px;">
          Hello {full_name},
        </div>

        <p>{main_message}</p>

        {f'''
        <p style="text-align:center; margin-top:16px;">
          <a href="{action_url}"
             style="display:inline-block; background:#4A148C; color:#ffffff;
                    padding:12px 22px; text-decoration:none; border-radius:8px;
                    font-weight:600;">
            {action_text}
          </a>
        </p>
        ''' if action_url and action_text else ""}

        <p>
          Best regards,<br>
          <strong>Foreign A Foot Logistics Team</strong>
        </p>
      </div>

      <!-- FOOTER (logo + details) -->
      <div style="background:#f5f2fb; padding:18px 24px; font-size:13px; color:#555; text-align:center;">

        <div style="display:flex; justify-content:center; align-items:center; gap:10px; margin-bottom:8px;">
          <img src="{LOGO_URL}" alt="FAFL Logo"
               style="height:32px; width:auto; display:block;">
          <strong>Foreign A Foot Logistics Limited</strong>
        </div>

        <div>Unit 7, Lot C22, Cedar Manor, Gregory Park, St. Catherine, Jamaica</div>

        <div style="margin-top:6px;">
          📞 (876) 560-7764 ·
          ✉️ <a href="mailto:foreignafootlogistics@gmail.com" style="color:#4A148C; text-decoration:none;">
            foreignafootlogistics@gmail.com
          </a> ·
          🌐 <a href="https://app.faflcourier.com" style="color:#4A148C; text-decoration:none;">
            app.faflcourier.com
          </a>
        </div>
      </div>

    </div>
  </div>
</body>
</html>
"""


def wrap_fafl_email_html(title: str, body_html: str) -> str:
    """
    Reusable FAFL branded email shell with logo header.
    Pass in already-formatted HTML content for the body.
    """
    return f"""
    <html>
    <body style="font-family:Inter,Arial,sans-serif; background:#f6f7fb; margin:0; padding:0;">
      <div style="padding:24px;">
        <div style="max-width:680px; margin:0 auto; background:#ffffff; border-radius:12px;
                    overflow:hidden; border:1px solid #e5e7eb;">

          <!-- Header -->
          <div style="background:#4A148C; padding:16px 20px;">
            <img src="{LOGO_URL}" alt="Foreign A Foot Logistics"
                 style="height:52px; display:block;">
          </div>

          <!-- Content -->
          <div style="padding:22px 20px;">
            <div style="font-size:18px; font-weight:700; color:#111827; margin:0 0 12px 0;">
              {title}
            </div>

            {body_html}

            <div style="margin-top:18px; padding-top:14px; border-top:1px solid #e5e7eb;
                        color:#6b7280; font-size:12px; line-height:1.4;">
              <div><b>Foreign A Foot Logistics Limited</b></div>
              <div>Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine</div>
              <div>(876) 560-7764 • foreignafootlogistics@gmail.com</div>
              <div style="margin-top:6px;">
                <a href="{DASHBOARD_URL}" style="color:#4A148C; text-decoration:none;">
                  Open Customer Dashboard
                </a>
              </div>
            </div>
          </div>

        </div>
      </div>
    </body>
    </html>
    """


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
            created_at=datetime.now(timezone.utc),
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
    recipient_user_id: int | None = None,
    log_to_messages: bool = False,  # ✅ NEW: only log when True
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

        # ✅ Mirror into in-app Messages ONLY when explicitly requested
        if log_to_messages and recipient_user_id:
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
3200 112th Ave
KCDA-{reg_number} A
Doral, Florida 33172

Thank you for choosing us!

- Foreign A Foot Logistics Limited Team
""".strip()

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {full_name},</p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        Welcome to <b>Foreign A Foot Logistics Limited</b> — your trusted partner in shipping and logistics!
      </p>

      <div style="margin:14px 0; color:#111827; line-height:1.6;">
        <div><b>Registration Number:</b> {reg_number}</div>
        <div style="margin-top:10px;"><b>U.S. Shipping Address:</b></div>
        <div>Air Standard</div>
        <div>{full_name}</div>
        <div>3200 NW 112th Ave</div>
        <div>KCDA-{reg_number} A</div>
        <div>Doral, Florida 33172</div>
      </div>

      <p style="margin:14px 0 0 0; color:#374151;">
        You can now log in and manage shipments, invoices, and deliveries.
      </p>

      <div style="text-align:center; margin-top:16px;">
        <a href="{DASHBOARD_URL}"
           style="display:inline-block; background:#4A148C; color:#fff; padding:12px 18px;
                  border-radius:10px; text-decoration:none; font-weight:700;">
          Login to Dashboard
        </a>
      </div>
    """

    html_body = wrap_fafl_email_html(title="Welcome to Foreign A Foot Logistics", body_html=body_html)

    return send_email(
        to_email=email,
        subject="Welcome to Foreign A Foot Logistics Limited!",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,
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
""".strip()

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {full_name},</p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        We received a request to reset your password.
      </p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        Click the button below to reset it. This link expires in <b>10 minutes</b>.
      </p>

      <div style="text-align:center; margin-top:16px;">
        <a href="{reset_link}"
           style="display:inline-block; background:#4A148C; color:#fff; padding:12px 18px;
                  border-radius:10px; text-decoration:none; font-weight:700;">
          Reset Password
        </a>
      </div>

      <p style="margin:14px 0 0 0; color:#6b7280; font-size:13px;">
        If you didn’t request this, you can ignore this email.
      </p>
    """

    html_body = wrap_fafl_email_html(title="Password Reset Request", body_html=body_html)

    return send_email(
        to_email=to_email,
        subject="Reset Your Password - Foreign A Foot Logistics",
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,
    )


# ==========================================================
#  BULK MESSAGE EMAIL
# ==========================================================
def send_bulk_message_email(to_email, full_name, subject, message_body, recipient_user_id=None):
    subject = (subject or "").strip()
    message_body = (message_body or "").strip()

    plain_body = f"""
Dear {full_name},

{message_body}

Best regards,
Foreign A Foot Logistics Team
""".strip()

    safe_msg = escape(message_body or "").replace("\n", "<br>")

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {full_name},</p>

      <div style="white-space:normal; line-height:1.6; color:#111827;">
        {safe_msg}
      </div>

      <p style="margin:16px 0 0 0; color:#374151;">
        Best regards,<br>
        <b>Foreign A Foot Logistics Team</b>
      </p>
    """

    html_body = wrap_fafl_email_html(title=subject or "Announcement", body_html=body_html)

    email_subject = subject or "Announcement"
    if "foreign a foot" not in email_subject.lower():
        email_subject = f"{email_subject} - Foreign A Foot Logistics"

    return send_email(
        to_email=to_email,
        subject=email_subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=True,  # ✅ this IS a real message
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
        "📍 Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine",
        "🌐 app.faflcourier.com",
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
      <p style="margin:4px 0;">📍 Unit 7, Lot C22, Cedar Manor Gregory Park P.O. St. Catherine</p>
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
        log_to_messages=False,
    )


# ==========================================================
#  INVOICE EMAIL
# ==========================================================
def send_invoice_email(to_email, full_name, invoice, pdf_bytes=None, recipient_user_id=None):
    invoice = invoice or {}

    inv_no = invoice.get("number") or "—"
    subject = f"📄 Invoice {inv_no} is Ready"

    total_due = invoice.get("total_due") or 0
    try:
        total_due_num = float(total_due)
    except (TypeError, ValueError):
        total_due_num = 0.0

    inv_date = invoice.get("date")
    if hasattr(inv_date, "strftime"):
        inv_date_str = inv_date.strftime("%Y-%m-%d %H:%M")
    elif isinstance(inv_date, str) and inv_date:
        inv_date_str = inv_date
    else:
        inv_date_str = ""

    plain_body = (
        f"Hi {full_name},\n\n"
        f"Your invoice from Foreign A Foot Logistics Limited is now available.\n\n"
        f"Invoice #: {inv_no}\n"
        f"Date: {inv_date_str}\n"
        f"Total Due: JMD {total_due_num:,.2f}\n\n"
        f"View details / pay here:\n{TRANSACTIONS_URL}\n\n"
        f"Thank you for shipping with us!\n"
        f"Foreign A Foot Logistics Limited\n"
    )

    html_body = render_template(
        "emails/invoice_email.html",
        full_name=full_name,
        invoice=invoice,
        transactions_url=TRANSACTIONS_URL,
        LOGO_URL=LOGO_URL,
    )

    attachments = []
    if isinstance(pdf_bytes, (bytes, bytearray)) and len(pdf_bytes) > 0:
        attachments = [(bytes(pdf_bytes), f"Invoice_{inv_no}.pdf", "application/pdf")]

    return send_email(
        to_email=to_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        attachments=attachments,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,
    )
# ==========================================================
#  NEW MESSAGE EMAIL
# ==========================================================
def send_new_message_email(user_email, user_name, message_subject, message_body, recipient_user_id=None):
    base = (message_subject or "Message").strip()

    # avoid duplicate prefix
    if base.lower().startswith("new message:"):
        subject = base
    else:
        subject = f"New Message: {base}"

    # add branding
    if "foreign a foot" not in subject.lower():
        subject = f"{subject} - Foreign A Foot Logistics"

    plain_body = (
        f"Hello {user_name},\n\n"
        f"You have received a new message:\n\n"
        f"{message_body}\n\n"
        f"Please log in to your account to reply or view details."
    )

    safe_msg = escape(message_body or "").replace("\n", "<br>")
    messages_url = f"{DASHBOARD_URL}/customer/messages"

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {user_name},</p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        You have received a new message:
      </p>

      <div style="white-space:normal; line-height:1.6; color:#111827;">
        {safe_msg}
      </div>

      <div style="text-align:center; margin-top:16px;">
        <a href="{messages_url}"
           style="display:inline-block; background:#4A148C; color:#fff; padding:12px 18px;
                  border-radius:10px; text-decoration:none; font-weight:700;">
          Open Messages
        </a>
      </div>
    """

    html_body = wrap_fafl_email_html(title=subject, body_html=body_html)

    return send_email(
        to_email=user_email,
        subject=subject,
        plain_body=plain_body,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,  # ✅ notification email only
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

    base_url = (base or "https://app.faflcourier.com").rstrip("/")
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
        f"Prefer delivery? Schedule it here: {DELIVERY_URL}\n\n"
        f"Thanks,\nForeign A Foot Logistics"
    )

    items_html = "<br>".join(
        f"• <b>{it.get('tracking_number','-')}</b> — {it.get('description','-')} — "
        f"<b>${it.get('amount_due',0):.2f}</b>"
        for it in items
    ) or "• (no details)"

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {full_name},</p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        <b>Your package(s) are READY FOR PICKUP:</b>
      </p>

      <div style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        {items_html}
      </div>

      <p style="margin:0 0 12px 0; color:#374151;">
        Prefer delivery? Schedule it below.
      </p>

      <div style="text-align:center; margin-top:16px;">
        <a href="{DELIVERY_URL}"
           style="display:inline-block; background:#4A148C; color:#fff; padding:12px 18px;
                  border-radius:10px; text-decoration:none; font-weight:700;">
          Schedule Delivery
        </a>
      </div>
    """

    html_body = wrap_fafl_email_html(title="Ready for Pickup", body_html=body_html)

    return send_email(
        to_email=to_email,
        subject="Your package is Ready for Pick Up",
        plain_body=plain,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,
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

    body_html = f"""
      <p style="margin:0 0 12px 0; color:#374151;">Hi {full_name},</p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        Your shipment invoice is ready.
      </p>

      <p style="margin:0 0 12px 0; color:#111827; line-height:1.6;">
        <b>Total Due:</b> ${total_due:.2f}
      </p>

      <div style="text-align:center; margin-top:16px;">
        <a href="{invoice_link}"
           style="display:inline-block; background:#4A148C; color:#fff; padding:12px 18px;
                  border-radius:10px; text-decoration:none; font-weight:700;">
          View / Pay Invoice
        </a>
      </div>
    """

    html_body = wrap_fafl_email_html(title="Shipment Invoice", body_html=body_html)

    return send_email(
        to_email=to_email,
        subject="Your Shipment Invoice",
        plain_body=plain,
        html_body=html_body,
        recipient_user_id=recipient_user_id,
        log_to_messages=False,
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
        log_to_messages=False,        
    )

