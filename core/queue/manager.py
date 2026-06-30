from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Mapping
from uuid import uuid4

from core.cache import get_blocking_redis_client, get_redis_client, reset_blocking_redis_client

from .jobs import execute_job, get_job_definition
from .queues import (
    ALL_QUEUES,
    QUEUE_REALTIME,
    dead_letter_queue_name,
    processing_queue_name,
    resolve_queue_for_job,
    worker_poll_queues,
)


logger = logging.getLogger("sociovia.queue")

try:
    import redis.exceptions as redis_exceptions
except ImportError:  # pragma: no cover
    redis_exceptions = None


def _redis_transient_errors() -> tuple[type[BaseException], ...]:
    if redis_exceptions is None:
        return (TimeoutError, ConnectionError, OSError)
    return (
        redis_exceptions.TimeoutError,
        redis_exceptions.ConnectionError,
        redis_exceptions.BusyLoadingError,
        TimeoutError,
        ConnectionError,
        OSError,
    )


def _queue_client(*, blocking: bool = False):
    if blocking:
        return get_blocking_redis_client()
    return get_redis_client()


def _handle_worker_redis_error(exc: BaseException) -> None:
    logger.warning("[queue] redis transient error; reconnecting worker client: %s", exc)
    reset_blocking_redis_client()
    time.sleep(1)


def get_queue_backend() -> str:
    configured = os.getenv("JOB_QUEUE_BACKEND", "auto").strip().lower()
    if configured not in {"auto", "redis", "inline"}:
        configured = "auto"

    client = get_redis_client()
    if configured == "inline":
        return "inline"
    if configured == "redis":
        if client is None:
            raise RuntimeError("JOB_QUEUE_BACKEND=redis but REDIS_URL is not configured or redis is unavailable")
        return "redis"
    return "redis" if client is not None else "inline"


def get_queue_name() -> str:
    return QUEUE_REALTIME


def get_processing_queue_name(queue_name: str | None = None) -> str:
    return processing_queue_name(queue_name or get_queue_name())


def get_dead_letter_queue_name(queue_name: str | None = None) -> str:
    return dead_letter_queue_name(queue_name or get_queue_name())


def get_worker_poll_timeout() -> int:
    return max(1, int(os.getenv("JOB_WORKER_POLL_TIMEOUT", "5")))


def get_max_retries() -> int:
    return max(0, int(os.getenv("JOB_MAX_RETRIES", "3")))


def get_job_idempotency_ttl_seconds() -> int:
    return max(60, int(os.getenv("JOB_IDEMPOTENCY_TTL_SECONDS", "3600")))


def get_processing_stale_after_seconds() -> int:
    return max(60, int(os.getenv("JOB_PROCESSING_STALE_AFTER_SECONDS", "600")))


def _serialize(envelope: Mapping[str, Any]) -> str:
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True)


def _deserialize(raw_value: str) -> Dict[str, Any]:
    data = json.loads(raw_value)
    if not isinstance(data, dict):
        raise ValueError("Job envelope must decode to an object")
    return data


def _payload_value(payload: Mapping[str, Any] | None, *keys: str) -> Any:
    payload = payload or {}
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _resolve_idempotency_key(envelope: Mapping[str, Any]) -> str | None:
    payload = envelope.get("payload") or {}
    candidate = _payload_value(payload, "idempotency_key", "job_id", "request_id")
    if candidate is None:
        candidate = envelope.get("job_id")
    if candidate is None:
        return None
    normalized = str(candidate).strip()
    return normalized or None


def _job_done_key(queue_name: str, job_name: str, idempotency_key: str) -> str:
    return f"{queue_name}:done:{job_name}:{idempotency_key}"


def _job_inflight_key(queue_name: str, job_name: str, idempotency_key: str) -> str:
    return f"{queue_name}:inflight:{job_name}:{idempotency_key}"


def _workspace_id(payload: Mapping[str, Any] | None) -> Any:
    return _payload_value(payload, "workspace_id", "workspaceId", "ws_id")


def _emit_structured_log(*, event: str, severity: str = "INFO", **fields: Any) -> None:
    record: Dict[str, Any] = {
        "event": event,
        "severity": severity,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for key, value in fields.items():
        if value is not None:
            record[key] = value
    print(json.dumps(record, separators=(",", ":"), sort_keys=True, default=str), flush=True)


def _parse_timestamp(raw_value: Any) -> datetime | None:
    if raw_value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _claim_job_execution(client, envelope: Mapping[str, Any]) -> Dict[str, Any]:
    idempotency_key = _resolve_idempotency_key(envelope)
    if not idempotency_key:
        return {"enabled": False, "status": "disabled"}

    job_name = str(envelope.get("job_name") or "")
    queue_name = str(envelope.get("queue_name") or get_queue_name())
    ttl_seconds = get_job_idempotency_ttl_seconds()
    done_key = _job_done_key(queue_name, job_name, idempotency_key)
    inflight_key = _job_inflight_key(queue_name, job_name, idempotency_key)

    if client.get(done_key):
        return {
            "enabled": True,
            "status": "done",
            "idempotency_key": idempotency_key,
            "done_key": done_key,
            "inflight_key": inflight_key,
        }

    claimed = client.set(inflight_key, envelope.get("job_id") or job_name, ex=ttl_seconds, nx=True)
    if claimed:
        return {
            "enabled": True,
            "status": "claimed",
            "idempotency_key": idempotency_key,
            "done_key": done_key,
            "inflight_key": inflight_key,
        }

    if client.get(done_key):
        return {
            "enabled": True,
            "status": "done",
            "idempotency_key": idempotency_key,
            "done_key": done_key,
            "inflight_key": inflight_key,
        }

    return {
        "enabled": True,
        "status": "inflight",
        "idempotency_key": idempotency_key,
        "done_key": done_key,
        "inflight_key": inflight_key,
    }


def _mark_job_execution_complete(client, claim: Mapping[str, Any]) -> None:
    if not claim.get("enabled") or claim.get("status") != "claimed":
        return

    client.set(claim["done_key"], datetime.now(timezone.utc).isoformat(), ex=get_job_idempotency_ttl_seconds())
    client.delete(claim["inflight_key"])


def _release_job_execution_claim(client, claim: Mapping[str, Any]) -> None:
    if not claim.get("enabled") or claim.get("status") != "claimed":
        return
    client.delete(claim["inflight_key"])


def queue_health_snapshot() -> Dict[str, Any]:
    try:
        backend = get_queue_backend()
    except RuntimeError as exc:
        return {
            "backend": "misconfigured",
            "queue_name": get_queue_name(),
            "processing_queue_name": get_processing_queue_name(),
            "dead_letter_queue_name": get_dead_letter_queue_name(),
            "max_retries": get_max_retries(),
            "error": str(exc),
            "pending_jobs": 0,
            "processing_jobs": 0,
            "dead_letter_jobs": 0,
        }

    snapshot: Dict[str, Any] = {
        "backend": backend,
        "queue_name": get_queue_name(),
        "processing_queue_name": get_processing_queue_name(),
        "dead_letter_queue_name": get_dead_letter_queue_name(),
        "max_retries": get_max_retries(),
    }

    if backend != "redis":
        snapshot["pending_jobs"] = 0
        snapshot["processing_jobs"] = 0
        snapshot["dead_letter_jobs"] = 0
        snapshot["queues"] = {}
        return snapshot

    try:
        client = get_redis_client()
        total_pending = 0
        total_processing = 0
        total_dead = 0
        per_queue: Dict[str, Any] = {}
        for q in ALL_QUEUES:
            pending = client.llen(q)
            processing = client.llen(processing_queue_name(q))
            dead = client.llen(dead_letter_queue_name(q))
            per_queue[q] = {
                "pending": pending,
                "processing": processing,
                "dead_letter": dead,
            }
            total_pending += pending
            total_processing += processing
            total_dead += dead
        snapshot["pending_jobs"] = total_pending
        snapshot["processing_jobs"] = total_processing
        snapshot["dead_letter_jobs"] = total_dead
        snapshot["queues"] = per_queue
        snapshot["worker_mode"] = os.getenv("WORKER_MODE", "all")
    except Exception as exc:
        snapshot["backend"] = "redis-unavailable"
        snapshot["pending_jobs"] = 0
        snapshot["processing_jobs"] = 0
        snapshot["dead_letter_jobs"] = 0
        snapshot["error"] = str(exc)
    return snapshot


def enqueue_job(
    job_name: str,
    payload: Mapping[str, Any] | None = None,
    *,
    source: str = "api",
    queue_name: str | None = None,
) -> Dict[str, Any]:
    definition = get_job_definition(job_name)
    backend = get_queue_backend()
    payload_dict = dict(payload or {})
    target_queue = resolve_queue_for_job(definition.name, queue_name)
    envelope = {
        "job_id": str(uuid4()),
        "job_name": definition.name,
        "queue_name": target_queue,
        "payload": payload_dict,
        "source": source,
        "attempt": 0,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }

    if backend != "redis":
        return {
            "backend": "inline",
            "job_id": envelope["job_id"],
            "job_name": definition.name,
            "payload": payload_dict,
        }

    client = get_redis_client()
    client.lpush(target_queue, _serialize(envelope))
    depth = client.llen(target_queue)
    logger.info(
        "[queue] queued job=%s id=%s queue=%s depth=%s",
        definition.name,
        envelope["job_id"],
        target_queue,
        depth,
    )
    _emit_structured_log(
        event="job.dispatch",
        job=definition.name,
        status="queued",
        job_id=envelope["job_id"],
        source=source,
        queue=target_queue,
        depth=depth,
        workspace_id=_workspace_id(payload_dict),
        idempotency_key=_resolve_idempotency_key(envelope),
    )
    return {
        "backend": "redis",
        "job_id": envelope["job_id"],
        "job_name": definition.name,
        "queue_name": target_queue,
        "depth": depth,
        "payload": payload_dict,
    }


def dispatch_internal_job(job_name: str, payload: Mapping[str, Any] | None = None, *, source: str = "api") -> Dict[str, Any]:
    queued = enqueue_job(job_name, payload, source=source)
    if queued["backend"] == "redis":
        return {
            "ok": True,
            "job": job_name,
            "status": "queued",
            "job_id": queued["job_id"],
            "queue_name": queued["queue_name"],
            "depth": queued["depth"],
            "payload": queued["payload"],
        }

    executed = execute_job(job_name, queued["payload"])
    return {
        "ok": True,
        "job": job_name,
        "status": "completed-inline",
        "job_id": queued["job_id"],
        **executed,
    }


def _move_to_dead_letter(client, raw_envelope: str, envelope: Mapping[str, Any], error: Exception) -> None:
    failed_envelope = dict(envelope)
    failed_envelope["failed_at"] = datetime.now(timezone.utc).isoformat()
    failed_envelope["error"] = str(error)
    queue_name = str(envelope.get("queue_name") or get_queue_name())
    client.lpush(dead_letter_queue_name(queue_name), _serialize(failed_envelope))


def _handle_job_failure(client, raw_envelope: str, envelope: Dict[str, Any], error: Exception) -> None:
    queue_name = str(envelope.get("queue_name") or get_queue_name())
    proc_q = processing_queue_name(queue_name)
    client.lrem(proc_q, 1, raw_envelope)

    attempt = int(envelope.get("attempt", 0)) + 1
    envelope["attempt"] = attempt
    envelope["last_error"] = str(error)
    envelope["last_failed_at"] = datetime.now(timezone.utc).isoformat()

    if attempt > get_max_retries():
        _move_to_dead_letter(client, raw_envelope, envelope, error)
        _emit_structured_log(
            event="job.run",
            job=envelope.get("job_name"),
            status="dead-letter",
            job_id=envelope.get("job_id"),
            attempt=attempt,
            max_retries=get_max_retries(),
            error=str(error),
            source=envelope.get("source"),
            workspace_id=_workspace_id(envelope.get("payload") or {}),
            idempotency_key=_resolve_idempotency_key(envelope),
        )
        logger.exception(
            "[queue] job=%s id=%s exhausted retries and moved to dead-letter queue",
            envelope.get("job_name"),
            envelope.get("job_id"),
        )
        return

    client.lpush(queue_name, _serialize(envelope))
    _emit_structured_log(
        event="job.run",
        job=envelope.get("job_name"),
        status="retry",
        job_id=envelope.get("job_id"),
        attempt=attempt,
        max_retries=get_max_retries(),
        error=str(error),
        source=envelope.get("source"),
        workspace_id=_workspace_id(envelope.get("payload") or {}),
        idempotency_key=_resolve_idempotency_key(envelope),
    )
    logger.exception(
        "[queue] job=%s id=%s failed on attempt=%s and was re-queued",
        envelope.get("job_name"),
        envelope.get("job_id"),
        attempt,
    )
    time.sleep(min(attempt, 3))


def run_worker_once(poll_queues: list[str] | None = None) -> Dict[str, Any] | None:
    if get_queue_backend() != "redis":
        raise RuntimeError("Worker requires JOB_QUEUE_BACKEND=redis (or auto with REDIS_URL configured)")

    client = _queue_client(blocking=True)
    queues = poll_queues or worker_poll_queues()
    popped = client.brpop(queues, timeout=get_worker_poll_timeout())
    if popped is None:
        return None

    source_queue, raw_value = popped
    if isinstance(source_queue, bytes):
        source_queue = source_queue.decode("utf-8")
    queue_name = str(source_queue)
    proc_q = processing_queue_name(queue_name)
    client.lpush(proc_q, raw_value)

    envelope = _deserialize(raw_value)
    envelope.setdefault("queue_name", queue_name)
    envelope["processing_started_at"] = datetime.now(timezone.utc).isoformat()
    raw_envelope = _serialize(envelope)
    client.lrem(proc_q, 1, raw_value)
    client.lpush(proc_q, raw_envelope)

    job_name = envelope["job_name"]
    job_id = envelope.get("job_id")
    payload = envelope.get("payload") or {}
    claim = _claim_job_execution(client, envelope)
    started_at = time.perf_counter()

    if claim.get("status") in {"done", "inflight"}:
        client.lrem(proc_q, 1, raw_envelope)
        duplicate_reason = "already-completed" if claim["status"] == "done" else "already-processing"
        logger.info(
            "[queue] skipped duplicate job=%s id=%s reason=%s",
            job_name,
            job_id,
            duplicate_reason,
        )
        _emit_structured_log(
            event="job.run",
            job=job_name,
            status="duplicate-skipped",
            job_id=job_id,
            duplicate_reason=duplicate_reason,
            source=envelope.get("source"),
            workspace_id=_workspace_id(payload),
            idempotency_key=claim.get("idempotency_key"),
            queue=queue_name,
        )
        return {
            "job_id": job_id,
            "status": "duplicate-skipped",
            "job": job_name,
            "result": {},
            "duplicate_reason": duplicate_reason,
        }

    try:
        executed = execute_job(job_name, payload)
        client.lrem(proc_q, 1, raw_envelope)
        _mark_job_execution_complete(client, claim)
        elapsed_ms = round((time.perf_counter() - started_at) * 1000)
        logger.info(
            "[queue] completed job=%s id=%s queue=%s elapsed=%sms",
            job_name,
            job_id,
            queue_name,
            elapsed_ms,
        )
        _emit_structured_log(
            event="job.run",
            job=job_name,
            status="success",
            job_id=job_id,
            duration_ms=elapsed_ms,
            attempt=envelope.get("attempt", 0),
            source=envelope.get("source"),
            workspace_id=_workspace_id(payload),
            idempotency_key=claim.get("idempotency_key"),
            queue=queue_name,
        )
        return {
            "job_id": job_id,
            "status": "completed",
            **executed,
        }
    except Exception as exc:
        _release_job_execution_claim(client, claim)
        _handle_job_failure(client, raw_envelope, envelope, exc)
        raise


def retry_dead_letter_jobs(*, limit: int = 25, job_name: str | None = None, source: str = "api") -> Dict[str, Any]:
    if get_queue_backend() != "redis":
        raise RuntimeError("Dead-letter retries require JOB_QUEUE_BACKEND=redis")

    client = get_redis_client()
    requested_limit = max(1, int(limit))
    retried_jobs = 0
    skipped_jobs = 0
    skipped_raw_envelopes: list[str] = []

    while retried_jobs < requested_limit:
        raw_envelope = client.rpop(get_dead_letter_queue_name())
        if raw_envelope is None:
            break

        envelope = _deserialize(raw_envelope)
        if job_name and envelope.get("job_name") != job_name:
            skipped_jobs += 1
            skipped_raw_envelopes.append(raw_envelope)
            continue

        envelope["attempt"] = 0
        envelope["last_error"] = None
        envelope["last_failed_at"] = None
        envelope["retried_from_dead_letter_at"] = datetime.now(timezone.utc).isoformat()
        envelope["source"] = f"{source}:dead-letter-retry"
        client.lpush(get_queue_name(), _serialize(envelope))
        retried_jobs += 1
        _emit_structured_log(
            event="job.dead_letter.retry",
            job=envelope.get("job_name"),
            status="requeued",
            job_id=envelope.get("job_id"),
            source=envelope.get("source"),
            workspace_id=_workspace_id(envelope.get("payload") or {}),
            idempotency_key=_resolve_idempotency_key(envelope),
        )

    for raw_envelope in reversed(skipped_raw_envelopes):
        client.rpush(get_dead_letter_queue_name(), raw_envelope)

    return {
        "ok": True,
        "queue_name": get_queue_name(),
        "dead_letter_queue_name": get_dead_letter_queue_name(),
        "requested_limit": requested_limit,
        "retried_jobs": retried_jobs,
        "skipped_jobs": skipped_jobs,
        "remaining_dead_letter_jobs": client.llen(get_dead_letter_queue_name()),
    }


def recover_stale_processing_jobs(*, limit: int = 25) -> Dict[str, Any]:
    if get_queue_backend() != "redis":
        return {
            "ok": True,
            "backend": get_queue_backend(),
            "recovered_jobs": 0,
        }

    client = _queue_client(blocking=True)
    now = datetime.now(timezone.utc)
    stale_after_seconds = get_processing_stale_after_seconds()
    recovered_jobs = 0

    for raw_envelope in client.lrange(get_processing_queue_name(), 0, max(0, int(limit) - 1)):
        envelope = _deserialize(raw_envelope)
        reference_time = (
            _parse_timestamp(envelope.get("processing_started_at"))
            or _parse_timestamp(envelope.get("last_failed_at"))
            or _parse_timestamp(envelope.get("enqueued_at"))
        )
        if reference_time is None:
            continue

        age_seconds = (now - reference_time).total_seconds()
        if age_seconds < stale_after_seconds:
            continue

        removed = client.lrem(get_processing_queue_name(), 1, raw_envelope)
        if not removed:
            continue

        envelope["recovered_from_processing_at"] = now.isoformat()
        envelope["processing_started_at"] = None
        client.lpush(get_queue_name(), _serialize(envelope))
        recovered_jobs += 1
        _emit_structured_log(
            event="job.processing.recovered",
            job=envelope.get("job_name"),
            job_id=envelope.get("job_id"),
            source=envelope.get("source"),
            age_seconds=round(age_seconds),
            workspace_id=_workspace_id(envelope.get("payload") or {}),
            idempotency_key=_resolve_idempotency_key(envelope),
        )

    return {
        "ok": True,
        "queue_name": get_queue_name(),
        "processing_queue_name": get_processing_queue_name(),
        "recovered_jobs": recovered_jobs,
        "stale_after_seconds": stale_after_seconds,
    }


def start_worker(*, stop_event: threading.Event | None = None) -> None:
    backend = get_queue_backend()
    if backend != "redis":
        raise RuntimeError("Queue worker can only run with Redis-backed jobs")

    mode = os.getenv("WORKER_MODE", "all").strip().lower()
    poll = worker_poll_queues(mode)
    logger.info(
        "[queue] worker starting mode=%s backend=%s queues=%s",
        mode,
        backend,
        poll,
    )

    for q in ALL_QUEUES:
        recover_stale_processing_jobs_for_queue(q)

    if mode == "ai":
        from whatsapp.ai_job import get_ai_concurrency_limit, prewarm_ai_runtime

        prewarm_ai_runtime()
        concurrency = get_ai_concurrency_limit()
        logger.info("[queue] AI worker concurrency=%s", concurrency)

        def _ai_worker_loop(worker_idx: int) -> None:
            while stop_event is None or not stop_event.is_set():
                try:
                    run_worker_once(poll_queues=[poll[0]])
                except _redis_transient_errors() as exc:
                    _handle_worker_redis_error(exc)
                except Exception:
                    logger.exception("[queue] AI worker thread %s iteration failed", worker_idx)

        threads = [
            threading.Thread(
                target=_ai_worker_loop,
                args=(idx,),
                daemon=True,
                name=f"wa-ai-worker-{idx}",
            )
            for idx in range(concurrency)
        ]
        for thread in threads:
            thread.start()
        while stop_event is None or not stop_event.is_set():
            time.sleep(0.5)
        logger.info("[queue] AI worker stopped")
        return

    while stop_event is None or not stop_event.is_set():
        try:
            run_worker_once(poll_queues=poll)
        except _redis_transient_errors() as exc:
            _handle_worker_redis_error(exc)
        except Exception:
            logger.exception("[queue] worker iteration failed")

    logger.info("[queue] worker stopped")


def recover_stale_processing_jobs_for_queue(queue_name: str, *, limit: int = 25) -> Dict[str, Any]:
    if get_queue_backend() != "redis":
        return {"ok": True, "recovered_jobs": 0}

    client = _queue_client(blocking=True)
    proc_q = processing_queue_name(queue_name)
    now = datetime.now(timezone.utc)
    stale_after_seconds = get_processing_stale_after_seconds()
    recovered_jobs = 0

    for raw_envelope in client.lrange(proc_q, 0, max(0, int(limit) - 1)):
        envelope = _deserialize(raw_envelope)
        reference_time = (
            _parse_timestamp(envelope.get("processing_started_at"))
            or _parse_timestamp(envelope.get("last_failed_at"))
            or _parse_timestamp(envelope.get("enqueued_at"))
        )
        if reference_time is None:
            continue
        age_seconds = (now - reference_time).total_seconds()
        if age_seconds < stale_after_seconds:
            continue
        removed = client.lrem(proc_q, 1, raw_envelope)
        if not removed:
            continue
        envelope["recovered_from_processing_at"] = now.isoformat()
        envelope["processing_started_at"] = None
        envelope.setdefault("queue_name", queue_name)
        client.lpush(queue_name, _serialize(envelope))
        recovered_jobs += 1

    return {"ok": True, "queue_name": queue_name, "recovered_jobs": recovered_jobs}
