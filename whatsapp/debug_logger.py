"""
Structured WhatsApp debug logger — before/after tracing for Cloud Logging.

Enable with WHATSAPP_DEBUG_MODE=1 (or LOG_LEVEL=DEBUG).

Every event is a single JSON line prefixed with [wa-debug] for easy grep:
  gcloud logging read 'textPayload:"[wa-debug]"' ...

Context fields (short keys for compact logs):
  wp_id   — workspace_id
  us_id   — user_id (header/session)
  acct_id — whatsapp account id
  pn_id   — phone_number_id
  waba_id — WhatsApp Business Account id
  conv_id — conversation id
  wamid   — Meta message id
  req_id  — HTTP request correlation id
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("wa.debug")

_CTX: ContextVar[Dict[str, Any]] = ContextVar("wa_debug_ctx", default={})

_LOG_PREFIX = "[wa-debug]"

_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "token",
        "authorization",
        "password",
        "secret",
        "api_key",
        "cookie",
        "set-cookie",
    }
)


def _truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def debug_mode_enabled() -> bool:
    if _truthy(os.getenv("WHATSAPP_DEBUG_MODE")):
        return True
    if _truthy(os.getenv("FLASK_DEBUG")):
        return True
    level = os.getenv("LOG_LEVEL", "").strip().upper()
    return level == "DEBUG"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 4:
        return str(value)[:200]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 2000:
            return value[:2000] + "…(truncated)"
        return value
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in list(value.items())[:40]:
            key = str(k).lower()
            if any(s in key for s in _SENSITIVE_KEYS):
                out[str(k)] = "<redacted>"
            else:
                out[str(k)] = _sanitize(v, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_sanitize(v, depth=depth + 1) for v in list(value)[:30]]
    return str(value)[:500]


def _merge_ctx(**fields: Any) -> Dict[str, Any]:
    base = dict(_CTX.get() or {})
    for k, v in fields.items():
        if v is not None and v != "":
            base[k] = v
    return base


def bind_context(**fields: Any) -> None:
    """Attach correlation ids for the current thread/request."""
    current = dict(_CTX.get() or {})
    for k, v in fields.items():
        if v is not None and v != "":
            current[k] = v
    _CTX.set(current)


def clear_context() -> None:
    _CTX.set({})


def _emit(phase: str, event: str, payload: Dict[str, Any]) -> None:
    if not debug_mode_enabled():
        return
    record = {
        "ts": _now_iso(),
        "phase": phase,
        "event": event,
        **_merge_ctx(**{k: v for k, v in payload.items() if k not in ("phase", "event")}),
    }
    line = json.dumps(_sanitize(record), sort_keys=True, default=str)
    status = str(record.get("status") or "").lower()
    if status in {"fail", "error", "denied", "blocked"} or record.get("error"):
        logger.warning("%s %s", _LOG_PREFIX, line)
    else:
        logger.info("%s %s", _LOG_PREFIX, line)


class WADebugLogger:
    """Before/after tracing helper for WhatsApp operations."""

    def is_enabled(self) -> bool:
        return debug_mode_enabled()

    def before(
        self,
        event: str,
        *,
        wp_id: Any = None,
        us_id: Any = None,
        acct_id: Any = None,
        pn_id: Any = None,
        waba_id: Any = None,
        conv_id: Any = None,
        wamid: Any = None,
        req_id: Any = None,
        details: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> str:
        trace_id = uuid.uuid4().hex[:12]
        _emit(
            "before",
            event,
            {
                "trace_id": trace_id,
                "wp_id": wp_id,
                "us_id": us_id,
                "acct_id": acct_id,
                "pn_id": pn_id,
                "waba_id": waba_id,
                "conv_id": conv_id,
                "wamid": wamid,
                "req_id": req_id,
                "details": details or {},
                **extra,
            },
        )
        return trace_id

    def after(
        self,
        event: str,
        *,
        trace_id: Optional[str] = None,
        status: str = "ok",
        wp_id: Any = None,
        us_id: Any = None,
        acct_id: Any = None,
        pn_id: Any = None,
        waba_id: Any = None,
        conv_id: Any = None,
        wamid: Any = None,
        req_id: Any = None,
        elapsed_ms: Optional[float] = None,
        error: Any = None,
        error_code: Any = None,
        details: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> None:
        payload: Dict[str, Any] = {
            "trace_id": trace_id,
            "status": status,
            "wp_id": wp_id,
            "us_id": us_id,
            "acct_id": acct_id,
            "pn_id": pn_id,
            "waba_id": waba_id,
            "conv_id": conv_id,
            "wamid": wamid,
            "req_id": req_id,
            "elapsed_ms": round(elapsed_ms, 2) if elapsed_ms is not None else None,
            "details": details or {},
            **extra,
        }
        if error is not None:
            payload["error"] = str(error)[:2000]
        if error_code is not None:
            payload["error_code"] = error_code
        _emit("after", event, payload)

    def http_before(
        self,
        method: str,
        url: str,
        *,
        trace_id: Optional[str] = None,
        wp_id: Any = None,
        us_id: Any = None,
        acct_id: Any = None,
        pn_id: Any = None,
        waba_id: Any = None,
        conv_id: Any = None,
        wamid: Any = None,
        payload: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> str:
        tid = trace_id or uuid.uuid4().hex[:12]
        _emit(
            "before",
            "http_request",
            {
                "trace_id": tid,
                "http_method": method.upper(),
                "http_url": url,
                "wp_id": wp_id,
                "us_id": us_id,
                "acct_id": acct_id,
                "pn_id": pn_id,
                "waba_id": waba_id,
                "conv_id": conv_id,
                "wamid": wamid,
                "request_body": payload,
                **extra,
            },
        )
        return tid

    def http_after(
        self,
        method: str,
        url: str,
        *,
        trace_id: Optional[str] = None,
        status_code: Optional[int] = None,
        wp_id: Any = None,
        us_id: Any = None,
        acct_id: Any = None,
        pn_id: Any = None,
        waba_id: Any = None,
        conv_id: Any = None,
        wamid: Any = None,
        elapsed_ms: Optional[float] = None,
        response_body: Any = None,
        error: Any = None,
        error_code: Any = None,
        **extra: Any,
    ) -> None:
        ok = status_code is not None and 200 <= int(status_code) < 300
        meta_error = None
        meta_error_code = error_code
        if isinstance(response_body, dict):
            err = response_body.get("error")
            if isinstance(err, dict):
                meta_error = err.get("message") or meta_error
                meta_error_code = meta_error_code or err.get("code")
        self.after(
            "http_request",
            trace_id=trace_id,
            status="ok" if ok and not error and not meta_error else "fail",
            wp_id=wp_id,
            us_id=us_id,
            acct_id=acct_id,
            pn_id=pn_id,
            waba_id=waba_id,
            conv_id=conv_id,
            wamid=wamid,
            elapsed_ms=elapsed_ms,
            error=error or meta_error,
            error_code=meta_error_code,
            details={
                "http_method": method.upper(),
                "http_url": url,
                "http_status": status_code,
                "response_body": response_body,
            },
            **extra,
        )

    @contextmanager
    def span(
        self,
        event: str,
        *,
        wp_id: Any = None,
        us_id: Any = None,
        acct_id: Any = None,
        pn_id: Any = None,
        waba_id: Any = None,
        conv_id: Any = None,
        wamid: Any = None,
        req_id: Any = None,
        details: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Iterator[str]:
        started = time.perf_counter()
        trace_id = self.before(
            event,
            wp_id=wp_id,
            us_id=us_id,
            acct_id=acct_id,
            pn_id=pn_id,
            waba_id=waba_id,
            conv_id=conv_id,
            wamid=wamid,
            req_id=req_id,
            details=details,
            **extra,
        )
        err: Optional[BaseException] = None
        try:
            yield trace_id
        except BaseException as exc:
            err = exc
            raise
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            self.after(
                event,
                trace_id=trace_id,
                status="fail" if err else "ok",
                wp_id=wp_id,
                us_id=us_id,
                acct_id=acct_id,
                pn_id=pn_id,
                waba_id=waba_id,
                conv_id=conv_id,
                wamid=wamid,
                req_id=req_id,
                elapsed_ms=elapsed,
                error=str(err) if err else None,
                details=details,
                **extra,
            )


wa_debug = WADebugLogger()
