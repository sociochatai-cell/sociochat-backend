"""HTTP with exponential backoff for WhatsApp internal APIs."""

from __future__ import annotations

import random
import time
from typing import Any, Callable, Dict, Optional, Tuple

import requests

from .structured_log import mi_log


def request_with_retries(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    timeout: float,
    max_retries: int,
    backoff_base: float,
    log_context: Dict[str, Any],
    on_retry_attempt: Optional[Callable[[int, str], None]] = None,
) -> Tuple[Optional[requests.Response], Optional[str]]:
    """
    Returns (response, None) on success (2xx), or (None, error_reason) after retries exhausted.
    Does not raise on HTTP 4xx/5xx — caller decides; 5xx triggers retry.
    """
    last_err: Optional[str] = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.request(
                method.upper(),
                url,
                headers=headers or {},
                json=json_body,
                timeout=timeout,
            )
            if 200 <= resp.status_code < 300:
                if attempt > 1:
                    mi_log("http_retry_recovered", attempt=attempt, **log_context, status_code=resp.status_code)
                return resp, None
            if 400 <= resp.status_code < 500:
                return resp, f"http_{resp.status_code}"
            if resp.status_code >= 500:
                last_err = f"http_{resp.status_code}"
                mi_log(
                    "http_retry",
                    attempt=attempt,
                    max_retries=max_retries,
                    reason=last_err,
                    **log_context,
                )
        except requests.RequestException as exc:
            last_err = str(exc)
            mi_log(
                "http_retry",
                attempt=attempt,
                max_retries=max_retries,
                reason=last_err,
                **log_context,
            )
        if attempt >= max_retries:
            break
        if on_retry_attempt:
            try:
                on_retry_attempt(attempt, last_err or "")
            except Exception:
                pass
        sleep_s = backoff_base * (2 ** (attempt - 1)) * (1 + random.random() * 0.1)
        time.sleep(min(sleep_s, 30.0))
    return None, last_err or "unknown"
