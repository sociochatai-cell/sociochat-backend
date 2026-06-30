"""
Customer webhook routing for multi-tenant WhatsApp inbound events.

Meta -> POST /api/whatsapp/webhook (routes.webhook_receive)
     -> process_inbound_webhook()  [single DB pass]
     -> optional forward to account.customer_webhook_url
     -> internal WebhookProcessor on filtered payload
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from .models import WhatsAppAccount

logger = logging.getLogger(__name__)

FORWARD_TIMEOUT_SECONDS = float(os.getenv("WHATSAPP_CUSTOMER_WEBHOOK_TIMEOUT", "8"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def is_customer_forwarding_enabled() -> bool:
    return _env_bool("WHATSAPP_CUSTOMER_WEBHOOK_FORWARDING", True)


def _only_digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


@dataclass
class RoutingDecision:
    should_forward: bool
    reason: str
    account: Optional[WhatsAppAccount] = None
    destination_url: Optional[str] = None
    workspace_id: Optional[str] = None
    waba_id: Optional[str] = None
    user_id: Optional[str] = None


@dataclass
class InboundWebhookResult:
    filtered_payload: Optional[Dict[str, Any]]
    forward_decision: RoutingDecision


def _cache_key(waba_id: str, phone_number_id: str) -> Tuple[str, str]:
    return (str(waba_id or "").strip(), str(phone_number_id or "").strip())


def resolve_webhook_account(
    waba_id: str,
    phone_number_id: str,
    cache: Optional[Dict[Tuple[str, str], Optional[WhatsAppAccount]]] = None,
) -> Optional[WhatsAppAccount]:
    key = _cache_key(waba_id, phone_number_id)
    if cache is not None and key in cache:
        return cache[key]

    waba_id, phone_number_id = key
    account: Optional[WhatsAppAccount] = None

    if phone_number_id:
        account = WhatsAppAccount.query.filter_by(
            phone_number_id=phone_number_id,
            is_active=True,
        ).first()

    if not account and waba_id:
        account = (
            WhatsAppAccount.query.filter_by(waba_id=waba_id, is_active=True)
            .order_by(WhatsAppAccount.id.desc())
            .first()
        )

    if cache is not None:
        cache[key] = account
    return account


def change_matches_account(change: Dict[str, Any], account: WhatsAppAccount) -> bool:
    value = change.get("value", {}) if isinstance(change, dict) else {}
    metadata = value.get("metadata", {}) if isinstance(value, dict) else {}

    incoming_phone = str(metadata.get("phone_number_id") or "").strip()
    if incoming_phone and incoming_phone != str(account.phone_number_id or "").strip():
        return False

    incoming_display = _only_digits(metadata.get("display_phone_number"))
    stored_display = _only_digits(account.display_phone_number)
    if incoming_display and stored_display and incoming_display != stored_display:
        return False

    return True


def evaluate_forward_match(account: Optional[WhatsAppAccount], waba_id: str) -> RoutingDecision:
    waba_id = str(waba_id or "").strip()

    if not account:
        return RoutingDecision(False, "account_not_found")
    if not account.is_active:
        return RoutingDecision(False, "account_inactive", account=account)

    account_waba = str(account.waba_id or "").strip()
    if not waba_id or not account_waba or waba_id != account_waba:
        return RoutingDecision(
            False,
            "waba_id_mismatch",
            account=account,
            waba_id=waba_id,
            workspace_id=account.workspace_id,
            user_id=account.connected_by_user_id,
        )

    workspace_id = str(account.workspace_id or "").strip()
    if not workspace_id:
        return RoutingDecision(False, "workspace_id_missing", account=account, waba_id=waba_id)

    user_id = str(account.connected_by_user_id or "").strip()
    if _env_bool("WHATSAPP_WEBHOOK_REQUIRE_USER_ID", False) and not user_id:
        return RoutingDecision(
            False,
            "user_id_missing",
            account=account,
            waba_id=waba_id,
            workspace_id=workspace_id,
        )

    destination = str(getattr(account, "customer_webhook_url", None) or "").strip()
    if not destination:
        return RoutingDecision(
            False,
            "customer_webhook_url_not_configured",
            account=account,
            waba_id=waba_id,
            workspace_id=workspace_id,
            user_id=user_id or None,
        )

    return RoutingDecision(
        True,
        "matched",
        account=account,
        destination_url=destination,
        waba_id=waba_id,
        workspace_id=workspace_id,
        user_id=user_id or None,
    )


def process_inbound_webhook(
    payload: Dict[str, Any],
    *,
    strict_allowlist: Optional[bool] = None,
) -> InboundWebhookResult:
    no_match = RoutingDecision(False, "invalid_payload")
    if not isinstance(payload, dict):
        return InboundWebhookResult(None, no_match)

    entries = payload.get("entry", [])
    if not isinstance(entries, list) or not entries:
        return InboundWebhookResult(None, RoutingDecision(False, "empty_entries"))

    if strict_allowlist is None:
        strict_allowlist = _env_bool("WHATSAPP_WEBHOOK_STRICT_ALLOWLIST", False)

    expected_waba = str(
        os.getenv("WHATSAPP_ALLOWED_WABA_ID") or os.getenv("WHATSAPP_WABA_ID") or ""
    ).strip()
    expected_phone = str(
        os.getenv("WHATSAPP_ALLOWED_PHONE_NUMBER_ID")
        or os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        or ""
    ).strip()

    account_cache: Dict[Tuple[str, str], Optional[WhatsAppAccount]] = {}
    filtered_entries: List[Dict[str, Any]] = []
    forward_account: Optional[WhatsAppAccount] = None
    forward_waba: str = ""
    forward_destinations: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        entry_waba = str(entry.get("id") or "").strip()
        if strict_allowlist and expected_waba and entry_waba != expected_waba:
            continue

        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            continue

        allowed_changes: List[Dict[str, Any]] = []
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value", {}) or {}
            metadata = value.get("metadata", {}) if isinstance(value, dict) else {}
            incoming_phone = str(metadata.get("phone_number_id") or "").strip()

            if strict_allowlist and expected_phone and incoming_phone != expected_phone:
                continue

            account = resolve_webhook_account(entry_waba, incoming_phone, account_cache)
            if not account or not change_matches_account(change, account):
                continue

            allowed_changes.append(change)

            dest = str(getattr(account, "customer_webhook_url", None) or "").strip()
            if dest:
                forward_destinations.add(dest)
            if forward_account is None:
                forward_account = account
                forward_waba = entry_waba or str(account.waba_id or "").strip()

        if allowed_changes:
            row = dict(entry)
            row["changes"] = allowed_changes
            filtered_entries.append(row)

    filtered_payload: Optional[Dict[str, Any]] = None
    if filtered_entries:
        filtered_payload = dict(payload)
        filtered_payload["entry"] = filtered_entries

    if not is_customer_forwarding_enabled():
        decision = RoutingDecision(False, "forwarding_disabled")
    elif len(forward_destinations) > 1:
        decision = RoutingDecision(
            False,
            "multi_tenant_payload",
            account=forward_account,
            waba_id=forward_waba,
            workspace_id=getattr(forward_account, "workspace_id", None),
        )
    elif forward_account:
        effective_waba = forward_waba or str(forward_account.waba_id or "").strip()
        decision = evaluate_forward_match(forward_account, effective_waba)
    else:
        decision = RoutingDecision(False, "no_matched_account")

    return InboundWebhookResult(filtered_payload, decision)


def build_customer_forward_headers(
    destination_url: str,
    *,
    meta_signature: Optional[str] = None,
    account: Optional[WhatsAppAccount] = None,
) -> Dict[str, str]:
    url = str(destination_url or "").strip()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if meta_signature:
        headers["X-Hub-Signature-256"] = meta_signature
    if "loca.lt" in url:
        headers["Bypass-Tunnel-Reminder"] = "true"
    if account is not None:
        secret_header = str(getattr(account, "customer_webhook_secret_header", None) or "").strip()
        secret_value = str(getattr(account, "customer_webhook_secret_value", None) or "").strip()
        if secret_header and secret_value:
            headers[secret_header] = secret_value
    return headers


def forward_webhook_payload(
    destination_url: str,
    *,
    raw_body: Optional[bytes] = None,
    payload: Optional[Dict[str, Any]] = None,
    meta_signature: Optional[str] = None,
    account: Optional[WhatsAppAccount] = None,
) -> Tuple[bool, str]:
    url = str(destination_url or "").strip()
    if not url:
        return False, "empty_destination_url"

    headers = build_customer_forward_headers(
        url, meta_signature=meta_signature, account=account
    )

    try:
        if raw_body is not None:
            resp = requests.post(
                url, data=raw_body, headers=headers, timeout=FORWARD_TIMEOUT_SECONDS
            )
        elif payload is not None:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=FORWARD_TIMEOUT_SECONDS
            )
        else:
            return False, "no_body"
        if 200 <= resp.status_code < 300:
            return True, f"http_{resp.status_code}"
        return False, f"http_{resp.status_code}: {resp.text[:120]}"
    except requests.RequestException as exc:
        return False, str(exc)[:200]


def _forward_async(
    destination_url: str,
    *,
    raw_body: Optional[bytes],
    payload: Optional[Dict[str, Any]],
    meta_signature: Optional[str],
    decision: RoutingDecision,
) -> None:
    ok, detail = forward_webhook_payload(
        destination_url,
        raw_body=raw_body,
        payload=payload,
        meta_signature=meta_signature,
        account=decision.account,
    )
    if ok:
        logger.info(
            "Customer webhook forwarded: workspace_id=%s account_id=%s detail=%s",
            decision.workspace_id,
            getattr(decision.account, "id", None),
            detail,
        )
    else:
        logger.warning(
            "Customer webhook forward failed: workspace_id=%s account_id=%s detail=%s",
            decision.workspace_id,
            getattr(decision.account, "id", None),
            detail,
        )


def dispatch_customer_webhook(
    payload: Dict[str, Any],
    decision: RoutingDecision,
    *,
    raw_body: Optional[bytes] = None,
    meta_signature: Optional[str] = None,
    blocking: bool = False,
) -> RoutingDecision:
    if not decision.should_forward or not decision.destination_url:
        logger.info(
            "Customer webhook skip: reason=%s waba_id=%s workspace_id=%s account_id=%s",
            decision.reason,
            decision.waba_id,
            decision.workspace_id,
            getattr(decision.account, "id", None),
        )
        return decision

    logger.info(
        "Customer webhook dispatch: workspace_id=%s account_id=%s waba_id=%s url=%s",
        decision.workspace_id,
        getattr(decision.account, "id", None),
        decision.waba_id,
        decision.destination_url,
    )

    if blocking:
        _forward_async(
            decision.destination_url,
            raw_body=raw_body,
            payload=payload,
            meta_signature=meta_signature,
            decision=decision,
        )
        return decision

    threading.Thread(
        target=_forward_async,
        kwargs={
            "destination_url": decision.destination_url,
            "raw_body": raw_body,
            "payload": payload if raw_body is None else None,
            "meta_signature": meta_signature,
            "decision": decision,
        },
        daemon=True,
        name="wa-customer-webhook-forward",
    ).start()
    return decision
