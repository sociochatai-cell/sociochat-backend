"""
Webhook health + subscription integrity (advisory-only)
=========================================================

Polls Meta `subscribed_apps`, optionally probes public callback URL, correlates
recent `whatsapp_webhook_logs` for delivery-class signals, and persists
`webhook_health` + `webhook_echo_validation` on `whatsapp_accounts`.

Does not block sends, disconnect accounts, or mutate billing.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import func

from shared_models import db
from .models import WhatsAppAccount, WhatsAppWebhookLog
from .utils import subscribe_waba_to_app

logger = logging.getLogger(__name__)

# ── persisted webhook_health values (operator-facing) ─────────────────────────
WEBHOOK_HEALTH_HEALTHY = "healthy"
WEBHOOK_HEALTH_DEGRADED = "degraded"
WEBHOOK_HEALTH_MISSING_SUBSCRIPTION = "missing_subscription"
WEBHOOK_HEALTH_INACTIVE = "inactive"
WEBHOOK_HEALTH_FAILING = "failing"
WEBHOOK_HEALTH_UNKNOWN = "unknown"

DEFAULT_API_VERSION = os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION", "v22.0")
META_GRAPH = f"https://graph.facebook.com/{DEFAULT_API_VERSION}"

# Staleness / watchdog (hours)
_STALE_NO_WEBHOOK_HOURS = int(os.getenv("WH_WEBHOOK_STALE_HOURS", "48"))
_CALLBACK_TIMEOUT = float(os.getenv("WH_WEBHOOK_CALLBACK_TIMEOUT_SEC", "8"))


def _our_app_ids() -> List[str]:
    ids: List[str] = []
    for k in ("META_APP_ID", "FB_APP_ID"):
        v = (os.getenv(k) or "").strip()
        if v and v not in ids:
            ids.append(v)
    return ids


def fetch_subscribed_apps(waba_id: str, access_token: str) -> Tuple[bool, Dict[str, Any]]:
    """GET /{waba-id}/subscribed_apps — returns (http_ok, body_or_error)."""
    try:
        r = requests.get(
            f"{META_GRAPH}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        try:
            j = r.json() if r.content else {}
        except ValueError:
            j = {"error": {"message": f"non_json_body_http_{r.status_code}"}}
        return r.status_code == 200 and "error" not in j, j
    except Exception as e:
        logger.warning("subscribed_apps request failed waba=%s err=%s", waba_id, e)
        return False, {"error": {"message": str(e)}}


def our_app_is_subscribed(apps_payload: Dict[str, Any]) -> Tuple[bool, List[Dict[str, Any]]]:
    """True if any subscribed app row matches our configured app id(s)."""
    apps = apps_payload.get("data") or []
    want = set(_our_app_ids())
    if not want:
        return False, apps
    for row in apps:
        rid = str(row.get("id") or "")
        if rid in want:
            return True, apps
        nested = row.get("whatsapp_business_api_data") or {}
        nid = str(nested.get("id") or "")
        if nid in want:
            return True, apps
    return False, apps


def get_public_webhook_probe_url() -> Optional[str]:
    """
    URL used to verify our callback endpoint is reachable from this runtime.
    Prefer APP_BASE_URL (WhatsApp service public URL).
    """
    base = (os.getenv("APP_BASE_URL") or os.getenv("WHATSAPP_PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        return None
    return f"{base}/api/whatsapp/webhook/test"


def probe_callback_reachable(url: Optional[str]) -> Tuple[bool, Optional[str], Optional[float]]:
    if not url:
        return False, "no_probe_url_configured", None
    try:
        t0 = datetime.now(timezone.utc)
        r = requests.get(url, timeout=_CALLBACK_TIMEOUT)
        elapsed = (datetime.now(timezone.utc) - t0).total_seconds() * 1000
        if r.status_code < 500:
            return True, None, elapsed
        return False, f"http_{r.status_code}", elapsed
    except Exception as e:
        return False, str(e)[:500], None


def _webhook_log_counts(
    phone_number_id: str,
    since: datetime,
) -> Dict[str, int]:
    """Count recent webhook log rows by event_type for this phone."""
    q = (
        db.session.query(WhatsAppWebhookLog.event_type, func.count(WhatsAppWebhookLog.id))
        .filter(
            WhatsAppWebhookLog.phone_number_id == phone_number_id,
            WhatsAppWebhookLog.received_at >= since,
        )
        .group_by(WhatsAppWebhookLog.event_type)
    )
    out: Dict[str, int] = {}
    for et, cnt in q.all():
        key = et or "unknown"
        out[key] = int(cnt or 0)
    return out


def merge_echo_validation(
    current: Optional[Dict[str, Any]],
    last_seen_updates: Dict[str, str],
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    base = dict(current) if isinstance(current, dict) else {}
    ls = dict(base.get("last_seen") or {})
    for k, iso in last_seen_updates.items():
        ls[k] = iso
    base["last_seen"] = ls
    if extra:
        base["last_integrity"] = {**(base.get("last_integrity") or {}), **extra}
    return base


def touch_inbound_signals(waba_id: str, signal_keys: List[str]) -> None:
    """
    Lightweight update after a real webhook delivery (advisory counters).
    signal_keys: logical channels e.g. messages, statuses, template_status, echo, account_update
    """
    if not waba_id or not signal_keys:
        return
    try:
        account = WhatsAppAccount.query.filter_by(waba_id=str(waba_id), is_active=True).first()
        if not account:
            return
        now = datetime.now(timezone.utc)
        updates = {k: now.isoformat() for k in signal_keys}
        account.webhook_echo_validation = merge_echo_validation(
            account.webhook_echo_validation,
            updates,
            {"last_touch_source": "webhook_inbound"},
        )
        account.webhook_last_event_at = now
        account.last_inbound_webhook_at = now
        db.session.commit()
    except Exception as e:
        logger.warning("touch_inbound_signals failed waba=%s: %s", waba_id, e)
        try:
            db.session.rollback()
        except Exception:
            pass


def map_entry_changes_to_signal_keys(changes: List[Dict[str, Any]]) -> List[str]:
    keys: List[str] = []
    for change in changes or []:
        field = (change.get("field") or "").strip()
        value = change.get("value") or {}
        if field == "message_template_status_update":
            keys.append("message_template_status_update")
        elif field in ("message_echoes", "smb_message_echoes"):
            keys.append("echo")
        elif field == "account_update":
            keys.append("account_update")
        elif field in ("message_template_quality_update", "template_category_update"):
            keys.append("template_updates")
        elif field == "messages":
            if value.get("statuses"):
                keys.append("statuses")
            if value.get("messages"):
                keys.append("messages")
            if "history" in value:
                keys.append("history_sync")
            if "smb_app_state_sync" in value or (
                "contacts" in value and "messages" not in value and "statuses" not in value
            ):
                keys.append("contacts_sync")
        elif field in ("history", "history_sync"):
            keys.append("history_sync")
        elif field == "smb_app_state_sync":
            keys.append("contacts_sync")
    # dedupe preserving order
    seen = set()
    out: List[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _pick_health(
    *,
    token_ok: bool,
    graph_ok: bool,
    apps_non_empty: bool,
    our_app_ok: bool,
    callback_ok: Optional[bool],
    reasons: List[str],
    log_counts: Dict[str, int],
    last_inbound: Optional[datetime],
    is_active: bool,
    is_coexistence: bool,
    coexistence_paired_at: Optional[datetime],
) -> str:
    now = datetime.now(timezone.utc)
    if not is_active:
        return WEBHOOK_HEALTH_INACTIVE
    if not token_ok:
        return WEBHOOK_HEALTH_FAILING
    if not graph_ok:
        return WEBHOOK_HEALTH_FAILING
    if not apps_non_empty or not our_app_ok:
        return WEBHOOK_HEALTH_MISSING_SUBSCRIPTION
    if callback_ok is False:
        return WEBHOOK_HEALTH_FAILING
    now = datetime.now(timezone.utc)
    if last_inbound and (now - last_inbound) > timedelta(hours=_STALE_NO_WEBHOOK_HOURS):
        reasons.append("stale_no_inbound_webhook")
        return WEBHOOK_HEALTH_DEGRADED
    msgs = log_counts.get("message", 0) + log_counts.get("messages", 0)
    sts = log_counts.get("status", 0) + log_counts.get("statuses", 0)
    if msgs > 5 and sts == 0 and last_inbound and (now - last_inbound) < timedelta(hours=24):
        reasons.append("missing_status_events_recent")
        return WEBHOOK_HEALTH_DEGRADED
    if is_coexistence and coexistence_paired_at:
        echo_logs = log_counts.get("echo", 0)
        if echo_logs == 0 and (now - coexistence_paired_at) > timedelta(days=3):
            reasons.append("coexistence_echo_not_observed")
            return WEBHOOK_HEALTH_DEGRADED
    if reasons:
        return WEBHOOK_HEALTH_DEGRADED
    return WEBHOOK_HEALTH_HEALTHY


def validate_and_persist_account(account: WhatsAppAccount, *, probe_callback: bool = True) -> Dict[str, Any]:
    """
    Run full integrity check and persist columns on `account`.
    Returns a structured report (for APIs / logs).
    """
    now = datetime.now(timezone.utc)
    reasons: List[str] = []
    token = account.get_access_token()
    token_ok = bool(token)
    if not token_ok:
        reasons.append("missing_token")

    our_app_ok = False
    apps_body: Dict[str, Any] = {}
    apps_non_empty = False
    graph_ok = False
    if token_ok:
        ok, apps_body = fetch_subscribed_apps(account.waba_id, token)
        graph_ok = ok and "error" not in apps_body
        apps = apps_body.get("data") if isinstance(apps_body.get("data"), list) else []
        apps_non_empty = bool(apps)
        if graph_ok:
            our_app_ok, _ = our_app_is_subscribed(apps_body)
        else:
            err = (apps_body.get("error") or {}).get("message", "subscribed_apps_error")
            reasons.append(f"graph:{err}")
            account.webhook_last_error = err[:2000] if isinstance(err, str) else str(err)[:2000]

    callback_ok: Optional[bool] = None
    callback_detail: Optional[str] = None
    callback_ms: Optional[float] = None
    if probe_callback:
        url = get_public_webhook_probe_url()
        callback_ok, callback_detail, callback_ms = probe_callback_reachable(url)

    since = now - timedelta(hours=24)
    log_counts = _webhook_log_counts(account.phone_number_id, since)
    last_inbound = account.last_inbound_webhook_at or account.last_webhook_received

    health = _pick_health(
        token_ok=token_ok,
        graph_ok=graph_ok,
        apps_non_empty=apps_non_empty,
        our_app_ok=our_app_ok,
        callback_ok=callback_ok,
        reasons=reasons,
        log_counts=log_counts,
        last_inbound=last_inbound,
        is_active=bool(account.is_active),
        is_coexistence=bool(getattr(account, "is_coexistence", False)),
        coexistence_paired_at=getattr(account, "coexistence_paired_at", None),
    )

    prev_health = (account.webhook_health or WEBHOOK_HEALTH_UNKNOWN).strip()
    account.webhook_health = health
    account.webhook_last_checked_at = now
    account.webhook_last_validated_at = now
    if graph_ok and apps_non_empty and our_app_ok:
        account.webhook_subscription_status = "subscribed"
    elif graph_ok and apps_non_empty and not our_app_ok:
        account.webhook_subscription_status = "not_subscribed"
    elif graph_ok and not apps_non_empty:
        account.webhook_subscription_status = "not_subscribed"
    else:
        account.webhook_subscription_status = "error"
    account.webhook_subscribed_app_verified = our_app_ok if graph_ok else None

    if health == WEBHOOK_HEALTH_HEALTHY:
        account.webhook_last_success_at = now
        account.webhook_failure_reason = None
        account.webhook_failure_count = 0
    else:
        account.webhook_last_failure_at = now
        reason_text = "; ".join(reasons) if reasons else health
        account.webhook_failure_reason = reason_text[:4000]
        account.webhook_failure_count = min(999_999, int(account.webhook_failure_count or 0) + 1)

    integrity_meta = {
        "checked_at": now.isoformat(),
        "subscribed_apps_count": len(apps_body.get("data") or []),
        "our_app_subscribed": our_app_ok,
        "callback_probe_ok": callback_ok,
        "callback_probe_error": callback_detail,
        "callback_latency_ms": callback_ms,
        "log_counts_24h": log_counts,
        "health": health,
    }
    account.webhook_echo_validation = merge_echo_validation(
        account.webhook_echo_validation,
        {},
        integrity_meta,
    )

    try:
        db.session.add(account)
        db.session.commit()
    except Exception as e:
        logger.exception("validate_and_persist_account commit failed: %s", e)
        db.session.rollback()
        raise

    logger.info(
        "webhook_integrity account_id=%s waba=%s health=%s prev_health=%s our_app=%s callback_ok=%s reasons=%s",
        account.id,
        account.waba_id,
        health,
        prev_health,
        our_app_ok,
        callback_ok,
        reasons,
    )
    return {
        "account_id": account.id,
        "waba_id": account.waba_id,
        "webhook_health": health,
        "our_app_subscribed": our_app_ok,
        "subscribed_apps": apps_body.get("data"),
        "callback_probe": {"ok": callback_ok, "error": callback_detail, "latency_ms": callback_ms},
        "log_counts_24h": log_counts,
        "reasons": reasons,
    }


def refresh_subscribed_apps_metadata(account: WhatsAppAccount) -> Dict[str, Any]:
    """GET subscribed_apps only; lighter than full validate."""
    token = account.get_access_token()
    if not token:
        return {"success": False, "error": "no_token"}
    ok, body = fetch_subscribed_apps(account.waba_id, token)
    graph_ok = ok and "error" not in body
    apps = body.get("data") if isinstance(body.get("data"), list) else []
    our_ok, apps_list = our_app_is_subscribed(body if graph_ok else {})
    account.webhook_last_checked_at = datetime.now(timezone.utc)
    if graph_ok and apps and our_ok:
        account.webhook_subscription_status = "subscribed"
    elif graph_ok and apps and not our_ok:
        account.webhook_subscription_status = "not_subscribed"
    elif graph_ok and not apps:
        account.webhook_subscription_status = "not_subscribed"
    else:
        account.webhook_subscription_status = "error"
    account.webhook_subscribed_app_verified = our_ok if graph_ok else None
    if not graph_ok:
        account.webhook_last_error = str((body.get("error") or {}).get("message", body))[:2000]
    db.session.commit()
    return {"success": graph_ok, "our_app_subscribed": our_ok, "apps": apps_list}


def retry_subscribe_webhooks(account: WhatsAppAccount) -> Dict[str, Any]:
    """POST subscribed_apps with full field set (same as utils.subscribe_waba_to_app)."""
    token = account.get_access_token()
    if not token:
        return {"success": False, "error": "no_token"}
    result = subscribe_waba_to_app(account.waba_id, token)
    validate_and_persist_account(account, probe_callback=True)
    return {"subscribe_result": result, "integrity": {"webhook_health": account.webhook_health}}


def run_webhook_integrity_sweep(
    *,
    limit: int = 100,
    only_active: bool = True,
    probe_callback: bool = True,
) -> Dict[str, Any]:
    """
    Batch job: validate a slice of accounts (Cloud Scheduler / internal).
    """
    q = WhatsAppAccount.query
    if only_active:
        q = q.filter_by(is_active=True)
    q = q.order_by(WhatsAppAccount.id.asc()).limit(max(1, min(limit, 500)))
    accounts = q.all()
    stats = {"examined": 0, "healthy": 0, "degraded": 0, "missing_subscription": 0, "inactive": 0, "failing": 0, "errors": 0}
    for acct in accounts:
        try:
            if not acct.get_access_token():
                continue
            stats["examined"] += 1
            validate_and_persist_account(acct, probe_callback=probe_callback)
            h = acct.webhook_health
            if h == WEBHOOK_HEALTH_HEALTHY:
                stats["healthy"] += 1
            elif h == WEBHOOK_HEALTH_DEGRADED:
                stats["degraded"] += 1
            elif h == WEBHOOK_HEALTH_MISSING_SUBSCRIPTION:
                stats["missing_subscription"] += 1
            elif h == WEBHOOK_HEALTH_INACTIVE:
                stats["inactive"] += 1
            elif h == WEBHOOK_HEALTH_FAILING:
                stats["failing"] += 1
        except Exception:
            stats["errors"] += 1
            logger.exception("webhook integrity sweep failed for account_id=%s", acct.id)
            try:
                db.session.rollback()
            except Exception:
                pass
    logger.info("webhook_integrity_sweep stats=%s", stats)
    return stats
