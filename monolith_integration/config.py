"""Env-driven config for capability writer + usage consumer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class MonolithWhatsappConfig:
    api_base_url: str
    capabilities_secret: str
    usage_events_secret: str
    database_uri: str
    consumer_name: str
    http_timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    poll_interval_seconds: float
    poll_limit: int

    @classmethod
    def from_env(cls) -> "MonolithWhatsappConfig":
        base = (os.getenv("WHATSAPP_INTERNAL_API_URL") or "").strip().rstrip("/")
        cap = (os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
        usage = (os.getenv("WHATSAPP_USAGE_EVENTS_SECRET") or "").strip() or cap
        dsn = (os.getenv("SQLALCHEMY_DATABASE_URI") or "").strip()
        consumer = (os.getenv("WHATSAPP_USAGE_CONSUMER_NAME") or "billing_default").strip()
        timeout = float(os.getenv("WHATSAPP_INTEGRATION_HTTP_TIMEOUT", "25") or "25")
        retries = int(os.getenv("WHATSAPP_INTEGRATION_MAX_RETRIES", "5") or "5")
        backoff = float(os.getenv("WHATSAPP_INTEGRATION_BACKOFF_BASE", "0.5") or "0.5")
        poll_interval = float(os.getenv("WHATSAPP_USAGE_POLL_INTERVAL_SECONDS", "3") or "3")
        poll_limit = int(os.getenv("WHATSAPP_USAGE_POLL_LIMIT", "100") or "100")
        return cls(
            api_base_url=base,
            capabilities_secret=cap,
            usage_events_secret=usage,
            database_uri=dsn,
            consumer_name=consumer,
            http_timeout_seconds=timeout,
            max_retries=max(1, retries),
            backoff_base_seconds=max(0.1, backoff),
            poll_interval_seconds=max(0.5, poll_interval),
            poll_limit=max(1, min(poll_limit, 500)),
        )

    def capabilities_url(self, account_id: int) -> str:
        return f"{self.api_base_url}/api/internal/whatsapp/accounts/{account_id}/capabilities"

    def usage_events_url(self) -> str:
        return f"{self.api_base_url}/api/internal/whatsapp/usage-events"

    def is_configured(self) -> bool:
        return bool(self.api_base_url and self.capabilities_secret)

    def is_usage_consumer_configured(self) -> bool:
        if not self.database_uri:
            return False
        inline = (os.getenv("WHATSAPP_USAGE_INLINE_POLL") or "true").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if inline:
            return True
        return bool(self.api_base_url and self.usage_events_secret)
