"""
Trust / reputation timeline HTTP API (advisory read + operator capture).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from shared_models import db

from .models import MetaAppHealthSnapshot, TrustSnapshot, WhatsAppAccount, WhatsAppPhoneReputationSnapshot
from .trust_snapshot_engine import capture_trust_snapshot_for_account, trust_snapshots_enabled

logger = logging.getLogger(__name__)

trust_snapshot_bp = Blueprint("whatsapp_trust_snapshots", __name__)


def _operator_secret_ok() -> bool:
    secret = (os.getenv("WH_TRUST_OPERATOR_SECRET") or os.getenv("WHATSAPP_CAPABILITIES_UPSERT_SECRET") or "").strip()
    if not secret:
        return False
    auth = request.headers.get("Authorization", "")
    token = auth.split()[-1] if auth.lower().startswith("bearer ") else auth.strip()
    return token == secret


def _account_for_workspace(account_id: int, workspace_id: str) -> Optional[WhatsAppAccount]:
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    acc = WhatsAppAccount.query.get(account_id)
    if not acc or str(acc.workspace_id or "") != wid:
        return None
    return acc


def _limit_param(default: int = 60, cap: int = 500) -> int:
    try:
        n = int(request.args.get("limit") or default)
    except (TypeError, ValueError):
        n = default
    return max(1, min(cap, n))


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/history", methods=["GET"])
def trust_history(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    lim = _limit_param(90, 500)
    rows = (
        TrustSnapshot.query.filter_by(account_id=account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .limit(lim)
        .all()
    )
    return jsonify({"success": True, "snapshots": [r.to_dict() for r in reversed(rows)]})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/reputation-history", methods=["GET"])
def reputation_history(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    lim = _limit_param(90, 500)
    rows = (
        WhatsAppPhoneReputationSnapshot.query.filter_by(account_id=account_id)
        .order_by(WhatsAppPhoneReputationSnapshot.captured_at.desc())
        .limit(lim)
        .all()
    )
    return jsonify({"success": True, "reputation": [r.to_dict() for r in reversed(rows)]})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/webhook-history", methods=["GET"])
def webhook_health_history(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    lim = _limit_param(90, 500)
    rows = (
        TrustSnapshot.query.filter_by(account_id=account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .limit(lim)
        .all()
    )
    out = [
        {
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
            "webhook_health": r.webhook_health,
            "webhook_subscription_status": r.webhook_subscription_status,
            "inputs_webhook_failures": (r.inputs or {}).get("webhook_failure_count") if isinstance(r.inputs, dict) else None,
        }
        for r in reversed(rows)
    ]
    return jsonify({"success": True, "webhook_history": out})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/restriction-timeline", methods=["GET"])
def restriction_timeline(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    lim = _limit_param(120, 500)
    rows = (
        TrustSnapshot.query.filter_by(account_id=account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .limit(lim)
        .all()
    )
    points: List[Dict[str, Any]] = []
    prev_rs = None
    for r in reversed(rows):
        rs = r.restriction_state or "none"
        if rs != prev_rs:
            points.append(
                {
                    "captured_at": r.captured_at.isoformat() if r.captured_at else None,
                    "restriction_state": rs,
                    "changed": True,
                }
            )
            prev_rs = rs
        else:
            points.append(
                {
                    "captured_at": r.captured_at.isoformat() if r.captured_at else None,
                    "restriction_state": rs,
                    "changed": False,
                }
            )
    return jsonify({"success": True, "timeline": points})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/diagnostics", methods=["GET"])
def trust_diagnostics(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    last = (
        TrustSnapshot.query.filter_by(account_id=account_id).order_by(TrustSnapshot.captured_at.desc()).first()
    )
    prev = (
        TrustSnapshot.query.filter_by(account_id=account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .offset(1)
        .first()
    )
    diag: Dict[str, Any] = {
        "account": acc.to_dict(),
        "latest_snapshot": last.to_dict() if last else None,
        "previous_snapshot": prev.to_dict() if prev else None,
        "snapshots_enabled": trust_snapshots_enabled(),
    }
    return jsonify({"success": True, "diagnostics": diag})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/summary", methods=["GET"])
def trust_summary(account_id: int):
    wid = str(request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    lim = _limit_param(30, 200)
    rows = (
        TrustSnapshot.query.filter_by(account_id=account_id)
        .order_by(TrustSnapshot.captured_at.desc())
        .limit(lim)
        .all()
    )
    if not rows:
        return jsonify(
            {
                "success": True,
                "summary": {
                    "snapshot_count": 0,
                    "latest": None,
                    "quality_trend": "unknown",
                    "webhook_trend": "unknown",
                    "recommendations": [
                        "No trust snapshots yet. Run the daily trust job or wait for the next scheduled capture.",
                    ],
                },
            }
        )

    latest = rows[0]
    qualities = [r.quality_rating for r in rows if r.quality_rating]
    whs = [r.webhook_health for r in rows if r.webhook_health]

    def _trend(vals: List[Optional[str]], good: set) -> str:
        if len(vals) < 2:
            return "insufficient_data"
        good_u = {g.upper() for g in good}
        cut = max(1, len(vals) // 3)
        recent = vals[:cut]
        older = vals[cut:]
        if not older:
            return "stable"

        def _score(chunk: List[Optional[str]]) -> float:
            if not chunk:
                return 0.0
            return sum(1 for v in chunk if (v or "").strip().upper() in good_u) / len(chunk)

        r_s, o_s = _score(recent), _score(older)
        if r_s < o_s - 0.01:
            return "worsening"
        if r_s > o_s + 0.01:
            return "improving"
        return "stable"

    q_trend = _trend(qualities, {"GREEN"})
    w_trend = _trend(whs, {"HEALTHY", "OK", "UNKNOWN"})

    recs: List[str] = []
    if q_trend == "worsening":
        recs.append("Quality rating has softened recently — reduce broadcast volume and review template categories.")
    if w_trend == "worsening":
        recs.append("Webhook reliability dipped — re-run webhook health checks and verify subscription to your app.")
    if (latest.restriction_state or "none").lower() not in ("none", ""):
        recs.append("Meta restriction signals present — favor manual replies and approved templates until cleared.")
    if not recs:
        recs.append("Signals look stable. Keep gradual sending and monitor this Trust Center weekly.")

    return jsonify(
        {
            "success": True,
            "summary": {
                "snapshot_count": len(rows),
                "latest": latest.to_dict(),
                "quality_trend": q_trend,
                "webhook_trend": w_trend,
                "recommendations": recs,
            },
        }
    )


@trust_snapshot_bp.route("/platform/meta-app-health/history", methods=["GET"])
def meta_app_health_history():
    lim = _limit_param(30, 200)
    rows = MetaAppHealthSnapshot.query.order_by(MetaAppHealthSnapshot.captured_at.desc()).limit(lim).all()
    return jsonify({"success": True, "snapshots": [r.to_dict() for r in reversed(rows)]})


@trust_snapshot_bp.route("/accounts/<int:account_id>/trust/capture-now", methods=["POST"])
def capture_now(account_id: int):
    """Operator: force one snapshot (same-day allowed)."""
    if not _operator_secret_ok():
        return jsonify({"success": False, "error": "Unauthorized"}), 401
    wid = str((request.get_json(silent=True) or {}).get("workspace_id") or request.args.get("workspace_id") or "").strip()
    acc = _account_for_workspace(account_id, wid)
    if not acc:
        return jsonify({"success": False, "error": "account_not_found"}), 404
    if not trust_snapshots_enabled():
        return jsonify({"success": False, "error": "trust_snapshots_disabled"}), 503
    try:
        snap, st = capture_trust_snapshot_for_account(acc, session=db.session, force=True, skip_if_same_day=False)
        db.session.commit()
    except Exception as e:
        logger.exception("capture_now: %s", e)
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "status": st, "snapshot": snap.to_dict() if snap else None})
