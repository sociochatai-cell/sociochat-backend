import smtplib
import ssl
import logging
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_mail(to_addrs, subject, body, tenant_id=None, workspace_id=None):
    """Send email using per-tenant SMTP configuration.

    When ``tenant_id`` (or ``workspace_id``) is provided, SMTP host/port/user/
    pass/from are resolved for that tenant. Each field falls back to the global
    ``.env`` values (SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS/MAIL_FROM), so when
    no tenant override exists — or when called with no tenant — behavior is
    byte-identical to the previous global-only configuration.
    """
    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    # Lazy import to avoid circular imports at module load time.
    from tenant.integration import get_tenant_smtp_config

    cfg = get_tenant_smtp_config(tenant_id=tenant_id, workspace_id=workspace_id)

    smtp_user = cfg.user
    smtp_pass = cfg.password
    smtp_host = cfg.host
    smtp_port = int(cfg.port)
    mail_from = cfg.mail_from

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
