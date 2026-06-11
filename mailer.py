import smtplib
import ssl
import logging
from email.message import EmailMessage
from flask import current_app

logger = logging.getLogger(__name__)


def send_mail(to_addrs, subject, body):
    """Send email using SMTP configuration from Flask config."""
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    smtp_user = current_app.config.get("SMTP_USER")
    smtp_pass = current_app.config.get("SMTP_PASS")
    smtp_host = current_app.config.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(current_app.config.get("SMTP_PORT", 587))
    mail_from = current_app.config.get("MAIL_FROM", smtp_user or "noreply@sociochat.com")

    if not smtp_user or not smtp_pass:
        logger.warning("SMTP not configured — email not sent to %s: %s", to_addrs, subject)
        logger.debug("Email body preview: %s", body[:200] if body else "")
        return False

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = mail_from
        msg["To"] = ", ".join(to_addrs)
        msg.set_content(body)

        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)

        logger.info("Email sent successfully to %s: %s", to_addrs, subject)
        return True

    except Exception as e:
        logger.exception("Failed to send email to %s: %s", to_addrs, e)
        raise
