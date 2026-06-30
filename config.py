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

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
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
