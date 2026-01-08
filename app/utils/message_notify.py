from flask import current_app
from flask_mail import Message as MailMessage
from app import mail

def send_new_message_email(to_email: str, subject: str, preview: str):
    try:
        if not to_email:
            return False

        msg = MailMessage(
            subject=f"FAFL: New message - {subject}",
            recipients=[to_email],
        )
        msg.body = (
            "You have a new message in your FAFL portal.\n\n"
            f"Subject: {subject}\n"
            f"Preview: {preview}\n\n"
            "Log in to view and reply."
        )
        mail.send(msg)
        return True
    except Exception as e:
        current_app.logger.warning("send_new_message_email failed: %s", e)
        return False
