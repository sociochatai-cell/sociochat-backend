import os, json
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()

# Database URL is REQUIRED — there is NO SQLite fallback. This app runs on PostgreSQL
# in every environment. If the variable is missing we fail fast at startup instead of
# silently starting on a throwaway SQLite file (on Cloud Run that filesystem is
# ephemeral, so a silent fallback would mean data loss).
_DATABASE_URI = os.getenv("SQLALCHEMY_DATABASE_URI")
if not _DATABASE_URI:
    raise RuntimeError(
        "SQLALCHEMY_DATABASE_URI is not set. Refusing to start without a PostgreSQL "
        "connection string (the SQLite fallback has been removed). Set "
        "SQLALCHEMY_DATABASE_URI in your environment / .env to your PostgreSQL URL."
    )

# The SECRET_KEY signs every session cookie AND every auth JWT (see auth_core). If it
# is missing or left at a well-known dev default, anyone can forge a valid token for
# any user/admin — a total auth bypass. So, like the DB URL above, fail fast in any
# real (non-dev) environment instead of silently running on the guessable default.
# Local dev (no K_SERVICE / dev env) still gets the convenience fallback.
_KNOWN_WEAK_SECRETS = {
    "",
    "dev-secret-change-in-production",
    "dev-secret-key-change-in-production",
    "changeme",
    "change-me",
}
_SECRET_KEY = (os.getenv("SECRET_KEY") or os.getenv("SESSION_SECRET") or "").strip()
if not _SECRET_KEY or _SECRET_KEY in _KNOWN_WEAK_SECRETS:
    try:
        from core.deployment_safety import is_non_dev_environment
        _is_prod = is_non_dev_environment()
    except Exception:
        _is_prod = False
    if _is_prod:
        raise RuntimeError(
            "SECRET_KEY (or SESSION_SECRET) is not set to a strong secret. Refusing to "
            "start in a non-dev environment because session cookies and auth JWTs would "
            "be forgeable. Set a long random SECRET_KEY in your environment and redeploy."
        )
    _SECRET_KEY = _SECRET_KEY or "dev-secret-change-in-production"

class Config:
    SECRET_KEY = _SECRET_KEY
    SQLALCHEMY_DATABASE_URI = _DATABASE_URI
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # SMTP
    SMTP_HOST = os.getenv("SMTP_HOST")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER")
    SMTP_PASS = os.getenv("SMTP_PASS")
    MAIL_FROM = os.getenv("MAIL_FROM", "noreply@sociochat.com")

    # Admin
    ADMIN_EMAILS = [e.strip() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000")
    FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")

    VERIFY_TTL_MIN = int(os.getenv("VERIFY_TTL_MIN", 15))
    ADMIN_LINK_TTL_HOURS = int(os.getenv("ADMIN_LINK_TTL_HOURS", 48))
    PERMANENT_SESSION_LIFETIME = timedelta(days=int(os.getenv("PERMANENT_SESSION_LIFETIME_DAYS", 7)))
