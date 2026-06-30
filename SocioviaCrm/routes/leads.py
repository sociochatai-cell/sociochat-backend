from flask import Blueprint, request, jsonify, current_app, session
from datetime import datetime
from decimal import Decimal
from sqlalchemy.exc import SQLAlchemyError

bp = Blueprint("leads", __name__, url_prefix="/leads")


# --------- Utilities ---------
def parse_date(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s) if "T" in s else datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


def _get_lead_model():
    try:
        return current_app.crm_models.get("Lead")
    except Exception:
        return None


def _get_activity_model():
    try:
        return current_app.crm_models.get("Activity")
    except Exception:
        return None


# Valid activity_type enum values. Keep in sync with the DB enum so we never
# attempt to insert an unknown label (which would 500 on commit).
ALLOWED_ACTIVITY_TYPES = {
    "email_opened", "page_visit", "call", "note_created",
    "status_change", "stage_change", "deal_created", "meeting",
}


def normalize_activity_type(t):
    """Normalize an incoming activity type to a known enum label.
    Falls back to 'note_created' for missing/unknown values."""
    if not t:
        return "note_created"
    tt = str(t).strip().lower()
    return tt if tt in ALLOWED_ACTIVITY_TYPES else "note_created"


def _get_request_user_id():
    """
    Try to determine user_id from:
      1) session["user_id"]
      2) Authorization: Bearer <id>
      3) X-User-Id header
    """
    uid = session.get("user_id")
    if uid:
        try:
            return int(uid)
        except Exception:
            return uid

    auth = request.headers.get("Authorization", "")
    if auth and auth.lower().startswith("bearer "):
        token_part = auth.split(None, 1)[1]
        if token_part.isdigit():
            return int(token_part)

    xuid = request.headers.get("X-User-Id")
    if xuid:
        try:
            return int(xuid)
        except Exception:
            return xuid
    return None


# --------- List Leads ---------
@bp.route("", methods=["GET"])
def list_leads():
    db = current_app.db
    Lead = _get_lead_model()
    if not Lead:
        return jsonify({"error": "Lead model not configured"}), 500

    status = request.args.get("status")
    search = request.args.get("search")
    workspace_id = request.args.get("workspace_id")

    # SECURITY: workspace_id is REQUIRED to prevent data leakage across workspaces
    if not workspace_id:
        return jsonify({"error": "workspace_id is required", "code": "WORKSPACE_REQUIRED"}), 400

    q = db.session.query(Lead)

    # Scope by workspace (always required now)
    if hasattr(Lead, "workspace_id"):
        # workspace_id is TEXT in your DB → compare as string
        q = q.filter(Lead.workspace_id == workspace_id)

    if status:
        q = q.filter(Lead.status == status)

    if search:
        term = f"%{search.lower()}%"
        q = q.filter(
            db.or_(
                db.func.lower(Lead.name).like(term),
                db.func.lower(Lead.email).like(term),
                db.func.lower(Lead.company).like(term),
            )
        )

    try:
        leads = q.order_by(Lead.created_at.desc()).all()
    except SQLAlchemyError as e:
        current_app.logger.exception("list_leads DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500

    results = []
    for l in leads:
        results.append({
            "id": l.id,
            "name": l.name,
            "email": l.email,
            "phone": getattr(l, "phone", None),
            "company": getattr(l, "company", None),
            "job_title": getattr(l, "job_title", None),
            "status": l.status,
            "source": getattr(l, "source", None),
            "external_source": getattr(l, "external_source", None),
            "external_id": getattr(l, "external_id", None),
            "sync_status": getattr(l, "sync_status", None) or "in_sync",
            "last_sync_at": (l.last_sync_at.isoformat() if getattr(l, "last_sync_at", None) else None),
            "score": getattr(l, "score", None),
            "lastInteraction": (l.last_interaction_at.isoformat() if getattr(l, "last_interaction_at", None) else None),
            "value": float(l.value or 0.0),
            "details": getattr(l, "details", None),
            "created_at": (l.created_at.isoformat() if getattr(l, "created_at", None) else None),
        })
    return jsonify(results)


# --------- Create Lead ---------
@bp.route("", methods=["POST"])
def create_lead():
    """
    Create a lead.
    Supports optional external metadata:
      - external_source: e.g. "meta_leadgen", "zapier", "gform"
      - external_id: external system id
    If external_source+external_id provided, we try to dedupe/upsert.
    """
    db = current_app.db
    Lead = _get_lead_model()
    Activity = _get_activity_model()

    if not Lead:
        return jsonify({"error": "Lead model not configured"}), 500
    if not Activity:
        return jsonify({"error": "Activity model not configured"}), 500

    payload = request.get_json() or {}
    name = payload.get("name")
    if not name:
        return jsonify({"error": "name is required"}), 400

    # workspace_id is NOT NULL in DB, so we MUST take it from query params
    workspace_id_param = request.args.get("workspace_id")
    if hasattr(Lead, "workspace_id"):
        if not workspace_id_param:
            return jsonify({"error": "workspace_id query param required"}), 400
        # In your DB it's TEXT, so keep as string
        ws_val = workspace_id_param
    else:
        ws_val = None

    # owner / user handling
    user_id_param = request.args.get("user_id")
    owner_id = payload.get("owner_id")

    if owner_id is None:
        if user_id_param is not None:
            try:
                owner_id = int(user_id_param)
            except Exception:
                owner_id = user_id_param
        else:
            fallback = _get_request_user_id()
            if fallback is not None:
                owner_id = fallback

    # external metadata (optional)
    external_source = payload.get("external_source")
    external_id = payload.get("external_id")

    # If external metadata present try dedupe by external id first (workspace-scoped)
    if external_source and external_id:
        try:
            q = db.session.query(Lead)
            if ws_val is not None and hasattr(Lead, "workspace_id"):
                q = q.filter(Lead.workspace_id == ws_val)
            # safe attribute checks
            if hasattr(Lead, "external_source") and hasattr(Lead, "external_id"):
                existing = q.filter(Lead.external_source == external_source, Lead.external_id == str(external_id)).one_or_none()
            else:
                existing = None
        except Exception:
            existing = None

        if existing:
            # update last_sync_at / last_interaction_at
            try:
                if hasattr(existing, "last_sync_at"):
                    existing.last_sync_at = datetime.utcnow()
                if hasattr(existing, "last_interaction_at"):
                    existing.last_interaction_at = datetime.utcnow()
                if hasattr(existing, "sync_status"):
                    existing.sync_status = "in_sync"
                db.session.add(existing)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
            return jsonify({"id": existing.id, "deduped": True}), 200

    # Fallback dedupe by email/phone in same workspace (if provided)
    email = payload.get("email")
    phone = payload.get("phone")
    if (email or phone) and ws_val is not None:
        try:
            q2 = db.session.query(Lead).filter(Lead.workspace_id == ws_val)
            if email:
                q2 = q2.filter(Lead.email == email)
            if phone:
                q2 = q2.filter(Lead.phone == phone)
            existing2 = q2.first()
        except Exception:
            existing2 = None

        if existing2:
            # update existing with any missing info and mark in_sync if external metadata provided
            changed = False
            if not existing2.name and name:
                existing2.name = name
                changed = True
            if not existing2.email and email:
                existing2.email = email
                changed = True
            if not existing2.phone and phone:
                existing2.phone = phone
                changed = True
            if external_source and hasattr(existing2, "external_source"):
                existing2.external_source = existing2.external_source or external_source
                changed = True
            if external_id and hasattr(existing2, "external_id"):
                existing2.external_id = existing2.external_id or str(external_id)
                changed = True
            if changed:
                try:
                    if hasattr(existing2, "last_sync_at") and external_source:
                        existing2.last_sync_at = datetime.utcnow()
                    if hasattr(existing2, "sync_status") and external_source:
                        existing2.sync_status = "in_sync"
                    existing2.updated_at = datetime.utcnow()
                    db.session.add(existing2)
                    db.session.commit()
                except Exception:
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
            return jsonify({"id": existing2.id, "deduped": True}), 200

    # Build Lead instance (new)
    try:
        l = Lead(
            name=name,
            email=payload.get("email"),
            phone=payload.get("phone"),
            company=payload.get("company"),
            job_title=payload.get("job_title"),
            status=payload.get("status") or "new",
            source=payload.get("source") or (external_source if external_source else None),
            score=payload.get("score") or 0,
            value=Decimal(str(payload.get("value", 0))) if payload.get("value") is not None else Decimal("0"),
            owner_id=owner_id,
            **({"workspace_id": ws_val} if ws_val is not None and hasattr(Lead, "workspace_id") else {}),
        )
        # sync metadata
        if hasattr(l, "external_source"):
            l.external_source = external_source
        if hasattr(l, "external_id") and external_id:
            l.external_id = str(external_id)
        # set sync_status/last_sync_at for external sources
        if external_source:
            if hasattr(l, "sync_status"):
                l.sync_status = "in_sync"
            if hasattr(l, "last_sync_at"):
                l.last_sync_at = datetime.utcnow()
        else:
            if hasattr(l, "sync_status") and not getattr(l, "sync_status", None):
                l.sync_status = "in_sync"

        # ensure created/updated timestamps exist
        if hasattr(l, "created_at") and not getattr(l, "created_at", None):
            l.created_at = datetime.utcnow()
        if hasattr(l, "updated_at"):
            l.updated_at = datetime.utcnow()

        db.session.add(l)
        db.session.commit()
        
        # Trigger CAPI Lead Event
        try:
            if l.email or l.phone:
                from SocioviaCrm.capi_service import send_capi_event
                send_capi_event(
                    event_name="Lead",
                    user_data_dict={
                        "email": l.email,
                        "phone": l.phone,
                        "name": l.name,
                        "client_ip_address": request.remote_addr,
                        "client_user_agent": request.headers.get("User-Agent")
                    },
                    workspace_id=ws_val,
                    action_source="system_generated"
                )
        except Exception as capi_err:
            current_app.logger.error(f"Failed to trigger CAPI Lead event: {capi_err}")
            
    except SQLAlchemyError as e:
        current_app.logger.exception("create_lead DB error on Lead insert: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500

    # Create activity note
    try:
        activity_kwargs = {
            "entity_type": "lead",
            "entity_id": l.id,
            "type": "note_created",
            "title": "Lead created",
            "description": f"Automated note: lead created (source={external_source or 'internal'})",
            "timestamp": datetime.utcnow(),
        }

        # propagate workspace_id to Activity if the model has it
        if hasattr(Activity, "workspace_id"):
            activity_kwargs["workspace_id"] = getattr(l, "workspace_id", None)

        a = Activity(**activity_kwargs)
        db.session.add(a)
        db.session.commit()
    except Exception:
        current_app.logger.exception("create_lead: failed to create Activity")
        try:
            db.session.rollback()
        except Exception:
            pass

    # ✅ Drip Campaign Auto-Enrollment Hook
    try:
        from whatsapp.drip_routes import enroll_from_crm
        lead_data = {
            'name': l.name,
            'email': l.email,
            'phone': l.phone,
            'company': l.company,
            'source': l.source,
            'status': l.status,
            'job_title': getattr(l, 'job_title', None),
            'value': str(l.value) if l.value else None,
        }
        ws_id = int(ws_val) if ws_val else None
        if ws_id:
            result = enroll_from_crm('lead', lead_data, ws_id)
            if result.get('enrolled_campaigns', 0) > 0:
                current_app.logger.info(f"[CRM Drip] Lead {l.id} enrolled in {result['enrolled_campaigns']} drip campaigns")
    except Exception as drip_err:
        current_app.logger.warning(f"[CRM Drip] Failed to process drip enrollment for lead {l.id}: {drip_err}")

    return jsonify({"id": l.id}), 201


# --------- Create Lead from WhatsApp Conversation ---------
@bp.route("/from-conversation", methods=["POST"])
def lead_from_conversation():
    """
    Promote a WhatsApp conversation to a CRM Lead.

    Body: {"conversation_id": <id>, "name"?: str, "status"?: str}

    Uses the shared SocioviaCrm.lead_ingest.upsert_lead_from_conversation bridge
    (the same logic the inbound webhook auto-capture uses), so dedupe is
    consistent: existing leads (by external_id then phone) are refreshed, not
    duplicated. workspace_id is resolved from the conversation's account.
    """
    db = current_app.db
    Lead = _get_lead_model()
    if not Lead:
        return jsonify({"error": "Lead model not configured"}), 500

    payload = request.get_json(silent=True) or {}
    conversation_id = payload.get("conversation_id")
    if conversation_id is None:
        return jsonify({"error": "conversation_id is required"}), 400

    # Lazy import to avoid circular imports between SocioviaCrm and whatsapp.
    try:
        from whatsapp.models import WhatsAppConversation, WhatsAppAccount
        from SocioviaCrm.lead_ingest import upsert_lead_from_conversation
    except Exception as imp_err:
        current_app.logger.exception("lead_from_conversation: import failed")
        return jsonify({"error": "internal import error", "details": str(imp_err)}), 500

    conversation = db.session.get(WhatsAppConversation, conversation_id)
    if not conversation:
        return jsonify({"error": "conversation not found"}), 404

    account = db.session.get(WhatsAppAccount, conversation.account_id)
    if not account:
        return jsonify({"error": "account for conversation not found"}), 404

    # Optional overrides applied to the conversation before ingest.
    name_override = payload.get("name")
    if name_override:
        conversation.user_name = name_override

    lead = upsert_lead_from_conversation(conversation, account, db_session=db.session)
    if lead is None:
        return jsonify({"error": "could not create or update lead from conversation"}), 500

    # Optional status override (the bridge always sets "new" for fresh leads).
    status_override = payload.get("status")
    created = getattr(lead, "created_at", None) == getattr(lead, "updated_at", None)
    if status_override:
        try:
            lead.status = status_override
            lead.updated_at = datetime.utcnow()
            db.session.add(lead)
            db.session.commit()
        except SQLAlchemyError:
            current_app.logger.exception("lead_from_conversation: status override failed")
            try:
                db.session.rollback()
            except Exception:
                pass

    result = {
        "id": lead.id,
        "name": lead.name,
        "email": getattr(lead, "email", None),
        "phone": getattr(lead, "phone", None),
        "company": getattr(lead, "company", None),
        "status": lead.status,
        "source": getattr(lead, "source", None),
        "external_source": getattr(lead, "external_source", None),
        "external_id": getattr(lead, "external_id", None),
        "sync_status": getattr(lead, "sync_status", None) or "in_sync",
        "score": getattr(lead, "score", None),
        "value": float(lead.value or 0.0) if getattr(lead, "value", None) is not None else 0.0,
        "details": getattr(lead, "details", None),
        "lastInteraction": (lead.last_interaction_at.isoformat() if getattr(lead, "last_interaction_at", None) else None),
        "created_at": (lead.created_at.isoformat() if getattr(lead, "created_at", None) else None),
        "conversation_id": conversation.id,
    }
    return jsonify(result), (201 if created else 200)


# --------- Patch Lead ---------
@bp.route("/<lead_id>", methods=["PATCH"])
def patch_lead(lead_id):
    db = current_app.db
    Lead = _get_lead_model()
    Activity = _get_activity_model()

    if not Lead:
        return jsonify({"error": "Lead model not configured"}), 500

    l = db.session.get(Lead, lead_id)
    if not l:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json() or {}
    allowed = {"status", "name", "email", "phone", "company", "job_title", "score", "value", "owner_id", "source", "notes"}

    old_status = l.status

    # column names on the Lead model (used to decide where to persist notes)
    try:
        lead_cols = set(l.__class__.__table__.columns.keys())
    except Exception:
        lead_cols = set()

    for k, v in payload.items():
        if k in allowed:
            if k == "value" and v is not None:
                try:
                    setattr(l, k, Decimal(str(v)))
                except Exception:
                    setattr(l, k, v)
            elif k == "notes" and "notes" not in lead_cols:
                # Lead has no dedicated notes column on this schema; persist the
                # note inside the JSON `details` column so it isn't lost.
                if "details" in lead_cols:
                    try:
                        existing = dict(getattr(l, "details", None) or {})
                    except Exception:
                        existing = {}
                    existing["notes"] = v
                    l.details = existing
                else:
                    setattr(l, k, v)
            else:
                setattr(l, k, v)

    l.updated_at = datetime.utcnow()

    # If this lead has an external_source, mark it as needing outbound sync
    if getattr(l, "external_source", None):
        try:
            if hasattr(l, "sync_status"):
                l.sync_status = "pending_push"
        except Exception:
            pass

    try:
        db.session.commit()
        
        # Trigger CAPI QualifiedLead Event
        if old_status != "qualified" and l.status == "qualified":
            try:
                if l.email or l.phone:
                    from SocioviaCrm.capi_service import send_capi_event
                    send_capi_event(
                        event_name="QualifiedLead",
                        user_data_dict={
                            "email": l.email,
                            "phone": l.phone,
                            "name": l.name,
                            "client_ip_address": request.remote_addr,
                            "client_user_agent": request.headers.get("User-Agent")
                        },
                        workspace_id=getattr(l, "workspace_id", None),
                        action_source="system_generated"
                    )
            except Exception as capi_err:
                current_app.logger.error(f"Failed to trigger CAPI QualifiedLead event: {capi_err}")
                
    except SQLAlchemyError as e:
        current_app.logger.exception("patch_lead DB error on Lead update")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500

    # Log status change activity
    if Activity and "status" in payload:
        try:
            activity_kwargs = {
                "entity_type": "lead",
                "entity_id": l.id,
                "type": "status_change",
                "title": f"Status -> {l.status}",
                "timestamp": datetime.utcnow(),
            }

            if hasattr(Activity, "workspace_id"):
                activity_kwargs["workspace_id"] = getattr(l, "workspace_id", None)

            a = Activity(**activity_kwargs)
            db.session.add(a)
            db.session.commit()
        except Exception:
            current_app.logger.exception("patch_lead: failed to create status Activity")
            try:
                db.session.rollback()
            except Exception:
                pass

    # Recompute the automatic lead score from the lead's new state, unless the
    # caller explicitly set "score" in this request (a manual score is respected).
    if "score" not in payload:
        try:
            from SocioviaCrm.lead_scoring import recompute_and_save_lead_score
            recompute_and_save_lead_score(l)
        except Exception:
            current_app.logger.warning(
                "patch_lead: lead score recompute failed", exc_info=True
            )

    return jsonify({"ok": True})


# --------- Lead Activity ---------
@bp.route("/<lead_id>/activity", methods=["GET"])
def lead_activity(lead_id):
    db = current_app.db
    Activity = _get_activity_model()
    if not Activity:
        return jsonify([])

    acts = (
        db.session.query(Activity)
        .filter(Activity.entity_type == "lead", Activity.entity_id == lead_id)
        .order_by(Activity.timestamp.desc())
        .all()
    )

    out = []
    for a in acts:
        out.append({
            "id": a.id,
            "title": a.title,
            "timestamp": a.timestamp.isoformat() if getattr(a, "timestamp", None) else None,
            "type": a.type,
            "description": a.description,
        })
    return jsonify(out)


# POST /api/leads/<lead_id>/activity
@bp.route("/<lead_id>/activity", methods=["POST"])
def add_lead_activity(lead_id):
    db = current_app.db
    Activity = _get_activity_model()
    Lead = _get_lead_model()

    if not Activity or not Lead:
        return jsonify({"error": "Activity or Lead model not configured"}), 500

    # ensure lead exists (optional)
    lead_obj = db.session.get(Lead, lead_id)
    if not lead_obj:
        return jsonify({"error": "lead not found"}), 404

    payload = request.get_json(silent=True) or {}
    try:
        incoming_type = payload.get("type") or payload.get("activity_type")
        activity_kwargs = {
            "entity_type": "lead",
            "entity_id": lead_id,
            "type": normalize_activity_type(incoming_type),
            "title": payload.get("title") or (incoming_type or "Activity"),
            "description": payload.get("description"),
            "timestamp": datetime.utcnow(),
        }

        # propagate workspace_id if Activity model has it
        if hasattr(Activity, "workspace_id"):
            ws = request.args.get("workspace_id") or request.headers.get("X-Workspace-ID")
            # try to read workspace from lead if present and not provided
            if not ws and hasattr(lead_obj, "workspace_id"):
                ws = getattr(lead_obj, "workspace_id", None)
            if ws is not None:
                activity_kwargs["workspace_id"] = ws

        a = Activity(**activity_kwargs)
        db.session.add(a)
        db.session.commit()
        return jsonify({"id": getattr(a, "id", None)}), 201
    except Exception:
        current_app.logger.exception("add_lead_activity failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error"}), 500
