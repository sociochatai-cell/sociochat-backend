"""
Embedded Signup onboarding session orchestration (advisory, recoverable).
=======================================================================

Persists lifecycle state, postMessage-style client events, correlation with
token exchange, resume tokens, and expiration — without removing legacy flows
or blocking sends.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from shared_models import db
from .models import OnboardingEvent, OnboardingSession

logger = logging.getLogger(__name__)

DEFAULT_EXPIRY_HOURS = int(os.getenv("ONBOARDING_SESSION_EXPIRY_HOURS", "72"))

# Lifecycle `status` values (stored in onboarding_sessions.status)
ST_INITIALIZED = "initialized"
ST_POPUP_OPENED = "popup_opened"
ST_META_AUTH_STARTED = "meta_auth_started"
ST_BUSINESS_SELECTED = "business_selected"
ST_WABA_SELECTED = "waba_selected"
ST_PHONE_SELECTED = "phone_selected"
ST_WEBHOOK_SUBSCRIBED = "webhook_subscribed"
ST_TOKEN_EXCHANGED = "token_exchanged"
ST_COMPLETED = "completed"
ST_ABANDONED = "abandoned"
ST_FAILED = "failed"
ST_EXPIRED = "expired"

TERMINAL = {ST_COMPLETED, ST_ABANDONED, ST_FAILED, ST_EXPIRED}

IN_PROGRESS = {
    ST_INITIALIZED,
    ST_POPUP_OPENED,
    ST_META_AUTH_STARTED,
    ST_BUSINESS_SELECTED,
    ST_WABA_SELECTED,
    ST_PHONE_SELECTED,
    ST_WEBHOOK_SUBSCRIBED,
    ST_TOKEN_EXCHANGED,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_resume_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _structured(kind: str, **kwargs: Any) -> None:
    logger.info("onboarding_%s %s", kind, " ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items())))


def create_session(
    *,
    workspace_id: str,
    user_id: str,
    onboarding_path: str = "embedded",
    onboarding_method: Optional[str] = None,
    is_coexistence: bool = False,
    embedded_signup_version: Optional[str] = None,
    graph_version: Optional[str] = None,
    sdk_version: Optional[str] = None,
    config_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    expire_hours: Optional[int] = None,
) -> Tuple[OnboardingSession, str]:
    """
    Create a new session; supersede other in-progress sessions for same workspace+user.
    Returns (session, resume_token_plaintext) — show resume_token once to the client.
    """
    wid = str(workspace_id).strip()
    uid = str(user_id).strip()
    if not wid or not uid:
        raise ValueError("workspace_id and user_id are required")

    hours = expire_hours if expire_hours is not None else DEFAULT_EXPIRY_HOURS
    expires_at = _now() + timedelta(hours=hours)
    corr = (correlation_id or "").strip() or secrets.token_urlsafe(24)
    resume_plain = secrets.token_urlsafe(32)

    # Abandon stale in-progress sessions (avoid orphaned parallel states)
    q = (
        OnboardingSession.query.filter(
            OnboardingSession.workspace_id == wid,
            OnboardingSession.user_id == uid,
            OnboardingSession.status.in_(list(IN_PROGRESS)),
        )
    )
    for old in q.all():
        old.status = ST_ABANDONED
        old.abandoned_at = _now()
        old.last_error = old.last_error or "superseded_by_new_session"
        db.session.add(
            OnboardingEvent(
                session_id=old.id,
                account_id=old.account_id,
                event_type="onboarding_abandoned",
                correlation_id=old.correlation_id,
                payload={"reason": "superseded_by_new_session"},
            )
        )
        _structured("abandoned", session_id=str(old.id), reason="superseded_by_new_session")

    row = OnboardingSession(
        id=uuid_mod.uuid4(),
        workspace_id=wid,
        user_id=uid,
        correlation_id=corr[:64],
        onboarding_path=onboarding_path[:40],
        onboarding_method=(onboarding_method or onboarding_path or "embedded")[:40],
        is_coexistence=bool(is_coexistence),
        embedded_signup_version=(embedded_signup_version[:32] if embedded_signup_version else None),
        graph_version=(graph_version[:16] if graph_version else None),
        sdk_version=(sdk_version[:32] if sdk_version else None),
        config_id=(config_id[:64] if config_id else None),
        status=ST_INITIALIZED,
        last_step=ST_INITIALIZED,
        session_payload={},
        resume_token_hash=_hash_resume_token(resume_plain),
        expires_at=expires_at,
        last_event_at=_now(),
    )
    db.session.add(row)
    db.session.flush()
    db.session.add(
        OnboardingEvent(
            session_id=row.id,
            event_type="onboarding_session_created",
            correlation_id=row.correlation_id,
            payload={"expires_at": expires_at.isoformat()},
        )
    )
    db.session.commit()
    _structured("session_created", session_id=str(row.id), workspace_id=wid, user_id=uid, correlation_id=corr)
    return row, resume_plain


def get_session(session_id: str) -> Optional[OnboardingSession]:
    try:
        sid = uuid_mod.UUID(str(session_id))
    except Exception:
        return None
    return OnboardingSession.query.get(sid)


def verify_resume(session: OnboardingSession, resume_token: Optional[str]) -> bool:
    if not resume_token or not session.resume_token_hash:
        return False
    return secrets.compare_digest(session.resume_token_hash, _hash_resume_token(resume_token.strip()))


def _is_expired(session: OnboardingSession) -> bool:
    if session.expires_at and session.expires_at < _now():
        return True
    return False


def expire_session_if_needed(session: OnboardingSession) -> bool:
    """Mark expired; returns True if session is terminal or expired after call."""
    if session.status in TERMINAL:
        return True
    if _is_expired(session):
        session.status = ST_EXPIRED
        session.last_error = session.last_error or "session_expired"
        session.last_event_at = _now()
        db.session.add(
            OnboardingEvent(
                session_id=session.id,
                event_type="onboarding_expired",
                correlation_id=session.correlation_id,
                payload={},
            )
        )
        _structured("expired", session_id=str(session.id))
        db.session.commit()
        return True
    return False


def transition(
    session: OnboardingSession,
    new_status: str,
    *,
    last_step: Optional[str] = None,
    session_payload_patch: Optional[Dict[str, Any]] = None,
    log_type: str = "onboarding_step_transition",
    extra_payload: Optional[Dict[str, Any]] = None,
) -> None:
    if session.status in TERMINAL and new_status not in TERMINAL:
        raise ValueError("Cannot transition a terminal session")
    expire_session_if_needed(session)
    if session.status == ST_EXPIRED:
        raise ValueError("Session expired")

    prev = session.status
    session.status = new_status[:32]
    session.last_step = (last_step or new_status)[:64]
    session.last_event_at = _now()
    pl = dict(session.session_payload or {})
    if session_payload_patch:
        pl.update(session_payload_patch)
    session.session_payload = pl
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            account_id=session.account_id,
            event_type=log_type,
            correlation_id=session.correlation_id,
            payload={"from": prev, "to": new_status, **(extra_payload or {})},
        )
    )
    _structured("step_transition", session_id=str(session.id), from_status=prev, to=new_status)
    db.session.commit()


def append_embedded_event(
    session: OnboardingSession,
    *,
    event_type: str,
    payload: Optional[Dict[str, Any]] = None,
    map_to_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Client-driven postMessage / SDK event ingestion."""
    if expire_session_if_needed(session):
        return {"success": False, "error": "session_expired", "session": session.to_dict()}
    if session.status in TERMINAL:
        return {"success": False, "error": "session_terminal", "session": session.to_dict()}

    mapped = map_to_status or map_client_event_to_status(event_type)
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            account_id=session.account_id,
            event_type="embedded_signup_event",
            correlation_id=session.correlation_id,
            payload={"client_event": event_type, **(payload or {})},
        )
    )
    _structured("embedded_signup_event", session_id=str(session.id), client_event=event_type)

    if mapped:
        session.status = mapped[:32]
        session.last_step = event_type[:64]
        session.last_event_at = _now()
        db.session.add(
            OnboardingEvent(
                session_id=session.id,
                event_type="onboarding_step_transition",
                correlation_id=session.correlation_id,
                payload={"via": "embedded_event", "event": event_type, "status": mapped},
            )
        )
        _structured("step_transition", session_id=str(session.id), via="embedded_event", to=mapped)

    db.session.commit()
    return {"success": True, "session": session.to_dict()}


def map_client_event_to_status(event_type: str) -> Optional[str]:
    t = (event_type or "").strip().lower()
    mapping = {
        "popup_opened": ST_POPUP_OPENED,
        "fb_login_opened": ST_POPUP_OPENED,
        "meta_auth_started": ST_META_AUTH_STARTED,
        "auth_started": ST_META_AUTH_STARTED,
        "business_selected": ST_BUSINESS_SELECTED,
        "waba_selected": ST_WABA_SELECTED,
        "phone_selected": ST_PHONE_SELECTED,
        "finish": ST_PHONE_SELECTED,
        "finish_whatsapp_business_app_onboarding": ST_PHONE_SELECTED,
        "finish_whatsapp_business_embedded_signup": ST_PHONE_SELECTED,
    }
    return mapping.get(t)


def record_asset_hints(
    session: OnboardingSession,
    *,
    business_manager_id: Optional[str] = None,
    waba_id: Optional[str] = None,
    phone_number_id: Optional[str] = None,
) -> None:
    if business_manager_id:
        session.business_manager_id = "".join(c for c in str(business_manager_id) if c.isdigit())[:64] or None
    if waba_id:
        session.waba_id = str(waba_id).strip()[:64] or None
    if phone_number_id:
        session.phone_number_id = str(phone_number_id).strip()[:64] or None
    session.last_event_at = _now()
    db.session.commit()


def mark_token_exchanged(session: OnboardingSession) -> None:
    transition(session, ST_TOKEN_EXCHANGED, last_step="token_exchanged", log_type="onboarding_step_transition")


def mark_webhook_subscribed(session: OnboardingSession, detail: Optional[Dict[str, Any]] = None) -> None:
    transition(
        session,
        ST_WEBHOOK_SUBSCRIBED,
        last_step="webhook_subscribed",
        session_payload_patch={"webhook_subscribe": detail or {}},
    )


def mark_completed(session: OnboardingSession, account_id: int) -> None:
    session.account_id = account_id
    session.completed_at = _now()
    session.status = ST_COMPLETED
    session.last_step = ST_COMPLETED
    session.last_event_at = _now()
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            account_id=account_id,
            event_type="onboarding_completed",
            correlation_id=session.correlation_id,
            payload={"account_id": account_id},
        )
    )
    _structured("completed", session_id=str(session.id), account_id=account_id)
    db.session.commit()


def mark_failed(session: OnboardingSession, message: str) -> None:
    session.status = ST_FAILED
    session.last_error = (message or "")[:4000]
    session.last_event_at = _now()
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            event_type="onboarding_failed",
            correlation_id=session.correlation_id,
            payload={"error": message},
        )
    )
    _structured("failed", session_id=str(session.id), error=message[:500])
    db.session.commit()


def mark_abandoned(session: OnboardingSession, reason: str = "user_abandoned") -> None:
    session.status = ST_ABANDONED
    session.abandoned_at = _now()
    session.last_error = reason[:2000]
    session.last_event_at = _now()
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            event_type="onboarding_abandoned",
            correlation_id=session.correlation_id,
            payload={"reason": reason},
        )
    )
    _structured("abandoned", session_id=str(session.id), reason=reason)
    db.session.commit()


def resume_session(session: OnboardingSession, resume_token: str) -> Dict[str, Any]:
    if not verify_resume(session, resume_token):
        return {"success": False, "error": "invalid_resume_token"}
    if expire_session_if_needed(session):
        return {"success": False, "error": "session_expired"}
    if session.status in TERMINAL:
        return {"success": False, "error": "session_terminal", "status": session.status}
    session.last_event_at = _now()
    db.session.add(
        OnboardingEvent(
            session_id=session.id,
            event_type="onboarding_resumed",
            correlation_id=session.correlation_id,
            payload={},
        )
    )
    _structured("resumed", session_id=str(session.id))
    db.session.commit()
    return {"success": True, "session": session.to_dict()}


def get_active_session(workspace_id: str, user_id: str) -> Optional[OnboardingSession]:
    wid, uid = str(workspace_id).strip(), str(user_id).strip()
    row = (
        OnboardingSession.query.filter(
            OnboardingSession.workspace_id == wid,
            OnboardingSession.user_id == uid,
            OnboardingSession.status.in_(list(IN_PROGRESS)),
        )
        .order_by(OnboardingSession.created_at.desc())
        .first()
    )
    if row and _is_expired(row):
        expire_session_if_needed(row)
        return None
    return row


def sweep_expired_sessions(limit: int = 200) -> int:
    """Mark in-progress rows past expires_at as expired. Returns count updated."""
    now = _now()
    n = 0
    rows = (
        OnboardingSession.query.filter(
            OnboardingSession.status.in_(list(IN_PROGRESS)),
            OnboardingSession.expires_at.isnot(None),
            OnboardingSession.expires_at < now,
        )
        .limit(limit)
        .all()
    )
    for s in rows:
        s.status = ST_EXPIRED
        s.last_error = s.last_error or "session_expired"
        s.last_event_at = now
        db.session.add(
            OnboardingEvent(
                session_id=s.id,
                event_type="onboarding_expired",
                correlation_id=s.correlation_id,
                payload={},
            )
        )
        n += 1
    if n:
        db.session.commit()
        logger.info("onboarding_sweep_expired count=%s", n)
    return n


def attach_exchange_context(
    session: OnboardingSession,
    *,
    workspace_id: str,
    hints: Dict[str, Any],
    discovery: Optional[Dict[str, Any]] = None,
) -> None:
    """Persist hints / discovery snapshot on the session before account write."""
    pl = dict(session.session_payload or {})
    pl["exchange_hints"] = hints
    if discovery is not None:
        pl["last_discovery"] = {
            "requires_user_choice": discovery.get("requires_user_choice"),
            "ambiguity": discovery.get("ambiguity"),
        }
    session.session_payload = pl
    session.last_event_at = _now()
    db.session.commit()
