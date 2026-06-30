from __future__ import annotations

import os
import threading
from threading import Lock

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None


_redis_client = None
_redis_lock = Lock()
_blocking_local = threading.local()

# WhatsApp-specific prefix to avoid key collisions with monolith
WA_KEY_PREFIX = os.getenv("WA_REDIS_KEY_PREFIX", "wa:")


def _redis_url() -> str | None:
    return (os.getenv("REDIS_URL") or os.getenv("REDIS_TLS_URL") or "").strip() or None


def _redis_connection_kwargs() -> dict:
    """
    Connection options tuned for GCP Memorystore + blocking queue polls (BRPOP).

    socket_timeout must be None (or > JOB_WORKER_POLL_TIMEOUT) or idle workers raise
    redis.exceptions.TimeoutError during blocking reads.
    """
    poll_timeout = max(1, int(os.getenv("JOB_WORKER_POLL_TIMEOUT", "5")))
    raw_socket_timeout = os.getenv("REDIS_SOCKET_TIMEOUT", "").strip().lower()
    if raw_socket_timeout in ("", "none", "null"):
        socket_timeout = None
    else:
        socket_timeout = float(raw_socket_timeout)
        if socket_timeout > 0 and socket_timeout <= poll_timeout:
            socket_timeout = float(poll_timeout + 5)

    health_check_interval = int(os.getenv("REDIS_HEALTH_CHECK_INTERVAL", "30"))

    return {
        "decode_responses": True,
        "socket_connect_timeout": float(os.getenv("REDIS_CONNECT_TIMEOUT", "5")),
        "socket_keepalive": True,
        "health_check_interval": max(0, health_check_interval),
        "retry_on_timeout": True,
        "socket_timeout": socket_timeout,
    }


def _create_redis_client():
    if redis is None:
        return None

    redis_url = _redis_url()
    if not redis_url:
        return None

    return redis.from_url(redis_url, **_redis_connection_kwargs())


def get_redis_client():
    """Shared Redis client for short-lived API/cache operations."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    if redis is None:
        return None

    if not _redis_url():
        return None

    with _redis_lock:
        if _redis_client is None:
            _redis_client = _create_redis_client()
    return _redis_client


def get_blocking_redis_client():
    """
    Per-thread Redis client for queue workers.

    Blocking commands (BRPOP) hold a connection open for several seconds.
    A dedicated connection per worker thread avoids pool contention and makes stale
    connection recovery safer under Memorystore/VPC.
    """
    client = getattr(_blocking_local, "client", None)
    if client is not None:
        return client

    client = _create_redis_client()
    _blocking_local.client = client
    return client


def reset_redis_client():
    """Drop cached clients so the next operation reconnects."""
    global _redis_client
    _redis_client = None
    _blocking_local.client = None


def reset_blocking_redis_client():
    """Drop only the current thread's blocking worker client."""
    client = getattr(_blocking_local, "client", None)
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    _blocking_local.client = None
