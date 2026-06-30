"""
Cache Helpers
=============

High-level caching utilities built on top of the existing Redis client.

Usage:
    from core.cache.helpers import cache_get, cache_set, cache_delete, cache_invalidate_pattern

    # Read-through cache
    data = cache_get("workspace:42")
    if data is None:
        data = fetch_from_db(42)
        cache_set("workspace:42", data, ttl=300)

    # Invalidate on write
    cache_delete("workspace:42")

RULES:
    - Never cache writes
    - Only cache reads
    - Always invalidate on mutation
    - Use short TTLs for volatile data
    - All operations are SAFE — if Redis is down, they return None/False silently
"""

import json
import hashlib
import logging
from typing import Any, Optional

from .redis_client import get_redis_client, reset_redis_client

logger = logging.getLogger(__name__)

# Key prefix to namespace all cache entries and avoid collisions with queue keys
CACHE_PREFIX = "sociovia:cache:"


def _prefixed_key(key: str) -> str:
    return f"{CACHE_PREFIX}{key}"


def _reset_client_on_connection_error(exc: Exception) -> None:
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        reset_redis_client()
        return
    try:
        import redis.exceptions as redis_exc

        if isinstance(exc, (redis_exc.TimeoutError, redis_exc.ConnectionError)):
            reset_redis_client()
    except ImportError:
        pass


def cache_get(key: str) -> Optional[Any]:
    """
    Get a cached value by key.

    Returns None if key doesn't exist, Redis is unavailable,
    or deserialization fails. NEVER raises.
    """
    try:
        client = get_redis_client()
        if client is None:
            return None

        raw = client.get(_prefixed_key(key))
        if raw is None:
            return None

        return json.loads(raw)
    except Exception as e:
        _reset_client_on_connection_error(e)
        logger.debug("[cache] get failed for key=%s: %s", key, e)
        return None


def cache_set(key: str, value: Any, ttl: int = 300) -> bool:
    """
    Set a cached value with TTL (seconds).

    Default TTL is 5 minutes. Returns True on success, False if Redis
    is unavailable. NEVER raises.

    Args:
        key: Cache key
        value: Any JSON-serializable value
        ttl: Time-to-live in seconds (default 300 = 5 min)
    """
    try:
        client = get_redis_client()
        if client is None:
            return False

        serialized = json.dumps(value, separators=(",", ":"), default=str)
        client.setex(_prefixed_key(key), ttl, serialized)
        return True
    except Exception as e:
        _reset_client_on_connection_error(e)
        logger.debug("[cache] set failed for key=%s: %s", key, e)
        return False


def cache_delete(key: str) -> bool:
    """
    Delete a cached value. Used for cache invalidation on writes.

    Returns True if deleted, False if Redis unavailable. NEVER raises.
    """
    try:
        client = get_redis_client()
        if client is None:
            return False

        client.delete(_prefixed_key(key))
        return True
    except Exception as e:
        _reset_client_on_connection_error(e)
        logger.debug("[cache] delete failed for key=%s: %s", key, e)
        return False


def cache_invalidate_pattern(pattern: str) -> int:
    """
    Invalidate all keys matching a pattern.

    Example:
        cache_invalidate_pattern("workspace:42:*")

    Uses SCAN (not KEYS) to avoid blocking Redis.
    Returns count of deleted keys.
    """
    try:
        client = get_redis_client()
        if client is None:
            return 0

        full_pattern = _prefixed_key(pattern)
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=full_pattern, count=100)
            if keys:
                client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break

        if deleted > 0:
            logger.info("[cache] invalidated %d keys matching %s", deleted, pattern)
        return deleted
    except Exception as e:
        _reset_client_on_connection_error(e)
        logger.debug("[cache] invalidate_pattern failed for %s: %s", pattern, e)
        return 0


def cache_hash_key(*parts: str) -> str:
    """
    Generate a deterministic cache key from multiple parts.

    Useful for caching AI responses where the key is a hash of the prompt.

    Example:
        key = cache_hash_key("ai:workspace", url, str(max_snapshots))
    """
    combined = "|".join(str(p) for p in parts)
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
