import os, json
from dotenv import load_dotenv
from datetime import timedelta

load_dotenv()

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "SQLALCHEMY_DATABASE_URI",
          # SQLite default for local dev
    )
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
