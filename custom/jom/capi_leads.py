"""
Phase 4 — Meta Conversions API (Conversion Leads) for the JOM vertical.

Reuses the existing SocioviaCrm.capi_service.send_capi_event. We add:
  - capture the Meta Lead ID (+ email) on the lifecycle row when a lead-form lead enters,
  - fire down-funnel events (QualifiedLead, Converted) so Meta's "Conversion Leads"
    performance goal can auto-optimise.
Junk leads: we simply DO NOT send a positive event (absence = low-quality signal).
All workspace-scoped; call behind is_jom_workspace().
"""
import logging
from datetime import datetime, timezone

from models import db
from .models import JomLifecycleState

logger = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc)


def set_lead_identity(workspace_id, customer_phone, meta_lead_id=None, email=None, ctwa_clid=None):
    """Store the Meta ad-attribution identity on the lifecycle row (captured at ingest).
    `ctwa_clid` = Click-to-WhatsApp click id (the JOM case); `meta_lead_id` = Lead Ads id.
    Only overwrites a field when a new non-empty value is given (idempotent per message)."""
    row = JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
    if not row:
        row = JomLifecycleState(workspace_id=int(workspace_id), customer_phone=str(customer_phone))
        db.session.add(row)
    if meta_lead_id:
        row.meta_lead_id = str(meta_lead_id)[:40]
    if ctwa_clid and not row.ctwa_clid:   # first click wins; don't clobber on later messages
        row.ctwa_clid = str(ctwa_clid)[:255]
    if email:
        row.email = str(email)[:160]
    row.updated_at = _now()
    db.session.commit()
    return row


def _send(workspace_id, customer_phone, event_name, value=None):
    """Fire a CAPI event. Fail-safe — never raises into the caller."""
    try:
        row = JomLifecycleState.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
        user_data = {"phone": customer_phone}
        if row and row.email:
            user_data["email"] = row.email
        # Click-to-WhatsApp attribution: ctwa_clid is Meta's key for messaging ads.
        if row and getattr(row, "ctwa_clid", None):
            user_data["ctwa_clid"] = row.ctwa_clid
        # Lead-Ads attribution: lead_id (instant forms). Both can be present; send what we have.
        if row and row.meta_lead_id:
            user_data["lead_id"] = row.meta_lead_id
        custom = {"value": value, "currency": "INR"} if value is not None else None
        from SocioviaCrm.capi_service import send_capi_event
        res = send_capi_event(
            event_name=event_name,
            user_data_dict=user_data,
            custom_data_dict=custom,
            workspace_id=int(workspace_id),
            action_source="business_messaging",
        )
        logger.info("[jom] CAPI %s sent for %s: %s", event_name, customer_phone, res)
        return res
    except Exception as exc:  # noqa: BLE001
        logger.warning("[jom] CAPI %s failed for %s: %s", event_name, customer_phone, exc)
        return {"success": False, "error": str(exc)}


def _record(workspace_id, customer_phone, field_at, field_ok, res):
    """Persist the CAPI send outcome on the lead so the dashboard can show it."""
    try:
        row = JomLifecycleState.query.filter_by(
            workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()
        if row:
            setattr(row, field_at, _now())
            setattr(row, field_ok, bool(res and res.get("success")))
            db.session.commit()
    except Exception:  # noqa: BLE001
        db.session.rollback()


def on_qualified(workspace_id, customer_phone):
    res = _send(workspace_id, customer_phone, "QualifiedLead")
    _record(workspace_id, customer_phone, "capi_qualified_at", "capi_qualified_ok", res)
    return res


def on_converted(workspace_id, customer_phone, value=None):
    res = _send(workspace_id, customer_phone, "Converted", value=value)
    _record(workspace_id, customer_phone, "capi_converted_at", "capi_converted_ok", res)
    return res
