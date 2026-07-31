# crm_management/routes/deals.py
import inspect
import logging
from flask import Blueprint, request, jsonify, current_app, session, make_response
from datetime import datetime
from decimal import Decimal
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import Integer, BigInteger

logger = logging.getLogger(__name__)
bp = Blueprint("deals", __name__, url_prefix="/deals")


# ---------------- Utilities ----------------
def _get_deal_model():
    try:
        return current_app.crm_models.get("Deal")
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


def _get_note_model():
    try:
        return current_app.crm_models.get("Note")
    except Exception:
        return None


def _get_request_user_id():
    """Resolve the user id from the server session or a SIGNED Bearer JWT only.

    Forgeable plaintext ``Bearer <digits>`` / ``X-User-Id`` paths removed — any
    caller could previously impersonate any user id. Identity now goes through
    auth_core, consistent with the rest of the app.
    """
    from auth_core import authenticated_user_id
    return authenticated_user_id()


def _require_owned_workspace(requested_workspace_id=None):
    """Resolve + verify the caller owns a workspace (fail CLOSED).

    Returns (workspace, None) on success or (None, (response, status)) so callers
    can ``return err``. Lazy import of tenant.context avoids circular imports.
    """
    from tenant.context import get_current_user, resolve_owned_workspace
    user = get_current_user()
    if not user:
        return None, (jsonify({"success": False, "error": "authentication_required"}), 401)
    return resolve_owned_workspace(user, requested_workspace_id)


def _authorize_row(row):
    """Verify the authenticated caller owns the workspace this CRM row belongs to.

    Returns None when authorized, else an (response, status) tuple to return.
    Fails CLOSED: no user -> 401; workspace not owned -> 403.
    """
    from tenant.context import get_current_user, user_owns_workspace
    user = get_current_user()
    if not user:
        return (jsonify({"success": False, "error": "authentication_required"}), 401)
    if not user_owns_workspace(user, getattr(row, "workspace_id", None)):
        return (jsonify({"success": False, "error": "forbidden"}), 403)
    return None


def _model_columns(model):
    try:
        return list(model.__table__.columns.keys())
    except Exception:
        return []


def _serialize_deal(d, allowed_fields=None):
    if not allowed_fields:
        try:
            allowed_fields = list(d.__class__.__table__.columns.keys())
        except Exception:
            allowed_fields = []
    out = {}
    for k in allowed_fields:
        v = getattr(d, k, None)
        # datetimes -> iso
        try:
            if hasattr(v, "isoformat"):
                out[k] = v.isoformat()
            else:
                out[k] = v
        except Exception:
            out[k] = v
    if hasattr(d, "id"):
        out.setdefault("id", getattr(d, "id"))
    return out


# ---------- DB helpers / defensive helpers ----------
def safe_commit():
    """
    Commit the current session but rollback and re-raise on failure.
    """
    db = current_app.db
    try:
        db.session.commit()
    except Exception:
        logger.exception("DB commit failed, rolling back")
        try:
            db.session.rollback()
        except Exception:
            logger.exception("Failed to rollback session after commit failure")
        raise


# Keep these sets in sync with DB enums; they're used to avoid accidental inserts
# of missing enum labels and to provide a safe fallback.
ALLOWED_ACTIVITY_ENTITIES = {"contact", "deal", "company", "note", "task"}
ALLOWED_ACTIVITY_TYPES = {
    "note", "note_created", "note_updated", "contact_created",
    "deal_created", "deal_deleted", "deal_updated", "task_created",
    "lead_converted", "stage_change", "deal_closed"
}

# Allowed deal stages - keep these in sync with your DB enum values.
ALLOWED_DEAL_STAGES = {
    "prospect", "discovery", "qualified", "proposal", "negotiation", "won", "lost", "closed"
}


def normalize_activity_entity(entity: str) -> str:
    if not entity:
        return "note"
    ent = str(entity).lower()
    return ent if ent in ALLOWED_ACTIVITY_ENTITIES else "note"


def normalize_activity_type(t: str) -> str:
    if not t:
        return "note"
    tt = str(t).lower()
    return tt if tt in ALLOWED_ACTIVITY_TYPES else "note"


def normalize_deal_stage(s: str) -> str:
    """
    Normalize an incoming stage label to a known allowed label.
    Fallback to 'prospect' if unknown or falsy.
    """
    if not s:
        return "prospect"
    ss = str(s).strip().lower()
    if ss in ALLOWED_DEAL_STAGES:
        return ss
    # some heuristics for common synonyms / prefixes
    if ss.startswith("won"):
        return "won"
    if ss.startswith("lost"):
        return "lost"
    # last resort
    return "prospect"


def _log_activity(entity_type, entity_id, activity_type, title, description=None, workspace_id=None):
    """
    Log an activity row safely. If Activity insert fails due to enum mismatch or
    other DB errors, fallback to creating a Note (if Note model exists), otherwise
    log and continue.
    """
    Activity = _get_activity_model()
    if not Activity:
        logger.debug("Activity model not configured - skipping log")
        return None

    db = current_app.db
    try:
        # Normalize to prevent writing unknown enum values
        entity_type_n = normalize_activity_entity(entity_type)
        activity_type_n = normalize_activity_type(activity_type)

        a_kwargs = {
            "entity_type": entity_type_n,
            "entity_id": entity_id,
            "type": activity_type_n,
            "title": title,
            "description": description,
            "timestamp": datetime.utcnow(),
        }
        if workspace_id and hasattr(Activity, "workspace_id"):
            a_kwargs["workspace_id"] = workspace_id

        a = Activity(**a_kwargs)
        db.session.add(a)
        safe_commit()
        return getattr(a, "id", None)
    except Exception:
        logger.exception("Failed to log activity for deal; attempting fallback to Note")
        # attempt fallback: create a Note record if model exists
        try:
            Note = _get_note_model()
            if Note:
                note_kwargs = {
                    "content": f"[Activity Fallback] {title}: {description or ''}",
                    "created_at": datetime.utcnow(),
                }
                # include workspace_id if present/available
                if workspace_id and hasattr(Note, "workspace_id"):
                    note_kwargs["workspace_id"] = workspace_id
                n = Note(**note_kwargs)
                db.session.add(n)
                safe_commit()
                logger.info("Fallback Note created for failed Activity log")
                return getattr(n, "id", None)
        except Exception:
            logger.exception("Failed to create fallback Note after Activity log failure")
        # nothing else to do
        return None


def _ok_options():
    """Return a minimal valid CORS-friendly OPTIONS response."""
    resp = make_response("", 200)
    resp.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = request.headers.get(
        "Access-Control-Request-Headers", "Authorization,Content-Type,X-User-Id,X-Workspace-ID"
    )
    return resp


def _column_is_integer_type(model, colname):
    """Return True if the given model column is an integer-like SQLAlchemy type."""
    try:
        col = model.__table__.columns.get(colname)
        if col is None:
            return False
        col_type = col.type
        return isinstance(col_type, (Integer, BigInteger))
    except Exception:
        return False


# ---------------- List / Create ----------------
@bp.route("", methods=["OPTIONS", "GET", "POST"], strict_slashes=False)
def deals_handler():
    if request.method == "OPTIONS":
        return _ok_options()

    db = current_app.db
    Deal = _get_deal_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500

    colnames = _model_columns(Deal)

    # LIST
    if request.method == "GET":
        logger.debug("GET /deals called - args=%s headers=%s", dict(request.args), {k: v for k, v in request.headers.items()})
        workspace_id = request.args.get("workspace_id") or request.args.get("ws") or None
        owner_id = request.args.get("owner_id") or request.args.get("user_id") or None
        stage = request.args.get("stage")
        search = request.args.get("search")
        limit = request.args.get("limit", 100)
        try:
            limit = int(limit)
        except Exception:
            limit = 100

        # SECURITY: resolve + verify the caller owns the requested workspace and
        # ALWAYS scope to it. Previously the workspace filter was applied only when
        # workspace_id was present (fail-OPEN -> returned every tenant's deals).
        ws, err = _require_owned_workspace(workspace_id)
        if err:
            return err
        workspace_id = ws.id

        q = db.session.query(Deal)

        if "workspace_id" in colnames:
            # keep workspace_id type as string (most apps store this as string), but let DB decide
            try:
                if _column_is_integer_type(Deal, "workspace_id"):
                    q = q.filter(getattr(Deal, "workspace_id") == int(workspace_id))
                else:
                    q = q.filter(getattr(Deal, "workspace_id") == str(workspace_id))
            except Exception:
                q = q.filter(getattr(Deal, "workspace_id") == workspace_id)

        if owner_id is not None and "owner_id" in colnames:
            # decide casting based on column type to avoid varchar=int or int=varchar errors
            try:
                if _column_is_integer_type(Deal, "owner_id"):
                    o_val = int(owner_id)
                else:
                    o_val = str(owner_id)
            except Exception:
                o_val = str(owner_id)
            q = q.filter(getattr(Deal, "owner_id") == o_val)

        if stage and "stage" in colnames:
            # normalize stage before filtering
            try:
                stage_n = normalize_deal_stage(stage)
                q = q.filter(getattr(Deal, "stage") == stage_n)
            except Exception:
                q = q.filter(getattr(Deal, "stage") == stage)

        if search:
            term = f"%{search.lower()}%"
            try:
                q = q.filter(
                    db.or_(
                        db.func.lower(Deal.name).like(term),
                        db.func.lower(Deal.company).like(term),
                    )
                )
            except Exception:
                try:
                    q = q.filter(
                        db.or_(
                            Deal.name == search,
                            Deal.company == search,
                        )
                    )
                except Exception:
                    pass

        try:
            if "updated_at" in colnames:
                q = q.order_by(getattr(Deal, "updated_at").desc())
            elif "created_at" in colnames:
                q = q.order_by(getattr(Deal, "created_at").desc())
            elif hasattr(Deal, "id"):
                q = q.order_by(getattr(Deal, "id").desc())
        except Exception:
            pass

        try:
            items = q.limit(limit).all()
        except SQLAlchemyError as e:
            logger.exception("deals list DB error")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"error": "DB error", "details": str(e)}), 500

        return jsonify([_serialize_deal(it, allowed_fields=colnames) for it in items])

    # CREATE
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not name:
        return jsonify({"error": "name required"}), 400

    workspace_q = request.args.get("workspace_id")
    ws_val = None
    if "workspace_id" in colnames:
        if not workspace_q:
            return jsonify({"error": "workspace_id query param required"}), 400
        # SECURITY: verify the caller owns the workspace they're writing into.
        ws, err = _require_owned_workspace(workspace_q)
        if err:
            return err
        ws_val = ws.id

    owner_q = request.args.get("owner_id")
    owner_val = payload.get("owner_id") or owner_q or _get_request_user_id()

    create_kwargs = {}
    for k, v in payload.items():
        if k in colnames:
            if k in ("value", "amount", "expected_value"):
                try:
                    create_kwargs[k] = Decimal(str(v)) if v is not None else None
                except Exception:
                    create_kwargs[k] = v
            elif k in ("close_date", "created_at", "updated_at") or k.endswith("_at") or k.endswith("_date"):
                try:
                    create_kwargs[k] = datetime.fromisoformat(v) if v else None
                except Exception:
                    create_kwargs[k] = v
            else:
                create_kwargs[k] = v

    if ws_val is not None and "workspace_id" in colnames:
        # cast workspace id based on column type
        try:
            if _column_is_integer_type(Deal, "workspace_id"):
                create_kwargs["workspace_id"] = int(ws_val)
            else:
                create_kwargs["workspace_id"] = str(ws_val)
        except Exception:
            create_kwargs["workspace_id"] = ws_val

    if owner_val is not None and "owner_id" in colnames:
        # cast owner id based on column type to avoid type mismatch
        try:
            if _column_is_integer_type(Deal, "owner_id"):
                create_kwargs["owner_id"] = int(owner_val)
            else:
                create_kwargs["owner_id"] = str(owner_val)
        except Exception:
            create_kwargs["owner_id"] = owner_val

    # Normalize stage before inserting to avoid unknown enum labels
    if "stage" in colnames:
        incoming_stage = payload.get("stage")
        try:
            create_kwargs["stage"] = normalize_deal_stage(incoming_stage)
        except Exception:
            create_kwargs["stage"] = payload.get("stage") or "prospect"

    if "created_at" in colnames and "created_at" not in create_kwargs:
        create_kwargs["created_at"] = datetime.utcnow()

    try:
        DealCls = Deal
        d = DealCls(**create_kwargs)
        db.session.add(d)
        safe_commit()
        try:
            _log_activity("deal", getattr(d, "id", None), "deal_created", "Deal created", description=f"Deal '{getattr(d, 'name', None)}' created", workspace_id=create_kwargs.get("workspace_id"))
        except Exception:
            logger.exception("Activity logging after create failed (ignored)")
        return jsonify({"id": getattr(d, "id", None)}), 201
    except TypeError as te:
        logger.debug("TypeError creating Deal, trying filtered constructor: %s", te)
        try:
            sig_names = []
            try:
                sig = inspect.signature(Deal)
                sig_names = [p for p in sig.parameters.keys() if p != "self"]
            except Exception:
                sig_names = colnames
            filtered = {k: v for k, v in create_kwargs.items() if k in sig_names}
            d = Deal(**filtered)
            db.session.add(d)
            safe_commit()
            try:
                _log_activity("deal", getattr(d, "id", None), "deal_created", "Deal created", description=f"Deal '{getattr(d, 'name', None)}' created", workspace_id=filtered.get("workspace_id"))
            except Exception:
                logger.exception("Activity logging after create (filtered) failed (ignored)")
            return jsonify({"id": getattr(d, "id", None)}), 201
        except Exception as e:
            logger.exception("create deal failed after filtering")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"error": "DB error", "details": str(e)}), 500
    except SQLAlchemyError as e:
        logger.exception("create_deal DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500


# ---------------- Single Deal operations ----------------
@bp.route("/<deal_id>", methods=["OPTIONS", "GET", "PATCH", "PUT", "DELETE"], strict_slashes=False)
def deal_detail(deal_id):
    if request.method == "OPTIONS":
        return _ok_options()

    db = current_app.db
    Deal = _get_deal_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500

    d = db.session.get(Deal, deal_id)
    if not d:
        return jsonify({"error": "not found"}), 404

    # SECURITY (IDOR): verify the caller owns this deal's workspace before any op.
    auth_err = _authorize_row(d)
    if auth_err:
        return auth_err

    colnames = _model_columns(Deal)

    if request.method == "GET":
        return jsonify(_serialize_deal(d, allowed_fields=colnames))

    if request.method == "DELETE":
        try:
            db.session.delete(d)
            safe_commit()
            try:
                _log_activity("deal", deal_id, "deal_deleted", "Deal deleted", description="Deleted via API", workspace_id=getattr(d, "workspace_id", None))
            except Exception:
                logger.exception("Activity logging on delete failed (ignored)")
            return jsonify({"ok": True})
        except SQLAlchemyError as e:
            logger.exception("delete_deal DB error")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500

    # UPDATE (PATCH/PUT)
    payload = request.get_json(silent=True) or {}
    allowed = set(colnames)
    
    old_stage = getattr(d, "stage", None)
    
    for k, v in payload.items():
        if k not in allowed:
            continue
        if k in ("value", "amount", "expected_value"):
            try:
                setattr(d, k, Decimal(str(v)) if v is not None else None)
            except Exception:
                setattr(d, k, v)
        elif k in ("close_date", "created_at", "updated_at") or k.endswith("_at") or k.endswith("_date"):
            try:
                setattr(d, k, datetime.fromisoformat(v) if v else None)
            except Exception:
                setattr(d, k, v)
        else:
            # normalize stage specifically
            if k == "stage":
                try:
                    setattr(d, k, normalize_deal_stage(v))
                except Exception:
                    setattr(d, k, v)
            else:
                setattr(d, k, v)

    if "updated_at" in colnames:
        try:
            setattr(d, "updated_at", datetime.utcnow())
        except Exception:
            pass

    try:
        db.session.add(d)
        safe_commit()
        
        # Trigger CAPI Purchase Event if stage changed to won
        if old_stage != "won" and getattr(d, "stage", None) == "won":
            try:
                contact_email = getattr(d, "contact_email", None)
                contact_phone = getattr(d, "contact_phone", None)
                contact_name = getattr(d, "contact_name", None) or getattr(d, "name", None)
                
                if not contact_email and getattr(d, "contact_id", None):
                    Contact = current_app.crm_models.get("Contact")
                    if Contact:
                        c = db.session.get(Contact, d.contact_id)
                        if c:
                            contact_email = contact_email or getattr(c, "email", None)
                            contact_phone = contact_phone or getattr(c, "phone", None)
                            contact_name = getattr(c, "name", getattr(d, "name", None))

                user_data = {
                    "email": contact_email,
                    "phone": contact_phone,
                    "name": contact_name,
                    "client_ip_address": request.remote_addr,
                    "client_user_agent": request.headers.get("User-Agent")
                }
                
                custom_data = {
                    "value": float(getattr(d, "value", 0) or 0),
                    "currency": getattr(d, "currency", "USD")
                }
                
                from SocioviaCrm.capi_service import send_capi_event
                send_capi_event("Purchase", user_data, custom_data, getattr(d, "workspace_id", None), "system_generated")
            except Exception as e:
                logger.error(f"Failed to trigger CAPI Purchase event from deal update: {e}")
                
        try:
            _log_activity("deal", deal_id, "deal_updated", "Deal updated", description="Updated via API", workspace_id=getattr(d, "workspace_id", None))
        except Exception:
            logger.exception("Activity logging on update failed (ignored)")
        return jsonify({"ok": True, "deal": _serialize_deal(d, allowed_fields=colnames)})
    except SQLAlchemyError as e:
        logger.exception("update_deal DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# ---------------- Change stage / close ----------------
@bp.route("/<deal_id>/stage", methods=["OPTIONS", "POST"], strict_slashes=False)
def change_stage(deal_id):
    if request.method == "OPTIONS":
        return _ok_options()
    db = current_app.db
    Deal = _get_deal_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500
    d = db.session.get(Deal, deal_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    # SECURITY (IDOR): verify workspace ownership before mutating the deal.
    auth_err = _authorize_row(d)
    if auth_err:
        return auth_err
    payload = request.get_json(silent=True) or {}
    stage = payload.get("stage")
    note = payload.get("note")
    
    old_stage = getattr(d, "stage", None)
    
    if not stage:
        return jsonify({"error": "stage required"}), 400
    if "stage" in _model_columns(Deal):
        try:
            setattr(d, "stage", normalize_deal_stage(stage))
        except Exception:
            setattr(d, "stage", stage)
    if "updated_at" in _model_columns(Deal):
        setattr(d, "updated_at", datetime.utcnow())
    try:
        db.session.add(d)
        safe_commit()
        
        # Trigger CAPI Purchase Event if stage changed to won
        if old_stage != "won" and normalize_deal_stage(stage) == "won":
            try:
                contact_email = getattr(d, "contact_email", None)
                contact_phone = getattr(d, "contact_phone", None)
                contact_name = getattr(d, "contact_name", None) or getattr(d, "name", None)
                
                if not contact_email and getattr(d, "contact_id", None):
                    Contact = current_app.crm_models.get("Contact")
                    if Contact:
                        c = db.session.get(Contact, d.contact_id)
                        if c:
                            contact_email = contact_email or getattr(c, "email", None)
                            contact_phone = contact_phone or getattr(c, "phone", None)
                            contact_name = getattr(c, "name", getattr(d, "name", None))

                user_data = {
                    "email": contact_email,
                    "phone": contact_phone,
                    "name": contact_name,
                    "client_ip_address": request.remote_addr,
                    "client_user_agent": request.headers.get("User-Agent")
                }
                
                custom_data = {
                    "value": float(getattr(d, "value", 0) or 0),
                    "currency": getattr(d, "currency", "USD")
                }
                
                from SocioviaCrm.capi_service import send_capi_event
                send_capi_event("Purchase", user_data, custom_data, getattr(d, "workspace_id", None), "system_generated")
            except Exception as e:
                logger.error(f"Failed to trigger CAPI Purchase event from change_stage: {e}")
                
        try:
            _log_activity("deal", deal_id, "stage_change", f"Stage -> {stage}", description=note, workspace_id=getattr(d, "workspace_id", None))
        except Exception:
            logger.exception("Activity logging for stage change failed (ignored)")
        return jsonify({"ok": True})
    except SQLAlchemyError as e:
        logger.exception("change_stage DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


@bp.route("/<deal_id>/close", methods=["OPTIONS", "POST"], strict_slashes=False)
def close_deal(deal_id):
    if request.method == "OPTIONS":
        return _ok_options()
    db = current_app.db
    Deal = _get_deal_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500
    d = db.session.get(Deal, deal_id)
    if not d:
        return jsonify({"error": "not found"}), 404
    # SECURITY (IDOR): verify workspace ownership before closing the deal.
    auth_err = _authorize_row(d)
    if auth_err:
        return auth_err
    payload = request.get_json(silent=True) or {}
    status = (payload.get("status") or "").lower()
    closed_reason = payload.get("closed_reason")
    closed_at = payload.get("closed_at")
    if status not in ("won", "lost"):
        return jsonify({"error": "status must be 'won' or 'lost'"}), 400
    if "status" in _model_columns(Deal):
        setattr(d, "status", status)
    if "closed_reason" in _model_columns(Deal):
        setattr(d, "closed_reason", closed_reason)
    if "closed_at" in _model_columns(Deal):
        try:
            setattr(d, "closed_at", datetime.fromisoformat(closed_at) if closed_at else datetime.utcnow())
        except Exception:
            setattr(d, "closed_at", datetime.utcnow())
    if "updated_at" in _model_columns(Deal):
        setattr(d, "updated_at", datetime.utcnow())
    try:
        db.session.add(d)
        safe_commit()
        try:
            _log_activity("deal", deal_id, "deal_closed", f"Deal {status.upper()}", description=closed_reason, workspace_id=getattr(d, "workspace_id", None))
        except Exception:
            logger.exception("Activity logging for close_deal failed (ignored)")
        return jsonify({"ok": True})
    except SQLAlchemyError as e:
        logger.exception("close_deal DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# ---------------- Deal Activity ----------------
@bp.route("/<deal_id>/activity", methods=["OPTIONS", "GET", "POST"], strict_slashes=False)
def deal_activity(deal_id):
    if request.method == "OPTIONS":
        return _ok_options()
    db = current_app.db
    Deal = _get_deal_model()
    Activity = _get_activity_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500

    # SECURITY (IDOR): the activity of a deal is only visible/writable to a caller
    # who owns the deal's workspace. Load + authorize before exposing anything.
    d = db.session.get(Deal, deal_id)
    if not d:
        return jsonify({"error": "deal not found"}), 404
    auth_err = _authorize_row(d)
    if auth_err:
        return auth_err

    if request.method == "GET":
        if not Activity:
            return jsonify([])
        acts = (
            db.session.query(Activity)
            .filter(Activity.entity_type == "deal", Activity.entity_id == deal_id)
            .order_by(Activity.timestamp.desc())
            .all()
        )
        out = []
        for a in acts:
            out.append({
                "id": getattr(a, "id", None),
                "title": getattr(a, "title", None),
                "timestamp": getattr(a, "timestamp", None).isoformat() if getattr(a, "timestamp", None) else None,
                "type": getattr(a, "type", None),
                "description": getattr(a, "description", None),
            })
        return jsonify(out)

    # POST -> add activity  (deal already loaded + ownership verified above)
    if not Activity:
        return jsonify({"error": "Activity model not configured"}), 500

    payload = request.get_json(silent=True) or {}
    try:
        ts_raw = payload.get("timestamp")
        try:
            ts = datetime.fromisoformat(ts_raw) if ts_raw else datetime.utcnow()
        except Exception:
            ts = datetime.utcnow()

        # Normalize incoming activity type & be defensive
        incoming_type = payload.get("type") or payload.get("activity_type") or "note"
        activity_type_normalized = normalize_activity_type(incoming_type)

        a_kwargs = {
            "entity_type": "deal",
            "entity_id": deal_id,
            "type": activity_type_normalized,
            "title": payload.get("title") or (incoming_type or "Activity"),
            "description": payload.get("description"),
            "timestamp": ts,
        }
        if hasattr(Activity, "workspace_id"):
            ws = request.args.get("workspace_id") or getattr(d, "workspace_id", None)
            if ws is not None:
                # cast workspace id based on column type
                try:
                    if _column_is_integer_type(Activity, "workspace_id"):
                        a_kwargs["workspace_id"] = int(ws)
                    else:
                        a_kwargs["workspace_id"] = str(ws)
                except Exception:
                    a_kwargs["workspace_id"] = ws
        a = Activity(**a_kwargs)
        db.session.add(a)
        safe_commit()
        return jsonify({"id": getattr(a, "id", None)}), 201
    except Exception:
        logger.exception("add_deal_activity failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error"}), 500


# ---------------- Convert lead -> deal helper ----------------
@bp.route("/convert-from-lead", methods=["OPTIONS", "POST"], strict_slashes=False)
def convert_from_lead():
    if request.method == "OPTIONS":
        return _ok_options()
    db = current_app.db
    Deal = _get_deal_model()
    Lead = _get_lead_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500
    data = request.get_json(silent=True) or {}
    lead_id = data.get("lead_id")
    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400

    lead_obj = None
    if Lead:
        lead_obj = db.session.get(Lead, lead_id)

    # SECURITY (IDOR): a lead may only be converted by a caller who owns the
    # lead's workspace — otherwise one tenant could spin the deal (and copy the
    # source workspace_id) off another tenant's lead.
    if lead_obj is not None:
        auth_err = _authorize_row(lead_obj)
        if auth_err:
            return auth_err
    else:
        # No source lead: the deal is created in a client-supplied workspace, so
        # verify ownership of it up front and use the resolved id.
        req_ws = request.args.get("workspace_id") or (data.get("workspace_id"))
        ws, err = _require_owned_workspace(req_ws)
        if err:
            return err
        data = dict(data)
        data["workspace_id"] = ws.id

    create_kwargs = {}
    if lead_obj:
        create_kwargs["name"] = data.get("name") or f"Deal from {getattr(lead_obj, 'name', 'lead')}"
        if "company" in _model_columns(Deal) and hasattr(lead_obj, "company"):
            create_kwargs["company"] = getattr(lead_obj, "company", None)
        if "contact_email" in _model_columns(Deal) and hasattr(lead_obj, "email"):
            create_kwargs["contact_email"] = getattr(lead_obj, "email", None)
        if "source" in _model_columns(Deal):
            create_kwargs["source"] = getattr(lead_obj, "source", None) or "lead_conversion"
        if "workspace_id" in _model_columns(Deal) and hasattr(lead_obj, "workspace_id"):
            create_kwargs["workspace_id"] = getattr(lead_obj, "workspace_id", None)
    else:
        create_kwargs["name"] = data.get("name") or "Deal from lead"
        # workspace_id was resolved + ownership-verified above for the no-lead case
        if "workspace_id" in _model_columns(Deal) and data.get("workspace_id") is not None:
            create_kwargs["workspace_id"] = data.get("workspace_id")

    if "owner_id" in _model_columns(Deal) and data.get("owner_id"):
        # cast based on column
        try:
            if _column_is_integer_type(Deal, "owner_id"):
                create_kwargs["owner_id"] = int(data.get("owner_id"))
            else:
                create_kwargs["owner_id"] = str(data.get("owner_id"))
        except Exception:
            create_kwargs["owner_id"] = data.get("owner_id")
    elif _get_request_user_id() and "owner_id" in _model_columns(Deal):
        try:
            uid = _get_request_user_id()
            if _column_is_integer_type(Deal, "owner_id"):
                create_kwargs["owner_id"] = int(uid)
            else:
                create_kwargs["owner_id"] = str(uid)
        except Exception:
            create_kwargs["owner_id"] = _get_request_user_id()

    # normalize incoming stage for safety
    if "stage" in _model_columns(Deal):
        try:
            create_kwargs["stage"] = normalize_deal_stage(data.get("stage"))
        except Exception:
            create_kwargs["stage"] = data.get("stage") or "prospect"

    if "value" in _model_columns(Deal) and data.get("value") is not None:
        try:
            create_kwargs["value"] = Decimal(str(data.get("value")))
        except Exception:
            create_kwargs["value"] = data.get("value")

    if "created_at" in _model_columns(Deal):
        create_kwargs["created_at"] = datetime.utcnow()

    try:
        d = Deal(**create_kwargs)
        db.session.add(d)
        safe_commit()
        try:
            _log_activity("deal", getattr(d, "id", None), "deal_created", "Deal created from lead", description=f"Converted from lead {lead_id}", workspace_id=create_kwargs.get("workspace_id"))
        except Exception:
            logger.exception("Activity logging for convert_from_lead failed (ignored)")
        if lead_obj and _get_activity_model():
            try:
                _log_activity("lead", lead_id, "lead_converted", "Lead converted to deal", description=f"Created deal {getattr(d, 'id', None)}", workspace_id=getattr(lead_obj, "workspace_id", None))
            except Exception:
                logger.exception("Activity logging (lead converted) failed (ignored)")
        return jsonify({"id": getattr(d, "id", None)}), 201
    except Exception as e:
        logger.exception("convert_from_lead failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500


# ---------------- Search ----------------
@bp.route("/search", methods=["OPTIONS", "GET"], strict_slashes=False)
def search_deals():
    if request.method == "OPTIONS":
        return _ok_options()
    q = request.args.get("q", "").strip()
    workspace_id = request.args.get("workspace_id")
    limit = int(request.args.get("limit", 20))
    db = current_app.db
    Deal = _get_deal_model()
    if not Deal:
        return jsonify({"error": "Deal model not configured"}), 500

    # SECURITY: resolve + verify the caller owns the workspace and ALWAYS scope
    # the search to it (was fail-OPEN: unscoped search across all tenants).
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    if not q:
        return jsonify({"data": []})
    colnames = _model_columns(Deal)
    filters = []
    if "name" in colnames:
        try:
            filters.append(Deal.name.ilike(f"%{q}%"))
        except Exception:
            filters.append(Deal.name == q)
    if "company" in colnames:
        try:
            filters.append(Deal.company.ilike(f"%{q}%"))
        except Exception:
            filters.append(Deal.company == q)
    if not filters:
        return jsonify({"data": []})
    query = db.session.query(Deal).filter(db.or_(*filters))
    if workspace_id and "workspace_id" in colnames:
        # cast workspace id to column type
        try:
            if _column_is_integer_type(Deal, "workspace_id"):
                query = query.filter(getattr(Deal, "workspace_id") == int(workspace_id))
            else:
                query = query.filter(getattr(Deal, "workspace_id") == str(workspace_id))
        except Exception:
            query = query.filter(getattr(Deal, "workspace_id") == workspace_id)
    try:
        results = query.limit(limit).all()
    except SQLAlchemyError as e:
        logger.exception("search_deals DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500
    return jsonify({"data": [_serialize_deal(r, allowed_fields=colnames) for r in results]})
