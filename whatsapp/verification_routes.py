"""
Verification, Operational Timeline, and Support Diagnostics HTTP API.
Provides aggregated visibility into Meta lifecycle, webhooks, and trust.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from shared_models import db
from .models import (
    WhatsAppAccount,
    TrustSnapshot,
    WhatsAppWebhookLog,
    WhatsAppPhoneReputationSnapshot,
    WhatsAppOperationalLog,
)
from .webhook_health import (
    retry_subscribe_webhooks,
    refresh_subscribed_apps_metadata,
)
from .warmup_engine import evaluate_warmup

logger = logging.getLogger(__name__)

verification_bp = Blueprint("whatsapp_verification", __name__)


def _operator_secret_ok() -> bool:
    secret = (os.getenv("WH_TRUST_OPERATOR_SECRET") or os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
    if not secret:
        return False

    candidates: list[str] = []

    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        candidates.append(auth[7:].strip())
    elif auth:
        candidates.append(auth)

    for header in ("X-Operator-Secret", "X-WhatsApp-Operator-Secret"):
        val = (request.headers.get(header) or "").strip()
        if val:
            candidates.append(val)

    body = request.get_json(silent=True) or {}
    body_secret = (body.get("operator_secret") or body.get("operatorSecret") or "").strip()
    if body_secret:
        candidates.append(body_secret)

    return any(token == secret for token in candidates if token)


def _workspace_account_authorized(account_id: int, workspace_id: str) -> Optional[WhatsAppAccount]:
    """Allow dashboard actions when workspace_id matches the account (no operator secret)."""
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    return _account_for_workspace(account_id, wid)


def _operator_or_workspace_ok(account_id: int, workspace_id: str) -> tuple[bool, Optional[WhatsAppAccount], str]:
    acc = WhatsAppAccount.query.get(account_id)
    if not acc:
        return False, None, "account_not_found"

    if _operator_secret_ok():
        return True, acc, "operator"

    acc_ws = _workspace_account_authorized(account_id, workspace_id)
    if acc_ws:
        return True, acc_ws, "workspace"

    if not (os.getenv("WH_TRUST_OPERATOR_SECRET") or os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET")):
        return False, acc, "operator_secret_not_configured"

    return False, acc, "unauthorized"


def _account_for_workspace(account_id: int, workspace_id: str) -> Optional[WhatsAppAccount]:
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    acc = WhatsAppAccount.query.get(account_id)
    if not acc or str(acc.workspace_id or "") != wid:
        return None
    return acc


def _resolve_display_name_status(acc: WhatsAppAccount) -> Optional[str]:
    """
    Meta Graph `name_status` is stored on trust / reputation snapshots, not on whatsapp_accounts.
    Fallback: latest trust_snapshots, then whatsapp_phone_reputation_snapshots.
    """
    snap = (
        TrustSnapshot.query.filter_by(account_id=acc.id)
        .order_by(TrustSnapshot.captured_at.desc())
        .first()
    )
    if snap and snap.name_status:
        return snap.name_status
    pr = (
        WhatsAppPhoneReputationSnapshot.query.filter_by(account_id=acc.id)
        .order_by(WhatsAppPhoneReputationSnapshot.captured_at.desc())
        .first()
    )
    if pr and pr.name_status:
        return pr.name_status
    return None


def _derive_next_actions(acc: WhatsAppAccount, health_eval: Dict[str, Any]) -> List[Dict[str, Any]]:
    actions: List[Dict[str, Any]] = []
    
    # 1. Verification & Display Name
    if not acc.verified_name:
        actions.append({"type": "display_name", "message": "Verify Display Name in Meta Business Manager", "severity": "warning"})
    
    # 2. Webhook Health
    wh_health = acc.webhook_health or "unknown"
    if wh_health in ("missing_subscription", "failing", "degraded"):
        actions.append({"type": "webhook_unhealthy", "message": f"Webhook state is {wh_health}. Click Retry Subscription.", "severity": "error"})
    
    # 3. Connection State
    if not acc.get_access_token():
        actions.append({"type": "reconnect_meta", "message": "Access token missing. Please reconnect Meta account.", "severity": "critical"})
        
    # 4. Warmup / Safe Mode
    if health_eval.get("in_warmup_window"):
        actions.append({"type": "warmup_active", "message": "Account is in Warmup. Follow daily send limits.", "severity": "info"})
    
    # 5. Restriction State
    if health_eval.get("blocks"):
        actions.append({"type": "restricted", "message": "Meta restrictions applied. Check Meta Business Support.", "severity": "critical"})
        
    return actions


@verification_bp.route("/accounts/<int:account_id>/verification-status", methods=["GET"])
def get_verification_status(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404

    health_eval = evaluate_warmup(acc)
    
    score = 0
    if acc.get_access_token(): score += 20
    if acc.verified_name: score += 20
    if acc.webhook_health == "healthy": score += 20
    if acc.quality_score in ("GREEN", "YELLOW"): score += 20
    if acc.phone_number_id and acc.waba_id: score += 20

    actions = _derive_next_actions(acc, {
        "in_warmup_window": health_eval.in_warmup_window,
        "blocks": health_eval.blocks
    })

    maturity = "onboarding"
    if acc.get_access_token() and acc.webhook_health == "healthy":
        if health_eval.blocks:
            maturity = "restricted"
        elif health_eval.in_warmup_window:
            maturity = "warming_up"
        else:
            maturity = "trusted" if acc.quality_score == "GREEN" else "mature"

    return jsonify({
        "success": True,
        "status": {
            "completeness_score": score,
            "display_name_status": _resolve_display_name_status(acc),
            "verified_name": acc.verified_name,
            "maturity_state": maturity,
            "quality_score": acc.quality_score,
            "messaging_limit": acc.messaging_limit,
            "operational_mode": acc.operational_mode,
            "safe_mode_reason": acc.safe_mode_reason,
            "actions_required": actions
        }
    })


@verification_bp.route("/accounts/<int:account_id>/webhook/retry", methods=["POST"])
def webhook_retry(account_id: int):
    wid = str((request.get_json(silent=True) or {}).get("workspace_id") or request.args.get("workspace_id") or "").strip()
    ok, acc, reason = _operator_or_workspace_ok(account_id, wid)
    if not ok or not acc:
        if reason == "account_not_found":
            return jsonify({"success": False, "error": "account_not_found"}), 404
        if reason == "operator_secret_not_configured":
            return jsonify({
                "success": False,
                "error": "Unauthorized",
                "hint": "Set WH_TRUST_OPERATOR_SECRET on whatsapp-api, or pass workspace_id for your own account.",
            }), 401
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    result = retry_subscribe_webhooks(acc)
    return jsonify({"success": True, "result": result, "auth": reason})


@verification_bp.route("/accounts/<int:account_id>/webhook/refresh", methods=["POST"])
def webhook_refresh(account_id: int):
    wid = str((request.get_json(silent=True) or {}).get("workspace_id") or request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404

    result = refresh_subscribed_apps_metadata(acc)
    return jsonify({"success": True, "result": result})


@verification_bp.route("/accounts/<int:account_id>/safe-mode-override", methods=["POST"])
def safe_mode_override(account_id: int):
    if not _operator_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
        
    wid = str((request.get_json(silent=True) or {}).get("workspace_id") or request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
        
    data = request.get_json(silent=True) or {}
    advisory_only = data.get("advisory_only")
    ttl_hours = data.get("ttl_hours")
    
    if advisory_only is None:
        return jsonify({"success": False, "error": "advisory_only parameter required"}), 400
        
    old_state = acc.safe_mode_advisory_only
    acc.safe_mode_advisory_only = bool(advisory_only)
    
    expires_at = None
    if not acc.safe_mode_advisory_only and ttl_hours:
        try:
            hours = float(ttl_hours)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
            acc.safe_mode_override_expires_at = expires_at
        except ValueError:
            pass
    elif acc.safe_mode_advisory_only:
        acc.safe_mode_override_expires_at = None
        
    try:
        from .safe_mode_engine import log_operational_adaptation
        details = f"Operator changed safe_mode_advisory_only from {old_state} to {acc.safe_mode_advisory_only}"
        if expires_at:
            details += f". Expires at {expires_at.isoformat()}"
            
        log_operational_adaptation(
            account_id=acc.id,
            event_type="override_toggled",
            details=details,
            severity="warning" if not acc.safe_mode_advisory_only else "info"
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
        
    return jsonify({
        "success": True, 
        "safe_mode_advisory_only": acc.safe_mode_advisory_only,
        "safe_mode_override_expires_at": acc.safe_mode_override_expires_at.isoformat() if acc.safe_mode_override_expires_at else None
    })

@verification_bp.route("/accounts/<int:account_id>/operational-metrics", methods=["GET"])
def get_operational_metrics(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
        
    metrics = acc.operational_metrics or {}
    return jsonify({
        "success": True,
        "metrics": {
            "degradation_count": metrics.get("degradation_count", 0),
            "recovery_count": metrics.get("recovery_count", 0),
            "safe_mode_entries_count": metrics.get("safe_mode_entries_count", 0),
            "webhook_failure_count": acc.webhook_failure_count,
            "webhook_cooldown_ends_at": acc.webhook_cooldown_ends_at.isoformat() if acc.webhook_cooldown_ends_at else None
        }
    })


@verification_bp.route("/accounts/<int:account_id>/operational-timeline", methods=["GET"])
def get_operational_timeline(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404

    events = []
    
    # 1. Creation event
    if acc.created_at:
        events.append({"timestamp": acc.created_at.isoformat(), "type": "onboarding", "description": "Account registered in system"})

    # 2. Connection event
    if acc.last_inbound_webhook_at:
        events.append({"timestamp": acc.last_inbound_webhook_at.isoformat(), "type": "webhook", "description": "Last inbound webhook received"})

    # 3. Trust Snapshots (Quality, Restrictions, Webhook Health)
    snapshots = TrustSnapshot.query.filter_by(account_id=account_id).order_by(TrustSnapshot.captured_at.desc()).limit(20).all()
    for snap in snapshots:
        if snap.captured_at:
            events.append({
                "timestamp": snap.captured_at.isoformat(),
                "type": "trust_snapshot",
                "description": f"Trust Snapshot Captured",
                "details": {
                    "quality": snap.quality_rating,
                    "webhook_health": snap.webhook_health,
                    "restriction_state": snap.restriction_state
                }
            })

    # 4. Safe Mode and Operational Adaptations
    op_logs = WhatsAppOperationalLog.query.filter_by(account_id=account_id).order_by(WhatsAppOperationalLog.created_at.desc()).limit(20).all()
    for log in op_logs:
        if log.created_at:
            events.append({
                "timestamp": log.created_at.isoformat(),
                "type": "safe_mode",
                "description": log.reason or "Operational adaptation",
                "details": {
                    "event_type": log.event_type,
                    "previous_mode": log.previous_mode,
                    "new_mode": log.new_mode,
                    "actor": log.actor,
                }
            })

    events.sort(key=lambda x: x["timestamp"], reverse=True)
    return jsonify({"success": True, "timeline": events})


@verification_bp.route("/accounts/<int:account_id>/diagnostics/export", methods=["GET"])
def export_diagnostics(account_id: int):
    if not _operator_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404

    health_eval = evaluate_warmup(acc)
    
    # Get latest snapshot
    snap = TrustSnapshot.query.filter_by(account_id=account_id).order_by(TrustSnapshot.captured_at.desc()).first()

    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {
            "id": acc.id,
            "waba_id": acc.waba_id,
            "phone_number_id": acc.phone_number_id,
            "quality_score": acc.quality_score,
            "name_status": _resolve_display_name_status(acc),
            "verified_name": acc.verified_name,
            "is_active": acc.is_active,
            "webhook_health": acc.webhook_health,
        },
        "warmup_state": {
            "in_warmup_window": health_eval.in_warmup_window,
            "blocks": health_eval.blocks,
            "diagnostics": health_eval.diagnostics
        },
        "latest_trust_snapshot": snap.to_dict() if snap else None,
        "webhook_echo_validation": acc.webhook_echo_validation
    }
    
    return jsonify({"success": True, "diagnostics": data})
