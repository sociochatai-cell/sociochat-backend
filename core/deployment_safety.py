"""Deployment-time safety helpers for scheduler and internal APIs."""

from __future__ import annotations

import os
from typing import Tuple

from core.config import env_flag

_DEFAULT_INSECURE_SCHEDULER_TOKEN = "default-insecure-token-please-change-me"


def is_non_dev_environment() -> bool:
    if env_flag("WHATSAPP_FORCE_DEV", False):
        return False
    if (os.getenv("K_SERVICE") or "").strip():
        return True
    fe = (os.getenv("FLASK_ENV") or "").strip().lower()
    if fe == "development":
        return False
    env = (os.getenv("ENV") or "").strip().lower()
    if env in ("development", "dev", "local"):
        return False
    deploy = (os.getenv("WHATSAPP_DEPLOYMENT_ENV") or "").strip().lower()
    if deploy in ("staging", "production", "prod", "stage"):
        return True
    if fe in ("production", "staging", "prod", "stage"):
        return True
    if env in ("production", "staging", "prod", "stage"):
        return True
    return False


def scheduler_secret_acceptable_for_environment() -> Tuple[bool, str]:
    raw = (os.getenv("SCHEDULER_SECRET_TOKEN") or "").strip()
    if not is_non_dev_environment():
        return True, "scheduler_using_default_token_dev_ok"
    if not raw:
        return False, "SCHEDULER_SECRET_TOKEN_unset_in_non_dev"
    if raw == _DEFAULT_INSECURE_SCHEDULER_TOKEN:
        return False, "SCHEDULER_SECRET_TOKEN_is_default_placeholder_in_non_dev"
    return True, "scheduler_secret_ok"


def usage_events_read_secret_configured() -> bool:
    cap = (os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
    usage = (os.getenv("WHATSAPP_USAGE_EVENTS_SECRET") or "").strip()
    return bool(usage or cap)


def slog(event: str, **fields) -> None:
    """Structured log helper used by usage_events."""
    import logging
    logging.getLogger("whatsapp.platform").info("%s %s", event, fields)
