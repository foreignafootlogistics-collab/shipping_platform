# send_password_setup_bulk.py
import os
import time
from datetime import datetime
from itsdangerous import URLSafeTimedSerializer

from app import create_app
from app.models import User
from app.utils import email_utils

# ============================================================
# ✅ CONFIG
# ============================================================

# ✅ True = send to all customers, False = only TEST_EMAILS
SEND_TO_ALL = True

TEST_EMAILS = [
    "sweet_sade_18@yahoo.com",
]

SUBJECT = "Your New FAFL Customer Portal – Set Your Password"

# Slow down sending slightly (helps deliverability)
SLEEP_SECONDS_BETWEEN_EMAILS = 1.2

# ✅ Warm-up batches (recommended)
# Start: 50–100; then increase
MAX_TO_SEND = 100

# ✅ Resume control (0 = start from beginning of remaining list)
START_INDEX = 0

# ✅ Local logs so we never resend
SENT_LOG_FILE = "sent_password_setup_emails.txt"
FAILED_LOG_FILE = "failed_password_setup_emails.txt"

# Helpful identifier for your run
RUN_ID = datetime.now().strftime("%Y%m%d-%H%M%S")


# ============================================================
# ✅ EMAIL BODY BUILDERS (Deliverability-Friendly)
# ============================================================

def build_email_plain(reset_link: str) -> str:
    # ✅ Plain text includes the full link (copy/paste friendly)
    return f"""Dear Valued Customer,

Foreign A Foot Logistics Limited has launched a new and improved customer portal to help you manage shipments, track packages, view invoices, and update your account details more securely.

You are receiving this email because you are a customer of Foreign A Foot Logistics Limited.

🔐 Set your password (fastest way)
Use your secure personal link below (copy and paste into your browser if needed):
{reset_link}

If the link above does NOT work, follow these steps instead:
1) Go to: https://app.faflcourier.com
2) Click: “Forgot password / Reset password”
3) Enter your email address and follow the instructions.

🔒 Safety reminder
• The only official login website is: https://app.faflcourier.com
• We will never ask you for your password by WhatsApp, text message, or phone call.

Need help?
Reply to this email or contact us at support@faflcourier.com / (876) 560-7764.

Warm regards,
Foreign A Foot Logistics Limited
Customer Support Team
support@faflcourier.com
""".strip()


def build_email_html(reset_link: str) -> str:
    # ✅ HTML does NOT print the long token link (avoid spam triggers)
    # ✅ Link is only behind the button
    # ✅ Still includes clear fallback instructions without exposing the token
    return f"""
<p>Dear Valued Customer,</p>

<p>
  <strong>Foreign A Foot Logistics Limited</strong> has launched a new and improved customer portal to help you manage shipments,
  track packages, view invoices, and update your account details more securely.
</p>

<p style="margin:0 0 12px 0; color:#374151;">
  You are receiving this email because you are a customer of Foreign A Foot Logistics Limited.
</p>

<h3 style="margin:16px 0 8px 0;">🔐 Set your password</h3>

<p style="margin:0 0 10px 0;">
  For security reasons, we do not send passwords by email. Please use your secure personal link below:
</p>

<p style="margin:16px 0;">
  <a href="{reset_link}"
     style="display:inline-block;background:#4A148C;color:#fff;text-decoration:none;
            padding:12px 18px;border-radius:8px;font-weight:700;">
    Set Your Password
  </a>
</p>

<p style="margin:0 0 12px 0; color:#111827;">
  <strong>If the button does NOT work:</strong><br>
  1) Visit <a href="https://app.faflcourier.com" style="color:#4A148C;text-decoration:none;">https://app.faflcourier.com</a><br>
  2) Click <strong>“Forgot password / Reset password”</strong><br>
  3) Enter your email address and follow the instructions.<br><br>
  <span style="color:#6b7280; font-size:13px;">
    Tip: You can also open the <strong>plain-text version</strong> of this email to copy/paste your secure link if needed.
  </span>
</p>

<p style="margin:14px 0 0 0;">
  <strong>🔒 Safety reminder</strong><br>
  • The only official login website is:
  <a href="https://app.faflcourier.com" style="color:#4A148C;text-decoration:none;">https://app.faflcourier.com</a><br>
  • We will never ask you for your password by WhatsApp, text message, or phone call.
</p>

<p style="margin:14px 0 0 0;">
  Need help? Reply to this email or contact us at
  <a href="mailto:support@faflcourier.com" style="color:#4A148C;text-decoration:none;">support@faflcourier.com</a>
  or call <strong>(876) 560-7764</strong>.
</p>

<p style="margin:14px 0 0 0;">
  Warm regards,<br>
  <strong>Foreign A Foot Logistics Limited</strong><br>
  Customer Support Team<br>
  support@faflcourier.com
</p>
""".strip()


# ============================================================
# ✅ LOG HELPERS
# ============================================================

def load_set(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def append_line(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def append_sent(email: str) -> None:
    append_line(SENT_LOG_FILE, email.strip().lower())


def append_failed(email: str, reason: str) -> None:
    # include run id + reason for easy debugging
    append_line(FAILED_LOG_FILE, f"{RUN_ID}\t{email.strip().lower()}\t{reason}")


# ============================================================
# ✅ MAIN
# ============================================================

def main():
    app = create_app()

    with app.app_context():
        secret = app.config.get("SECRET_KEY") or app.secret_key
        if not secret:
            raise RuntimeError("SECRET_KEY is missing. Set SECRET_KEY in your environment/config.")

        serializer = URLSafeTimedSerializer(secret)
        base = (email_utils.DASHBOARD_URL or "https://app.faflcourier.com").rstrip("/")

        sent_set = load_set(SENT_LOG_FILE)
        print(f"📌 Already sent (from log): {len(sent_set)}")
        print(f"🧾 Run ID: {RUN_ID}")

        # Build recipient list (plain values only)
        if SEND_TO_ALL:
            rows = (
                User.query
                .with_entities(User.id, User.email)
                .filter(User.role == "customer")
                .filter(User.email.isnot(None))
                .all()
            )
            recipients_all = [(uid, (email or "").strip().lower()) for uid, email in rows if (email or "").strip()]
            print(f"📨 TOTAL customers with emails: {len(recipients_all)}")
        else:
            test_list = [e.strip().lower() for e in TEST_EMAILS if (e or "").strip()]
            rows = (
                User.query
                .with_entities(User.id, User.email)
                .filter(User.email.in_(test_list))
                .all()
            )
            recipients_all = [(uid, (email or "").strip().lower()) for uid, email in rows if (email or "").strip()]
            print(f"🧪 Test mode recipients: {len(recipients_all)}")

        # Skip already-sent
        recipients = [(uid, email) for (uid, email) in recipients_all if email not in sent_set]
        print(f"✅ Remaining to send (after skip): {len(recipients)}")

        # Apply resume + batch limits
        if START_INDEX > 0:
            recipients = recipients[START_INDEX:]
        if MAX_TO_SEND and MAX_TO_SEND > 0:
            recipients = recipients[:MAX_TO_SEND]

        print(f"🚀 Sending this run: {len(recipients)} (START_INDEX={START_INDEX}, MAX_TO_SEND={MAX_TO_SEND})")

        sent = 0
        failed = 0
        RETRIES = 3

        for i, (user_id, email) in enumerate(recipients, start=1):
            token = serializer.dumps(email, salt="reset-password-salt")
            reset_link = f"{base}/reset-password/{token}"

            plain_body = build_email_plain(reset_link)
            html_body = build_email_html(reset_link)

            ok = False
            last_err = None

            for attempt in range(1, RETRIES + 1):
                try:
                    ok = email_utils.send_email_sendgrid_api(
                        to_email=email,
                        subject=SUBJECT,
                        plain_body=plain_body,
                        html_body=html_body,
                        from_email="support@faflcourier.com",
                    )

                    if ok:
                        break

                    last_err = "send_email_sendgrid_api returned False"

                except Exception as e:
                    last_err = str(e)

                # ✅ STOP retrying on auth issues (no point retrying)
                msg = (last_err or "").lower()
                if "authorization grant" in msg or "invalid, expired, or revoked" in msg or " 401" in msg or "401" in msg or "403" in msg:
                    break

                # Backoff before retry
                time.sleep(2 * attempt)

            if ok:
                sent += 1
                append_sent(email)
                print(f"✅ [{i}/{len(recipients)}] Sent to {email}")
            else:
                failed += 1
                append_failed(email, last_err or "unknown_error")
                print(f"❌ [{i}/{len(recipients)}] Failed to send to {email} | last_error={last_err}")

            time.sleep(SLEEP_SECONDS_BETWEEN_EMAILS)

        print("\n==============================")
        print(f"DONE. Sent: {sent} | Failed: {failed} | This Run: {len(recipients)}")
        print(f"Sent log:   {SENT_LOG_FILE}")
        print(f"Failed log: {FAILED_LOG_FILE}")
        print("==============================\n")

        # ✅ Prevent teardown crash if DB connection drops after long run
        try:
            from app.extensions import db
            db.session.remove()
        except Exception:
            pass


if __name__ == "__main__":
    main()
