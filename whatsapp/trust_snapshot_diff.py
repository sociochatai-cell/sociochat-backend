"""
Diff consecutive trust snapshots — advisory structured logs only.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_QUALITY_RANK = {"GREEN": 3, "YELLOW": 2, "RED": 1, "UNKNOWN": 0, "": 0, None: 0}


def _q_rank(val: Optional[str]) -> int:
    if not val:
        return 0
    return _QUALITY_RANK.get(str(val).strip().upper(), 0)


def _webhook_worse(prev: Optional[str], cur: Optional[str]) -> bool:
    good = {"healthy", "ok", "unknown", ""}
    p = (prev or "unknown").lower()
    c = (cur or "unknown").lower()
    if p in good and c not in good:
        return True
    return False


def diff_trust_snapshots(
    prev: Optional[Dict[str, Any]],
    cur: Dict[str, Any],
    *,
    account_id: int,
) -> List[str]:
    """
    Compare previous snapshot column dict to current. Returns event type strings emitted to logs.
    prev keys: quality_rating, messaging_tier, webhook_health, restriction_state, inputs (optional)
    """
    events: List[str] = []
    if not prev:
        return events

    pq, cq = prev.get("quality_rating"), cur.get("quality_rating")
    if _q_rank(cq) < _q_rank(pq) and _q_rank(pq) > 0:
        logger.info(
            "quality_degraded account_id=%s from=%s to=%s",
            account_id,
            pq,
            cq,
        )
        events.append("quality_degraded")
    elif _q_rank(cq) > _q_rank(pq) and _q_rank(cq) > 0:
        logger.info(
            "quality_recovered account_id=%s from=%s to=%s",
            account_id,
            pq,
            cq,
        )
        events.append("quality_recovered")
        logger.info("trust_recovery_detected account_id=%s signal=quality", account_id)

    pw, cw = prev.get("webhook_health"), cur.get("webhook_health")
    if _webhook_worse(str(pw) if pw else None, str(cw) if cw else None):
        logger.info(
            "reputation_change_detected account_id=%s field=webhook_health from=%s to=%s",
            account_id,
            pw,
            cw,
        )
        events.append("webhook_degraded")

    pr, cr = (prev.get("restriction_state") or "none"), (cur.get("restriction_state") or "none")
    if pr != cr and str(cr).lower() not in ("none", ""):
        logger.info(
            "restriction_progression account_id=%s from=%s to=%s",
            account_id,
            pr,
            cr,
        )
        events.append("restriction_progression")

    if pr != cr and str(cr).lower() in ("none", "") and str(pr).lower() not in ("none", ""):
        logger.info("trust_recovery_detected account_id=%s signal=restriction_cleared", account_id)
        events.append("trust_recovery_restriction")

    ptier, ctier = prev.get("messaging_tier"), cur.get("messaging_tier")
    if ptier != ctier and (ctier or ptier):
        logger.info(
            "reputation_change_detected account_id=%s field=messaging_tier from=%s to=%s",
            account_id,
            ptier,
            ctier,
        )
        events.append("tier_changed")

    try:
        pi = (prev.get("inputs") or {})
        ci = (cur.get("inputs") or {})
        pfc = int(pi.get("webhook_failure_count") or 0)
        cfc = int(ci.get("webhook_failure_count") or 0)
        if cfc >= pfc + 5 and cfc >= 8:
            logger.info(
                "reputation_change_detected account_id=%s field=webhook_failure_counter from=%s to=%s",
                account_id,
                pfc,
                cfc,
            )
            events.append("webhook_failure_counter_surge")
    except (TypeError, ValueError):
        pass

    # Send spike vs previous inputs.volume.outbound_count_24h
    try:
        pv = (prev.get("inputs") or {}).get("volume") or {}
        cv = (cur.get("inputs") or {}).get("volume") or {}
        po, co = int(pv.get("outbound_count_24h") or 0), int(cv.get("outbound_count_24h") or 0)
        if po >= 10 and co > po * 3:
            logger.info(
                "reputation_change_detected account_id=%s field=outbound_spike prior_24h=%s cur_24h=%s",
                account_id,
                po,
                co,
            )
            events.append("send_spike_suspected")
    except (TypeError, ValueError):
        pass

    return events
