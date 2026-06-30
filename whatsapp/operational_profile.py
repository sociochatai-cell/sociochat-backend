"""
Derive connection authority mode, limited features, and remediation roadmap
from provisioning / health-check payloads (mirrors frontend operationalProfile.ts).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _send_check_details(checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    for c in checks or []:
        name = c.get("name") or ""
        if name in ("messaging_send_permission", "send_permission"):
            return c.get("details") or {}
    return {}


def _can_send(capability_matrix: Dict[str, Any], checks: List[Dict[str, Any]]) -> bool:
    cap = (capability_matrix or {}).get("can_send_messages") or {}
    if isinstance(cap, dict) and cap.get("enabled") is True:
        return True
    for c in checks or []:
        if c.get("name") in ("messaging_send_permission", "send_permission"):
            return c.get("status") == "healthy"
    return False


def build_operational_profile(
    *,
    capability_matrix: Optional[Dict[str, Any]] = None,
    checks: Optional[List[Dict[str, Any]]] = None,
    token_debug: Optional[Dict[str, Any]] = None,
    warmup_state: Optional[Dict[str, Any]] = None,
    connection_status: str = "CONNECTED",
) -> Dict[str, Any]:
    checks = checks or []
    capability_matrix = capability_matrix or {}
    token_debug = token_debug or {}
    warmup_state = warmup_state or {}

    details = _send_check_details(checks)
    token_type = str(details.get("token_type") or token_debug.get("type") or "").upper()
    waba_ownership = str(details.get("waba_ownership_type") or "").upper()
    hints = details.get("hints") if isinstance(details.get("hints"), list) else []

    can_send = _can_send(capability_matrix, checks)
    is_client_owned = waba_ownership == "CLIENT_OWNED"
    is_user_token = token_type == "USER"

    if is_client_owned and is_user_token:
        mode = "CLIENT_DIRECT"
    elif is_client_owned:
        mode = "CLIENT_SHARED"
    elif is_user_token and not can_send:
        mode = "CLIENT_DIRECT"
    elif not can_send and connection_status == "CONNECTED":
        mode = "CLIENT_SHARED"
    else:
        mode = "APP_OWNED"

    mode_labels = {
        "APP_OWNED": "App-owned (Embedded Signup)",
        "CLIENT_SHARED": "Client-shared (Partner WABA)",
        "CLIENT_DIRECT": "Client-linked (Facebook Login only)",
    }

    working = [
        {"id": "inbox", "label": "Incoming messages & inbox"},
        {"id": "metadata", "label": "Account metadata & health checks"},
    ]
    limited: List[Dict[str, str]] = []

    if not can_send:
        limited.append(
            {
                "id": "outbound",
                "label": "Outbound messages",
                "description": "Manual replies, bulk sends, and automations require Meta messaging authority.",
            }
        )
        working.append({"id": "webhooks_in", "label": "Webhooks for inbound events (when subscribed)"})
    else:
        working.append({"id": "outbound", "label": "Outbound messaging"})

    wh_cap = capability_matrix.get("webhook_subscribed") or {}
    if isinstance(wh_cap, dict) and wh_cap.get("enabled") is False:
        limited.append(
            {
                "id": "webhook_mgmt",
                "label": "Webhook auto-subscription",
                "description": "Subscription may fail until token has WhatsApp management rights.",
            }
        )

    if warmup_state.get("in_warmup_window"):
        cap = warmup_state.get("effective_daily_send_cap")
        limited.append(
            {
                "id": "warmup",
                "label": "Broadcast & aggressive automation",
                "description": f"Warmup active — reduced daily cap ({cap or 'limited'}).",
            }
        )

    roadmap: List[Dict[str, Any]] = []
    if not can_send and mode in ("CLIENT_DIRECT", "CLIENT_SHARED"):
        roadmap = [
            {
                "id": "partner",
                "title": "Confirm partner access in client Business Manager",
                "description": "Grant Sociovia partner permissions on the WhatsApp account.",
            },
            {
                "id": "system_user",
                "title": "Create System User token in Sociovia Business Manager",
                "description": "Assign client WABA; scopes: whatsapp_business_messaging & whatsapp_business_management.",
            },
            {
                "id": "manual_reconnect",
                "title": "Reconnect via Manual Connection in Sociovia",
                "description": "Paste WABA ID, Phone Number ID, and System User permanent token.",
            },
        ]

    return {
        "mode": mode,
        "mode_label": mode_labels.get(mode, mode),
        "can_send_messages": can_send,
        "can_receive_messages": connection_status == "CONNECTED",
        "token_type": token_type or None,
        "waba_ownership": waba_ownership or None,
        "working_features": working,
        "limited_features": limited,
        "roadmap": roadmap,
        "hints": hints,
    }
