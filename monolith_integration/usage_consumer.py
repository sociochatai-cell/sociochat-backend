"""Poll WhatsApp usage-events API with DB checkpoint + dedupe (replay-safe)."""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import func, text

from shared_models import db

from .config import MonolithWhatsappConfig
from .http_client import request_with_retries
from .metrics import integration_metrics
from .structured_log import mi_log

Handler = Callable[[Dict[str, Any], Dict[str, Any]], None]


def _usage_inline_poll_enabled() -> bool:
    raw = (os.getenv("WHATSAPP_USAGE_INLINE_POLL") or "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def poll_usage_events_for_scheduler() -> Dict[str, Any]:
    """
    Run usage-event poll batches during Cloud Scheduler /tick.

    Replaces the always-on whatsapp-usage-consumer Cloud Run service.
    """
    if (os.getenv("WHATSAPP_USAGE_BILLING_ON_SCHEDULER_TICK") or "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return {"skipped": True, "reason": "disabled"}

    consumer = WhatsappUsageEventConsumer.with_default_handler()
    max_batches = max(1, int(os.getenv("WHATSAPP_USAGE_POLL_BATCHES_PER_TICK", "5") or "5"))
    summary: Dict[str, Any] = {"batches": 0, "results": []}
    for _ in range(max_batches):
        result = consumer.poll_once()
        summary["results"].append(result)
        summary["batches"] += 1
        if result in ("empty", "not_configured", "http_fail"):
            break
    summary["last"] = summary["results"][-1] if summary["results"] else None
    return summary


def _validate_message_sent(event: Dict[str, Any], payload: Dict[str, Any]) -> Tuple[bool, str]:
    if (event.get("event_type") or "") != "message_sent":
        return False, "unsupported_event_type"
    if payload.get("event") != "message_sent":
        return False, "payload_missing_event"
    if payload.get("category") != "outbound":
        return False, "payload_bad_category"
    try:
        int(payload.get("account_id") or event.get("account_id") or 0)
    except (TypeError, ValueError):
        return False, "missing_account_id"
    return True, ""


class WhatsappUsageEventConsumer:
    def __init__(
        self,
        config: MonolithWhatsappConfig,
        *,
        handler: Handler,
    ):
        self._cfg = config
        self._handler = handler

    @classmethod
    def with_default_handler(cls) -> "WhatsappUsageEventConsumer":
        from .accounting import default_message_sent_billing_handler

        return cls(MonolithWhatsappConfig.from_env(), handler=default_message_sent_billing_handler)

    def _load_checkpoint(self) -> int:
        row = db.session.execute(
            text(
                "SELECT last_event_id FROM whatsapp_usage_event_checkpoints "
                "WHERE consumer_name = :cn LIMIT 1"
            ),
            {"cn": self._cfg.consumer_name},
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _set_checkpoint(self, last_event_id: int) -> None:
        db.session.execute(
            text(
                """
                INSERT INTO whatsapp_usage_event_checkpoints (consumer_name, last_event_id, updated_at)
                VALUES (:cn, :lid, NOW())
                ON CONFLICT (consumer_name) DO UPDATE SET
                    last_event_id = EXCLUDED.last_event_id,
                    updated_at = NOW()
                """
            ),
            {"cn": self._cfg.consumer_name, "lid": last_event_id},
        )
        mi_log(
            "usage_checkpoint_advanced",
            consumer_name=self._cfg.consumer_name,
            last_event_id=int(last_event_id),
        )

    def _try_claim_processed(self, event_key: str, source_event_id: int) -> bool:
        row = db.session.execute(
            text(
                """
                INSERT INTO whatsapp_usage_events_processed (event_key, source_event_id, consumer_name)
                VALUES (:ek, :sid, :cn)
                ON CONFLICT (event_key) DO NOTHING
                RETURNING event_key
                """
            ),
            {"ek": event_key, "sid": source_event_id, "cn": self._cfg.consumer_name},
        ).fetchone()
        return row is not None

    def _quarantine(self, event: Dict[str, Any], error_message: str) -> None:
        db.session.execute(
            text(
                """
                INSERT INTO whatsapp_usage_events_quarantine
                    (consumer_name, source_event_id, event_key, raw_payload, error_message)
                VALUES (:cn, :sid, :ek, CAST(:raw AS jsonb), :err)
                """
            ),
            {
                "cn": self._cfg.consumer_name,
                "sid": event.get("id"),
                "ek": event.get("event_key"),
                "raw": json.dumps(event, default=str),
                "err": error_message[:2000],
            },
        )
        integration_metrics.inc("usage_quarantine")
        mi_log(
            "usage_event_quarantine_inserted",
            consumer_name=self._cfg.consumer_name,
            source_event_id=event.get("id"),
            event_key=event.get("event_key"),
            reason=error_message,
        )

    def _direct_fetch(self, since_id: int) -> Optional[Dict[str, Any]]:
        """Read usage events from shared DB (no HTTP self-call on scheduler tick)."""
        from whatsapp.models import WhatsAppUsageEvent

        rows = (
            WhatsAppUsageEvent.query.filter(
                WhatsAppUsageEvent.id > since_id,
                WhatsAppUsageEvent.event_type == "message_sent",
            )
            .order_by(WhatsAppUsageEvent.id.asc())
            .limit(self._cfg.poll_limit)
            .all()
        )
        stream_max_id = db.session.query(func.max(WhatsAppUsageEvent.id)).scalar()
        stream_max_id = int(stream_max_id) if stream_max_id is not None else 0
        last_id = rows[-1].id if rows else since_id
        return {
            "success": True,
            "count": len(rows),
            "since_id": since_id,
            "last_id": int(last_id) if last_id is not None else since_id,
            "stream_max_id": stream_max_id,
            "events": [row.to_dict() for row in rows],
        }

    def _fetch_batch(self, since_id: int) -> Optional[Dict[str, Any]]:
        if _usage_inline_poll_enabled():
            return self._direct_fetch(since_id)
        return self._http_fetch(since_id)

    def _http_fetch(self, since_id: int) -> Optional[Dict[str, Any]]:
        url = (
            f"{self._cfg.usage_events_url()}"
            f"?since_id={since_id}&limit={self._cfg.poll_limit}&event_type=message_sent"
        )
        headers = {"Authorization": f"Bearer {self._cfg.usage_events_secret}"}

        def _on_retry(_attempt: int, _reason: str) -> None:
            integration_metrics.inc("usage_http_retries")

        resp, err = request_with_retries(
            "GET",
            url,
            headers=headers,
            json_body=None,
            timeout=self._cfg.http_timeout_seconds,
            max_retries=self._cfg.max_retries,
            backoff_base=self._cfg.backoff_base_seconds,
            log_context={"op": "usage_events_poll", "since_id": since_id},
            on_retry_attempt=_on_retry,
        )
        if resp is None:
            mi_log("usage_events_http_failed", since_id=since_id, error=err)
            return None
        if resp.status_code != 200:
            mi_log("usage_events_http_failed", since_id=since_id, status_code=resp.status_code, body=resp.text[:300])
            return None
        try:
            return resp.json()
        except ValueError as exc:
            mi_log("usage_events_json_failed", since_id=since_id, error=str(exc))
            return None

    def poll_once(self) -> str:
        """
        One poll + DB updates (requires Flask app context + DB tables from migration 006).

        Returns:
            ``ok`` | ``http_fail`` | ``not_configured`` | ``empty``
        """
        if not self._cfg.is_usage_consumer_configured():
            mi_log("usage_consumer_not_configured")
            return "not_configured"

        since_id = self._load_checkpoint()
        data = self._fetch_batch(since_id)
        if data is None:
            return "http_fail"

        events: List[Dict[str, Any]] = data.get("events") or []
        stream_max_id = int(data.get("stream_max_id") or 0)
        lag = max(0, stream_max_id - since_id)

        mi_log(
            "usage_consumer_poll",
            since_id=since_id,
            batch_size=len(events),
            lag=lag,
            stream_max_id=stream_max_id,
        )
        integration_metrics.inc("usage_polls")

        if not events:
            return "empty"

        for ev in events:
            eid = int(ev["id"])
            ek = str(ev.get("event_key") or "")
            payload = ev.get("payload") or {}

            if not ek:
                try:
                    self._quarantine(ev, "missing_event_key")
                    self._set_checkpoint(eid)
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    mi_log("usage_consumer_quarantine_failed", event_id=eid, error=str(exc))
                    return "http_fail"
                continue

            ok, reason = _validate_message_sent(ev, payload)
            if not ok:
                try:
                    self._quarantine(ev, reason)
                    self._set_checkpoint(eid)
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    mi_log("usage_consumer_quarantine_failed", event_id=eid, error=str(exc))
                    return "http_fail"
                mi_log("usage_event_quarantined", event_id=eid, event_key=ek, reason=reason)
                continue

            try:
                inserted = self._try_claim_processed(ek, eid)
                if not inserted:
                    self._set_checkpoint(eid)
                    db.session.commit()
                    integration_metrics.inc("usage_dedupe_hits")
                    integration_metrics.inc("usage_replays")
                    mi_log(
                        "usage_event_replay_detected",
                        consumer_name=self._cfg.consumer_name,
                        event_id=eid,
                        event_key=ek,
                    )
                    continue

                self._handler(ev, payload)
                self._set_checkpoint(eid)
                db.session.commit()
                integration_metrics.inc("usage_events_applied")
                mi_log("usage_event_applied", event_id=eid, event_key=ek, account_id=payload.get("account_id"))
            except Exception as exc:
                db.session.rollback()
                mi_log("usage_event_apply_failed", event_id=eid, event_key=ek, error=str(exc))
                return "http_fail"

        return "ok"

    def run_forever(self) -> None:
        while True:
            self.poll_once()
            time.sleep(self._cfg.poll_interval_seconds)
