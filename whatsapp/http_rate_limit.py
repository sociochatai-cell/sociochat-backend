"""
Lightweight HTTP rate limiting for whatsapp-service routes.

Uses an in-memory sliding window keyed by route + workspace (or client IP).
For multi-instance Cloud Run, prefer Redis-backed limits at the edge or via shared DB.
"""
from __future__ import annotations

import os
import time
from functools import wraps
from typing import Dict, List, Tuple

from flask import jsonify, request

# route_key -> (max_requests, window_seconds)
_DEFAULT_LIMITS: Dict[str, Tuple[int, int]] = {
    "whatsapp.drip.create": (30, 60),
    "whatsapp.sheets.sync": (10, 60),
    "whatsapp.bulk.create": (20, 60),
    "whatsapp.bulk.recipients": (30, 60),
    "whatsapp.bulk.schedule": (20, 60),
    "whatsapp.ai.test": (30, 60),
    "whatsapp.ai.intent": (60, 60),
    "whatsapp.ai.rewrite": (30, 60),
    "whatsapp.template.rewrite": (30, 60),
    "whatsapp.template.create": (20, 60),
    "whatsapp.template.submit": (10, 60),
    "whatsapp.knowledge.crawl": (5, 60),
    "whatsapp.knowledge.preview": (20, 60),
}

_cache: Dict[str, List[float]] = {}


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _limit_for_route(route_key: str) -> Tuple[int, int]:
    env_max = _env_int(f"RATE_LIMIT_{route_key.upper().replace('.', '_')}_MAX", -1)
    env_window = _env_int(f"RATE_LIMIT_{route_key.upper().replace('.', '_')}_WINDOW", -1)
    default_max, default_window = _DEFAULT_LIMITS.get(route_key, (60, 60))
    return (
        env_max if env_max > 0 else default_max,
        env_window if env_window > 0 else default_window,
    )


def _client_key() -> str:
    workspace = (
        request.headers.get("X-Workspace-ID")
        or request.args.get("workspace_id")
        or (request.get_json(silent=True) or {}).get("workspace_id")
        or request.form.get("workspace_id")
    )
    if workspace:
        return f"ws:{workspace}"
    return f"ip:{request.remote_addr or 'unknown'}"


def check_rate_limit(route_key: str) -> Tuple[bool, dict]:
    if (os.getenv("RATE_LIMIT_DISABLED") or "").strip().lower() in {"1", "true", "yes"}:
        return True, {}

    max_requests, window = _limit_for_route(route_key)
    cache_key = f"{route_key}:{_client_key()}"
    now = time.time()
    window_start = now - window

    hits = [t for t in _cache.get(cache_key, []) if t >= window_start]
    if len(hits) >= max_requests:
        retry_after = max(1, int(window - (now - hits[0])))
        return False, {
            "type": "http",
            "route": route_key,
            "retry_after": retry_after,
            "limit": max_requests,
            "window_seconds": window,
        }

    hits.append(now)
    _cache[cache_key] = hits
    return True, {}


def rate_limit(route_key: str):
    """Decorator compatible with Sociovia ``rate_limit(route_key)`` usage."""

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            allowed, info = check_rate_limit(route_key)
            if not allowed:
                return jsonify({
                    "error": "rate_limit_exceeded",
                    "limit_type": info.get("type", "http"),
                    "route": route_key,
                    "retry_after_seconds": info.get("retry_after", 60),
                }), 429
            return fn(*args, **kwargs)

        return wrapper

    return decorator
