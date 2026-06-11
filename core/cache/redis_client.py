from __future__ import annotations

import os
import threading
from threading import Lock

try:
    import redis
except ImportError:
    redis = None

_redis_client = None
_redis_lock = Lock()
_blocking_local = threading.local()

WA_KEY_PREFIX = os.getenv("WA_REDIS_KEY_PREFIX", "wa:")


def _redis_url() -> str | None:
    return (os.getenv("REDIS_URL") or os.getenv("REDIS_TLS_URL") or "").strip() or None


def _redis_connection_kwargs() -> dict:
    poll_timeout = max(1, int(os.getenv("JOB_WORKER_POLL_TIMEOUT", "5")))
    return {
        "decode_responses": True,
        "socket_connect_timeout": float(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
        "socket_keepalive": True,
        "health_check_interval": int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30")),
        "retry_on_timeout": True,
        "socket_timeout": None,
    }


def _create_redis_client():
    if redis is None:
        return None
    redis_url = _redis_url()
    if not redis_url:
        return None
    return redis.from_url(redis_url, **_redis_connection_kwargs())


def get_redis_client():
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    if redis is None or not _redis_url():
        return None
    with _redis_lock:
        if _redis_client is None:
            _redis_client = _create_redis_client()
    return _redis_client


def get_blocking_redis_client():
    client = getattr(_blocking_local, "client", None)
    if client is not None:
        return client
    client = _create_redis_client()
    _blocking_local.client = client
    return client
