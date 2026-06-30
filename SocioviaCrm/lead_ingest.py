"""
Lead Ingest - Shared WhatsApp -> CRM Lead bridge
================================================

Single source of truth for turning a WhatsApp conversation into a CRM Lead.

Used by:
    - whatsapp/webhook.py auto-capture: when a brand-new inbound conversation is
      created (or refreshed) we mirror it into the CRM as a Lead.
    - SocioviaCrm/routes/leads.py manual endpoint (POST /api/leads/from-conversation):
      an agent promotes an existing conversation to a Lead.

Design contract:
    - NEVER raises. Callers (especially the webhook) must not break if lead
      creation fails. On any error we rollback and return None.
    - Multi-tenant: workspace_id comes from account.workspace_id (the conversation
      itself has no workspace_id). It is stored/compared as TEXT (string).
    - Idempotent / dedupe-safe: an existing Lead (by external_id, then phone)
      in the same workspace is updated (last_interaction_at) instead of duplicated.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Forward-only lifecycle order for a CRM lead. Used by advance_lead_status to
# enforce no-downgrade / idempotent transitions (a lead can only move right).
LEAD_STATUS_ORDER = ["new", "contacted", "qualified", "proposal", "closed"]


def _model_columns(model):
    """Return the set of mapped column attribute names for a SQLAlchemy model."""
    try:
        return {c.key for c in model.__table__.columns}
    except Exception:
        return set()


def upsert_contact_from_conversation(conversation, account, db_session=None):
    """
    Create or update a CRM Contact mirroring a WhatsApp conversation, so a captured
    WhatsApp lead also appears in the Contacts section.

    Dedupe-safe (by external_id, then phone, scoped to workspace) and NEVER raises —
    a contact failure must not affect lead capture or message processing.
    Returns the Contact instance or None.
    """
    from flask import current_app

    try:
        if conversation is None or account is None:
            return None

        try:
            Contact = current_app.crm_models["Contact"]
        except Exception:
            Contact = None
        if Contact is None:
            logger.warning("upsert_contact_from_conversation: Contact model not configured")
            return None

        workspace_id = account.workspace_id
        if not workspace_id:
            return None
        workspace_id = str(workspace_id)

        session = db_session or current_app.db.session
        cols = _model_columns(Contact)

        user_phone = getattr(conversation, "user_phone", None)
        external_id = str(conversation.id)
        last_contacted = getattr(conversation, "last_inbound_at", None) or datetime.utcnow()

        # ---- Dedupe: external_id (whatsapp) first, then phone, scoped to workspace ----
        existing = None
        try:
            q = session.query(Contact).filter(Contact.workspace_id == workspace_id)
            if "external_id" in cols and "external_source" in cols:
                existing = q.filter(
                    Contact.external_source == "whatsapp",
                    Contact.external_id == external_id,
                ).first()
            if existing is None and user_phone and "phone" in cols:
                existing = (
                    session.query(Contact)
                    .filter(Contact.workspace_id == workspace_id, Contact.phone == user_phone)
                    .first()
                )
        except Exception:
            logger.exception("upsert_contact_from_conversation: dedupe lookup failed")
            existing = None

        if existing is not None:
            try:
                if "last_contacted" in cols:
                    existing.last_contacted = last_contacted
                if "name" in cols and not getattr(existing, "name", None):
                    existing.name = getattr(conversation, "user_name", None) or user_phone
                if "updated_at" in cols:
                    existing.updated_at = datetime.utcnow()
                session.add(existing)
                session.commit()
            except Exception:
                logger.exception("upsert_contact_from_conversation: failed to refresh existing contact")
                try:
                    session.rollback()
                except Exception:
                    pass
                return None
            return existing

        values = {
            "name": getattr(conversation, "user_name", None) or user_phone,
            "phone": user_phone,
            "status": "active",
            "external_source": "whatsapp",
            "external_id": external_id,
            "sync_status": "in_sync",
            "last_contacted": last_contacted,
            "created_at": getattr(conversation, "created_at", None) or datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if "workspace_id" in cols:
            values["workspace_id"] = workspace_id

        kwargs = {k: v for k, v in values.items() if k in cols}

        try:
            contact = Contact(**kwargs)
            session.add(contact)
            session.commit()
        except Exception:
            logger.exception("upsert_contact_from_conversation: failed to create contact")
            try:
                session.rollback()
            except Exception:
                pass
            return None

        logger.info(
            "Created CRM contact %s from whatsapp conversation %s (workspace %s)",
            getattr(contact, "id", "?"), conversation.id, workspace_id,
        )
        return contact

    except Exception:
        logger.exception("upsert_contact_from_conversation: unexpected error")
        try:
            sess = db_session
            if sess is None:
                from flask import current_app as _ca
                sess = _ca.db.session
            sess.rollback()
        except Exception:
            pass
        return None


def upsert_lead_from_conversation(conversation, account, db_session=None):
    """
    Create or update a CRM Lead from a WhatsApp conversation.

    Args:
        conversation: WhatsAppConversation instance (must have id, user_phone, etc.)
        account: WhatsAppAccount instance (provides workspace_id / display_phone_number).
        db_session: Optional SQLAlchemy session. Defaults to current_app.db.session.

    Returns:
        The Lead instance (created or updated) on success, or None on failure /
        when no workspace can be resolved.

    This helper is shared by the webhook auto-capture and the manual
    /api/leads/from-conversation endpoint. It never raises.
    """
    from flask import current_app

    try:
        if conversation is None or account is None:
            logger.warning("upsert_lead_from_conversation: missing conversation or account")
            return None

        Lead = None
        try:
            Lead = current_app.crm_models["Lead"]
        except Exception:
            Lead = None
        if Lead is None:
            logger.warning("upsert_lead_from_conversation: Lead model not configured")
            return None

        # workspace_id lives on the account; DB stores it as TEXT -> always a string.
        workspace_id = account.workspace_id
        if not workspace_id:
            logger.info("upsert_lead_from_conversation: no workspace_id on account, skipping")
            return None
        workspace_id = str(workspace_id)

        session = db_session or current_app.db.session
        cols = _model_columns(Lead)

        user_phone = getattr(conversation, "user_phone", None)
        external_id = str(conversation.id)
        last_interaction = getattr(conversation, "last_inbound_at", None) or datetime.utcnow()

        # ---- Dedupe: external_id (whatsapp) first, then phone, scoped to workspace ----
        existing = None
        try:
            q = session.query(Lead).filter(Lead.workspace_id == workspace_id)
            if "external_id" in cols and "external_source" in cols:
                existing = q.filter(
                    Lead.external_source == "whatsapp",
                    Lead.external_id == external_id,
                ).first()
            if existing is None and user_phone and "phone" in cols:
                existing = (
                    session.query(Lead)
                    .filter(Lead.workspace_id == workspace_id, Lead.phone == user_phone)
                    .first()
                )
        except Exception:
            logger.exception("upsert_lead_from_conversation: dedupe lookup failed")
            existing = None

        if existing is not None:
            # Refresh interaction timestamp only (do NOT duplicate).
            try:
                if "last_interaction_at" in cols:
                    existing.last_interaction_at = last_interaction
                if "updated_at" in cols:
                    existing.updated_at = datetime.utcnow()
                session.add(existing)
                session.commit()
            except Exception:
                logger.exception("upsert_lead_from_conversation: failed to refresh existing lead")
                try:
                    session.rollback()
                except Exception:
                    pass
                return None
            # Mirror into Contacts so the lead also shows in the Contacts section.
            try:
                upsert_contact_from_conversation(conversation, account, db_session=session)
            except Exception:
                logger.exception("upsert_lead_from_conversation: mirror contact failed")
            # Recompute the automatic lead score from the refreshed state.
            try:
                from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
                recompute_and_save_lead_score(existing, db_session=session)
            except Exception:
                logger.warning(
                    "upsert_lead_from_conversation: lead score recompute failed (existing lead)",
                    exc_info=True,
                )
            return existing

        # ---- Create a new Lead, only setting columns that exist on the model ----
        details = {
            "ad_id": getattr(conversation, "ad_id", None),
            "campaign_id": getattr(conversation, "campaign_id", None),
            "adset_id": getattr(conversation, "adset_id", None),
            "ctwa_clid": getattr(conversation, "ctwa_clid", None),
            "entry_source": getattr(conversation, "entry_source", None),
            "conversation_id": conversation.id,
            "account_phone": getattr(account, "display_phone_number", None),
        }

        values = {
            "name": getattr(conversation, "user_name", None) or user_phone,
            "phone": user_phone,
            "status": "new",
            "source": getattr(conversation, "entry_source", None) or "whatsapp",
            "external_source": "whatsapp",
            "external_id": external_id,
            "sync_status": "in_sync",
            "details": details,
            "last_interaction_at": last_interaction,
            "created_at": getattr(conversation, "created_at", None) or datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        if "workspace_id" in cols:
            values["workspace_id"] = workspace_id

        kwargs = {k: v for k, v in values.items() if k in cols}

        try:
            lead = Lead(**kwargs)
            session.add(lead)
            session.commit()
        except Exception:
            logger.exception("upsert_lead_from_conversation: failed to create lead")
            try:
                session.rollback()
            except Exception:
                pass
            return None

        logger.info(
            "Created CRM lead %s from whatsapp conversation %s (workspace %s)",
            getattr(lead, "id", "?"), conversation.id, workspace_id,
        )
        # Mirror into Contacts so the lead also shows in the Contacts section.
        try:
            upsert_contact_from_conversation(conversation, account, db_session=session)
        except Exception:
            logger.exception("upsert_lead_from_conversation: mirror contact failed")
        # Recompute the automatic lead score for the newly created lead.
        try:
            from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
            recompute_and_save_lead_score(lead, db_session=session)
        except Exception:
            logger.warning(
                "upsert_lead_from_conversation: lead score recompute failed (new lead)",
                exc_info=True,
            )
        return lead

    except Exception:
        logger.exception("upsert_lead_from_conversation: unexpected error")
        try:
            sess = db_session
            if sess is None:
                from flask import current_app as _ca
                sess = _ca.db.session
            sess.rollback()
        except Exception:
            pass
        return None


def advance_lead_status(workspace_id, phone=None, external_id=None,
                        target_status="contacted", reason=None, db_session=None):
    """
    Forward-only, idempotent "advance lead status" service.

    Moves an EXISTING CRM lead rightwards through LEAD_STATUS_ORDER
    (new -> contacted -> qualified -> proposal -> closed) based on observed
    behavior. Shared by WhatsApp hooks that detect engagement signals.

    Behavior contract:
        - Never creates a lead here (lookup only). If no lead is found -> None.
        - FORWARD-ONLY / NO-DOWNGRADE: if the target status is at or behind the
          lead's current status, the lead is returned UNCHANGED (no write, no
          activity). This makes the call idempotent and safe to fire repeatedly.
        - Records a status_change Activity on a successful advance (best-effort;
          an activity failure does not undo the status change).
        - NEVER raises. On any error it rolls back and returns None.

    Args:
        workspace_id: Tenant id (stored/compared as TEXT -> coerced to str).
        phone: Lead phone for fallback lookup.
        external_id: WhatsApp external id (usually the conversation id) for the
            primary (external_source == "whatsapp") lookup.
        target_status: Desired status to advance to (default "contacted").
        reason: Optional human/auto reason recorded on the activity.
        db_session: Optional SQLAlchemy session. Defaults to current_app.db.session.

    Returns:
        The Lead instance (advanced or already-ahead/unchanged), or None when no
        lead/model/workspace is resolved or on error.
    """
    from flask import current_app

    try:
        Lead = None
        try:
            Lead = current_app.crm_models["Lead"]
        except Exception:
            Lead = None
        if Lead is None:
            logger.warning("advance_lead_status: Lead model not configured")
            return None

        if not workspace_id:
            logger.info("advance_lead_status: no workspace_id, skipping")
            return None
        workspace_id = str(workspace_id)

        session = db_session or current_app.db.session
        cols = _model_columns(Lead)

        # ---- Locate the lead (lookup only, never create) ----
        lead = None
        try:
            if (
                external_id is not None
                and "external_id" in cols
                and "external_source" in cols
            ):
                lead = (
                    session.query(Lead)
                    .filter(
                        Lead.workspace_id == workspace_id,
                        Lead.external_source == "whatsapp",
                        Lead.external_id == str(external_id),
                    )
                    .first()
                )
            if lead is None and phone and "phone" in cols:
                lead = (
                    session.query(Lead)
                    .filter(Lead.workspace_id == workspace_id, Lead.phone == phone)
                    .first()
                )
        except Exception:
            logger.exception("advance_lead_status: lead lookup failed")
            lead = None

        if lead is None:
            logger.info(
                "advance_lead_status: no lead found (workspace %s, external_id %s, phone %s)",
                workspace_id, external_id, phone,
            )
            return None

        # ---- Forward-only / no-downgrade guard ----
        # Unknown target -> we don't know where it ranks, so leave the lead as-is.
        if target_status not in LEAD_STATUS_ORDER:
            logger.info(
                "advance_lead_status: unknown target status %r, leaving lead %s unchanged",
                target_status, getattr(lead, "id", "?"),
            )
            return lead

        target_index = LEAD_STATUS_ORDER.index(target_status)
        # Unknown current status is treated as -1 so any valid target advances it.
        current_status = getattr(lead, "status", None)
        if current_status in LEAD_STATUS_ORDER:
            current_index = LEAD_STATUS_ORDER.index(current_status)
        else:
            current_index = -1

        if target_index <= current_index:
            # Already at or past target -> idempotent no-op, no write/activity.
            return lead

        # ---- Advance the lead ----
        try:
            lead.status = target_status
            if "updated_at" in cols:
                lead.updated_at = datetime.utcnow()
            session.add(lead)
            session.commit()
        except Exception:
            logger.exception("advance_lead_status: failed to update lead status")
            try:
                session.rollback()
            except Exception:
                pass
            return None

        logger.info(
            "Auto-advanced lead %s -> %s (%s)",
            getattr(lead, "id", "?"), target_status, reason or "auto",
        )

        # ---- Record a status_change activity (best-effort) ----
        try:
            Activity = None
            try:
                Activity = current_app.crm_models["Activity"]
            except Exception:
                Activity = None
            if Activity is not None:
                acols = _model_columns(Activity)
                avalues = {
                    "entity_type": "lead",
                    "entity_id": lead.id,
                    "type": "status_change",
                    "title": f"Status -> {target_status}",
                    "description": reason or f"Auto-advanced to {target_status}",
                    "timestamp": datetime.utcnow(),
                    "workspace_id": workspace_id,
                }
                akwargs = {k: v for k, v in avalues.items() if k in acols}
                activity = Activity(**akwargs)
                session.add(activity)
                session.commit()
        except Exception:
            logger.exception("advance_lead_status: failed to record status_change activity")
            try:
                session.rollback()
            except Exception:
                pass

        # Recompute the automatic lead score so it reflects the advanced status.
        try:
            from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
            recompute_and_save_lead_score(lead, db_session=session)
        except Exception:
            logger.warning(
                "advance_lead_status: lead score recompute failed",
                exc_info=True,
            )

        return lead

    except Exception:
        logger.exception("advance_lead_status: unexpected error")
        try:
            sess = db_session
            if sess is None:
                from flask import current_app as _ca
                sess = _ca.db.session
            sess.rollback()
        except Exception:
            pass
        return None


def set_lead_status(workspace_id, phone=None, external_id=None,
                    target_status="contacted", reason=None, db_session=None):
    """
    Exact "set lead status" service (MAY downgrade).

    Mirrors advance_lead_status for lookup/activity/score behavior, but sets the
    EXACT target_status regardless of its position in LEAD_STATUS_ORDER. Unlike
    advance_lead_status this can move a lead BACKWARD (e.g. qualified -> new).

    Behavior contract:
        - Never creates a lead here (lookup only). If no lead is found -> None.
        - Validates target_status is in LEAD_STATUS_ORDER; otherwise no-op and
          the lead is returned UNCHANGED (no write, no activity).
        - If the lead's current status already equals target_status -> no-op
          (idempotent; no write, no activity), lead returned unchanged.
        - Records a status_change Activity on a successful set (best-effort;
          an activity failure does not undo the status change).
        - NEVER raises. On any error it rolls back and returns None.

    Args:
        workspace_id: Tenant id (stored/compared as TEXT -> coerced to str).
        phone: Lead phone for fallback lookup.
        external_id: WhatsApp external id (usually the conversation id) for the
            primary (external_source == "whatsapp") lookup.
        target_status: Desired exact status to set (default "contacted").
        reason: Optional human/auto reason recorded on the activity.
        db_session: Optional SQLAlchemy session. Defaults to current_app.db.session.

    Returns:
        The Lead instance (set or already-equal/unchanged), or None when no
        lead/model/workspace is resolved or on error.
    """
    from flask import current_app

    try:
        Lead = None
        try:
            Lead = current_app.crm_models["Lead"]
        except Exception:
            Lead = None
        if Lead is None:
            logger.warning("set_lead_status: Lead model not configured")
            return None

        if not workspace_id:
            logger.info("set_lead_status: no workspace_id, skipping")
            return None
        workspace_id = str(workspace_id)

        session = db_session or current_app.db.session
        cols = _model_columns(Lead)

        # ---- Validate target (exact-set still requires a known status) ----
        if target_status not in LEAD_STATUS_ORDER:
            logger.info(
                "set_lead_status: unknown target status %r, skipping",
                target_status,
            )
            return None

        # ---- Locate the lead (lookup only, never create) ----
        lead = None
        try:
            if (
                external_id is not None
                and "external_id" in cols
                and "external_source" in cols
            ):
                lead = (
                    session.query(Lead)
                    .filter(
                        Lead.workspace_id == workspace_id,
                        Lead.external_source == "whatsapp",
                        Lead.external_id == str(external_id),
                    )
                    .first()
                )
            if lead is None and phone and "phone" in cols:
                lead = (
                    session.query(Lead)
                    .filter(Lead.workspace_id == workspace_id, Lead.phone == phone)
                    .first()
                )
        except Exception:
            logger.exception("set_lead_status: lead lookup failed")
            lead = None

        if lead is None:
            logger.info(
                "set_lead_status: no lead found (workspace %s, external_id %s, phone %s)",
                workspace_id, external_id, phone,
            )
            return None

        # ---- Idempotent no-op when already at the target status ----
        current_status = getattr(lead, "status", None)
        if current_status == target_status:
            return lead

        # ---- Set the lead's exact status (may move backward) ----
        try:
            lead.status = target_status
            if "updated_at" in cols:
                lead.updated_at = datetime.utcnow()
            session.add(lead)
            session.commit()
        except Exception:
            logger.exception("set_lead_status: failed to update lead status")
            try:
                session.rollback()
            except Exception:
                pass
            return None

        logger.info(
            "Set lead %s -> %s (%s)",
            getattr(lead, "id", "?"), target_status, reason or "manual",
        )

        # ---- Record a status_change activity (best-effort) ----
        try:
            Activity = None
            try:
                Activity = current_app.crm_models["Activity"]
            except Exception:
                Activity = None
            if Activity is not None:
                acols = _model_columns(Activity)
                avalues = {
                    "entity_type": "lead",
                    "entity_id": lead.id,
                    "type": "status_change",
                    "title": f"Status -> {target_status}",
                    "description": reason or f"Set to {target_status}",
                    "timestamp": datetime.utcnow(),
                    "workspace_id": workspace_id,
                }
                akwargs = {k: v for k, v in avalues.items() if k in acols}
                activity = Activity(**akwargs)
                session.add(activity)
                session.commit()
        except Exception:
            logger.exception("set_lead_status: failed to record status_change activity")
            try:
                session.rollback()
            except Exception:
                pass

        # Recompute the automatic lead score so it reflects the new status.
        try:
            from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
            recompute_and_save_lead_score(lead, db_session=session)
        except Exception:
            logger.warning(
                "set_lead_status: lead score recompute failed",
                exc_info=True,
            )

        return lead

    except Exception:
        logger.exception("set_lead_status: unexpected error")
        try:
            sess = db_session
            if sess is None:
                from flask import current_app as _ca
                sess = _ca.db.session
            sess.rollback()
        except Exception:
            pass
        return None


def advance_lead_status_from_conversation(conversation, account, target_status,
                                          reason=None, db_session=None):
    """
    Convenience wrapper: ensure the lead exists, then forward-advance its status.

    Intended for WhatsApp hooks that have a conversation + account in hand and
    want to bump the lead's lifecycle on a behavior signal.

    Steps:
        1. upsert_lead_from_conversation(...) so the lead exists (idempotent —
           creates if missing, refreshes if present).
        2. advance_lead_status(...) keyed by the conversation's external id /
           phone, scoped to account.workspace_id.

    NEVER raises. Returns the Lead instance or None.
    """
    try:
        if conversation is None or account is None:
            return None

        # Ensure the lead exists first (idempotent; never raises).
        try:
            upsert_lead_from_conversation(conversation, account, db_session=db_session)
        except Exception:
            logger.exception(
                "advance_lead_status_from_conversation: ensure-lead (upsert) failed"
            )

        return advance_lead_status(
            workspace_id=account.workspace_id,
            phone=getattr(conversation, "user_phone", None),
            external_id=str(conversation.id),
            target_status=target_status,
            reason=reason,
            db_session=db_session,
        )

    except Exception:
        logger.exception("advance_lead_status_from_conversation: unexpected error")
        return None


def set_lead_status_from_conversation(conversation, account, target_status,
                                      reason=None, db_session=None):
    """
    Convenience wrapper: ensure the lead exists, then set its EXACT status.

    Like advance_lead_status_from_conversation but uses set_lead_status, so the
    status is set exactly (MAY downgrade) rather than forward-only.

    Steps:
        1. upsert_lead_from_conversation(...) so the lead exists (idempotent —
           creates if missing, refreshes if present).
        2. set_lead_status(...) keyed by the conversation's external id / phone,
           scoped to account.workspace_id.

    NEVER raises. Returns the Lead instance or None.
    """
    try:
        if conversation is None or account is None:
            return None

        # Ensure the lead exists first (idempotent; never raises).
        try:
            upsert_lead_from_conversation(conversation, account, db_session=db_session)
        except Exception:
            logger.exception(
                "set_lead_status_from_conversation: ensure-lead (upsert) failed"
            )

        return set_lead_status(
            workspace_id=account.workspace_id,
            phone=getattr(conversation, "user_phone", None),
            external_id=str(conversation.id),
            target_status=target_status,
            reason=reason,
            db_session=db_session,
        )

    except Exception:
        logger.exception("set_lead_status_from_conversation: unexpected error")
        return None
