"""HTTP client: push entitlement projection to WhatsApp internal API."""

from __future__ import annotations

from typing import Any, Dict

from .config import MonolithWhatsappConfig
from .http_client import request_with_retries
from .metrics import integration_metrics
from .structured_log import mi_log


class WhatsappCapabilitiesClient:
    def __init__(self, config: MonolithWhatsappConfig):
        self._cfg = config

    @classmethod
    def from_env(cls) -> "WhatsappCapabilitiesClient":
        return cls(MonolithWhatsappConfig.from_env())

    def is_configured(self) -> bool:
        return self._cfg.is_configured()

    def push_account(self, account_id: int, body: Dict[str, Any]) -> bool:
        """
        PUT capabilities for one WhatsApp account id. Retries on 5xx/transport errors.
        Returns True on 2xx. Logs failures (non-throwing for callers).
        """
        if not self._cfg.is_configured():
            mi_log("capabilities_push_skipped", reason="not_configured", account_id=account_id)
            return False

        url = self._cfg.capabilities_url(account_id)
        headers = {
            "Authorization": f"Bearer {self._cfg.capabilities_secret}",
            "Content-Type": "application/json",
        }
        resp, err = request_with_retries(
            "PUT",
            url,
            headers=headers,
            json_body=body,
            timeout=self._cfg.http_timeout_seconds,
            max_retries=self._cfg.max_retries,
            backoff_base=self._cfg.backoff_base_seconds,
            log_context={"op": "capabilities_upsert", "account_id": account_id, "url": url},
        )
        if resp is not None and 200 <= resp.status_code < 300:
            integration_metrics.inc("capability_upsert_ok")
            mi_log(
                "capabilities_projection_upsert_ok",
                account_id=account_id,
                status_code=resp.status_code,
                projection_version=body.get("projection_version"),
            )
            return True

        status = resp.status_code if resp is not None else None
        snippet = (resp.text[:500] if resp is not None and resp.text else "") or ""
        mi_log(
            "capabilities_projection_upsert_failed",
            account_id=account_id,
            status_code=status,
            error=err,
            body_snippet=snippet,
        )
        integration_metrics.inc("capability_upsert_fail")
        return False
