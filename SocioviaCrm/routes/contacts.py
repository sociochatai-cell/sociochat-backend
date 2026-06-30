import inspect
from datetime import datetime
from flask import Blueprint, jsonify, current_app, request, session
from sqlalchemy import or_, desc, asc, cast
from sqlalchemy.types import String, Integer
from sqlalchemy.exc import SQLAlchemyError

bp = Blueprint("contacts", __name__, url_prefix="/contacts")


def _get_contact_model():
    try:
        return current_app.crm_models.get("Contact")
    except Exception:
        return None


def _get_activity_model():
    try:
        return current_app.crm_models.get("Activity")
    except Exception:
        return None


def _get_workspace_from_request():
    """Resolve the active workspace id from query param or X-Workspace-ID header.
    Returns the raw string value or None. Ownership is validated by the global
    before_request hook when this value is present."""
    ws = request.args.get("workspace_id") or request.headers.get("X-Workspace-ID")
    return str(ws) if ws else None


def _require_workspace_id():
    """Fail-closed resolver. Returns (wid:int, None) on success, or
    (None, (response, status)) on failure so callers can `return err`."""
    raw = _get_workspace_from_request()
    if not raw:
        return None, (jsonify({"success": False, "error": "workspace_required"}), 400)
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, (jsonify({"success": False, "error": "invalid_workspace_id"}), 400)


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


def _serialize_contact(c, allowed_fields=None):
    if not allowed_fields:
        allowed_fields = getattr(c.__class__, "__table__", None)
        if allowed_fields:
            allowed_fields = list(c.__class__.__table__.columns.keys())
        else:
            allowed_fields = []
    out = {}
    for k in allowed_fields:
        val = getattr(c, k, None)
        if isinstance(val, datetime):
            out[k] = val.isoformat()
        else:
            out[k] = val
    # always include id if present
    if hasattr(c, "id"):
        out.setdefault("id", getattr(c, "id"))
    # include sync fields if present on model
    if hasattr(c, "external_source") and "external_source" not in out:
        out["external_source"] = getattr(c, "external_source", None)
    if hasattr(c, "external_id") and "external_id" not in out:
        out["external_id"] = getattr(c, "external_id", None)
    if hasattr(c, "sync_status") and "sync_status" not in out:
        out["sync_status"] = getattr(c, "sync_status", None)
    if hasattr(c, "last_sync_at") and "last_sync_at" not in out:
        out["last_sync_at"] = getattr(c, "last_sync_at").isoformat() if getattr(c, "last_sync_at", None) else None
    return out


def _model_columns(model):
    """Return list of column names for this ORM model (or empty list)."""
    try:
        return list(model.__table__.columns.keys())
    except Exception:
        return []


def _col_compare_expr(col_obj, raw_val):
    """
    Build a SQLAlchemy binary expression that compares column to raw_val
    while being robust to column type mismatches.
    """
    # If value is numeric-string, try int first
    try_int = None
    try:
        try_int = int(raw_val)
    except Exception:
        try_int = None

    # If column is integer-like, compare as integer
    try:
        col_type = getattr(col_obj, "type", None)
        # SQLAlchemy types.Integer check
        if col_type is not None and isinstance(col_type, Integer):
            if try_int is not None:
                return col_obj == try_int
            else:
                # fallback: cast col to text and compare string
                return cast(col_obj, String) == str(raw_val)
    except Exception:
        pass

    # For text/varchar columns: cast column to TEXT/STRING and compare string
    try:
        return cast(col_obj, String) == str(raw_val)
    except Exception:
        # last resort
        return col_obj == raw_val


# ---------------- List / filter / paginate ----------------
@bp.route("", methods=["GET"])
def list_contacts():
    """
    GET /contacts
    Optional query params:
      - workspace_id, user_id, email, name (only applied if Contact has those columns)
      - page, per_page, sort_by, sort_dir
    """
    db = current_app.db
    Contact = _get_contact_model()
    if not Contact:
        return jsonify({"error": "Contact model not configured"}), 500

    columns = _model_columns(Contact)

    # SECURITY: workspace_id is REQUIRED to prevent cross-workspace data leakage.
    wid, err = _require_workspace_id()
    if err:
        return err

    page = int(request.args.get("page", 1))
    per_page = int(request.args.get("per_page", 25))
    sort_by = request.args.get("sort_by", None)
    sort_dir = request.args.get("sort_dir", "asc").lower()

    q = db.session.query(Contact)

    # Always scope by the active workspace.
    if "workspace_id" in columns:
        q = q.filter(Contact.workspace_id == wid)

    # filter by any supported queryable fields (only apply if model has them)
    for param_name in ("user_id", "email", "name", "company", "phone", "external_source", "external_id"):
        val = request.args.get(param_name)
        if val and param_name in columns:
            col_obj = getattr(Contact, param_name)
            if param_name.endswith("_id") or param_name in ("workspace_id", "user_id"):
                # Use type-aware comparator to avoid postgres text/int errors
                try:
                    q = q.filter(_col_compare_expr(col_obj, val))
                except SQLAlchemyError as e:
                    current_app.logger.debug("filter by %s failed, trying fallback: %s", param_name, e, exc_info=True)
                    try:
                        q = q.filter(col_obj == val)
                    except Exception:
                        pass
            elif param_name in ("email", "name", "company", "phone"):
                # do case-insensitive partial match where possible
                try:
                    q = q.filter(col_obj.ilike(f"%{val}%"))
                except Exception:
                    q = q.filter(col_obj == val)
            else:
                # exact match for external_source/external_id
                try:
                    q = q.filter(col_obj == val)
                except Exception:
                    q = q.filter(cast(col_obj, String) == str(val))

    # Sorting: only if sort_by exists on model
    if sort_by and sort_by in columns:
        sort_col = getattr(Contact, sort_by)
        q = q.order_by(asc(sort_col) if sort_dir == "asc" else desc(sort_col))
    else:
        # fallback ordering if model has name / created_at / id
        if "name" in columns:
            q = q.order_by(asc(getattr(Contact, "name")))
        elif "created_at" in columns:
            q = q.order_by(desc(getattr(Contact, "created_at")))
        elif hasattr(Contact, "id"):
            q = q.order_by(desc(getattr(Contact, "id")))

    try:
        total = q.count()
        items = q.offset((page - 1) * per_page).limit(per_page).all()
    except Exception as e:
        current_app.logger.exception("list_contacts DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500

    # choose columns to serialize
    colnames = _model_columns(Contact)
    # ensure common friendly names present in output
    if "id" not in colnames and hasattr(Contact, "id"):
        colnames.append("id")

    return jsonify({
        "meta": {"page": page, "per_page": per_page, "total": total},
        "data": [_serialize_contact(c, allowed_fields=colnames) for c in items]
    })


# ---------------- Get single contact ----------------
@bp.route("/<contact_id>", methods=["GET"])
def get_contact(contact_id):
    db = current_app.db
    Contact = _get_contact_model()
    if not Contact:
        return jsonify({"error": "Contact model not configured"}), 500

    wid, err = _require_workspace_id()
    if err:
        return err

    # contact_id is a string (UUID or text); scope the lookup to the active workspace.
    c = db.session.query(Contact).filter_by(id=contact_id, workspace_id=wid).one_or_none()
    if not c:
        return jsonify({"error": "not found"}), 404

    return jsonify(_serialize_contact(c, allowed_fields=_model_columns(Contact)))


# ---------------- Create contact ----------------
@bp.route("", methods=["POST"])
def create_contact():
    """
    POST /contacts?workspace_id=...&user_id=...
    JSON body: only fields present on the Contact model will be used.
    If payload includes external_source+external_id, function attempts dedupe/upsert first.
    """
    db = current_app.db
    Contact = _get_contact_model()
    Activity = _get_activity_model()
    body = request.get_json(silent=True) or {}

    if not Contact:
        return jsonify({"ok": False, "error": "Contact model not configured"}), 500

    ws_raw = request.args.get("workspace_id")
    if not ws_raw and "workspace_id" not in _model_columns(Contact):
        # If model doesn't have workspace_id, we won't require it
        ws_val = None
    else:
        if not ws_raw:
            return jsonify({"ok": False, "error": "workspace_id query param required"}), 400
        ws_val = ws_raw

    if not request.args.get("user_id") and "user_id" not in _model_columns(Contact):
        user_val = None
    else:
        user_raw = request.args.get("user_id")
        if not user_raw:
            # fallback to session/header
            fallback = _get_request_user_id()
            if fallback is None:
                return jsonify({"ok": False, "error": "user_id query param required or session/header present"}), 400
            user_val = fallback
        else:
            user_val = user_raw

    colnames = _model_columns(Contact)

    # external dedupe attempt
    external_source = body.get("external_source")
    external_id = body.get("external_id")

    existing = None
    if external_source and external_id and hasattr(Contact, "external_source") and hasattr(Contact, "external_id"):
        try:
            q = db.session.query(Contact)
            if ws_val is not None and "workspace_id" in colnames:
                q = q.filter(getattr(Contact, "workspace_id") == ws_val)
            existing = q.filter(Contact.external_source == external_source, Contact.external_id == str(external_id)).one_or_none()
        except Exception:
            existing = None

    # fallback dedupe by email/phone
    if existing is None and ("email" in colnames) and (body.get("email") or body.get("phone")):
        try:
            q2 = db.session.query(Contact)
            if ws_val is not None and "workspace_id" in colnames:
                q2 = q2.filter(getattr(Contact, "workspace_id") == ws_val)
            if body.get("email"):
                q2 = q2.filter(getattr(Contact, "email") == body.get("email"))
            if body.get("phone"):
                q2 = q2.filter(getattr(Contact, "phone") == body.get("phone"))
            existing = q2.first()
        except Exception:
            existing = None

    if existing:
        # update existing minimal fields
        changed = False
        for k in ("name", "email", "phone", "company", "role", "notes", "avatar"):
            if k in body and getattr(existing, k, None) != body.get(k):
                setattr(existing, k, body.get(k))
                changed = True
        if external_source and hasattr(existing, "external_source") and not getattr(existing, "external_source", None):
            existing.external_source = external_source
            changed = True
        if external_id and hasattr(existing, "external_id") and not getattr(existing, "external_id", None):
            existing.external_id = str(external_id)
            changed = True
        if changed:
            try:
                if hasattr(existing, "last_sync_at") and external_source:
                    existing.last_sync_at = datetime.utcnow()
                if hasattr(existing, "sync_status") and external_source:
                    existing.sync_status = "in_sync"
                if hasattr(existing, "updated_at"):
                    existing.updated_at = datetime.utcnow()
                db.session.add(existing)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        return jsonify({"ok": True, "id": getattr(existing, "id", None), "deduped": True}), 200

    # create new contact
    create_kwargs = {}
    for k, v in (body or {}).items():
        if k in colnames:
            # special parse for datetime like last_contacted / created_at
            if k.endswith("date") or "date" in k or k.endswith("at") or k.endswith("time"):
                try:
                    create_kwargs[k] = datetime.fromisoformat(v) if v else None
                except Exception:
                    create_kwargs[k] = v
            else:
                create_kwargs[k] = v

    # attempt to attach workspace_id / user_id if available
    if ws_val is not None and "workspace_id" in colnames:
        create_kwargs["workspace_id"] = ws_val
    if user_val is not None and "user_id" in colnames:
        create_kwargs["user_id"] = user_val

    # ensure at least one identifying field is present (safe-guard)
    if not any(k in create_kwargs for k in ("name", "email", "phone", "external_id")):
        if not set(create_kwargs):
            return jsonify({"ok": False, "error": "No data to create"}), 400

    # set external metadata and sync fields if provided
    if external_source and "external_source" in colnames:
        create_kwargs["external_source"] = external_source
    if external_id and "external_id" in colnames:
        create_kwargs["external_id"] = str(external_id)
    if "sync_status" in colnames and external_source:
        create_kwargs["sync_status"] = "in_sync"
    if "last_sync_at" in colnames and external_source:
        create_kwargs["last_sync_at"] = datetime.utcnow()
    if "created_at" in colnames and "created_at" not in create_kwargs:
        create_kwargs["created_at"] = datetime.utcnow()
    if "updated_at" in colnames:
        create_kwargs["updated_at"] = datetime.utcnow()

    try:
        # build instance using only allowed kwargs
        c = Contact(**create_kwargs)
        db.session.add(c)
        db.session.commit()
        # Activity log
        try:
            if Activity:
                a_kwargs = {
                    "entity_type": "contact",
                    "entity_id": getattr(c, "id", None),
                    "type": "note_created",
                    "title": "Contact created",
                    "description": f"Created via API payload",
                    "timestamp": datetime.utcnow(),
                }
                if "workspace_id" in colnames:
                    a_kwargs["workspace_id"] = create_kwargs.get("workspace_id")
                a = Activity(**a_kwargs)
                db.session.add(a)
                db.session.commit()
        except Exception:
            current_app.logger.exception("create_contact: failed to create activity")
            try:
                db.session.rollback()
            except Exception:
                pass

        # ✅ Drip Campaign Auto-Enrollment Hook
        try:
            from whatsapp.drip_routes import enroll_from_crm
            contact_data = {
                'name': getattr(c, 'name', None),
                'first_name': getattr(c, 'name', '').split()[0] if getattr(c, 'name', '') else None,
                'last_name': ' '.join(getattr(c, 'name', '').split()[1:]) if getattr(c, 'name', '') else None,
                'email': getattr(c, 'email', None),
                'phone': getattr(c, 'phone', None),
                'company': getattr(c, 'company', None),
                'role': getattr(c, 'role', None),
                'tags': str(getattr(c, 'tags', None)) if getattr(c, 'tags', None) else None,
            }
            ws_id = int(create_kwargs.get('workspace_id')) if create_kwargs.get('workspace_id') else None
            if ws_id:
                result = enroll_from_crm('contact', contact_data, ws_id)
                if result.get('enrolled_campaigns', 0) > 0:
                    current_app.logger.info(f"[CRM Drip] Contact {c.id} enrolled in {result['enrolled_campaigns']} drip campaigns")
        except Exception as drip_err:
            current_app.logger.warning(f"[CRM Drip] Failed to process drip enrollment for contact {c.id}: {drip_err}")

        return jsonify({"ok": True, "id": getattr(c, "id", None), "contact": _serialize_contact(c, allowed_fields=colnames)}), 201
    except TypeError as te:
        # constructor didn't accept some kwargs, try to filter by model constructor signature
        current_app.logger.debug("TypeError on Contact(**kwargs), trying filtered constructor: %s", te)
        try:
            sig_names = []
            try:
                sig = inspect.signature(Contact)
                sig_names = [p for p in sig.parameters.keys() if p != "self"]
            except Exception:
                sig_names = colnames
            filtered = {k: v for k, v in create_kwargs.items() if k in sig_names}
            c = Contact(**filtered)
            db.session.add(c)
            db.session.commit()
            return jsonify({"ok": True, "id": getattr(c, "id", None), "contact": _serialize_contact(c, allowed_fields=colnames)}), 201
        except Exception as e:
            current_app.logger.exception("create_contact failed after filtering")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500
    except Exception as e:
        current_app.logger.exception("create_contact failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# ---------------- Upsert contact (explicit endpoint for integrations) ----------------
@bp.route("/upsert", methods=["POST"])
def upsert_contact():
    """
    POST /contacts/upsert
    Body JSON: { external_source, external_id, workspace_id (optional), ...contact fields... }
    Returns existing or created contact id and whether created.
    """
    db = current_app.db
    Contact = _get_contact_model()
    Activity = _get_activity_model()
    payload = request.get_json(silent=True) or {}

    if not Contact:
        return jsonify({"ok": False, "error": "Contact model not configured"}), 500

    workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
    external_source = payload.get("external_source")
    external_id = payload.get("external_id")
    email = payload.get("email")
    phone = payload.get("phone")

    colnames = _model_columns(Contact)

    existing = None
    if external_source and external_id and "external_source" in colnames and "external_id" in colnames:
        try:
            q = db.session.query(Contact)
            if workspace_id and "workspace_id" in colnames:
                q = q.filter(getattr(Contact, "workspace_id") == workspace_id)
            existing = q.filter(Contact.external_source == external_source, Contact.external_id == str(external_id)).one_or_none()
        except Exception:
            existing = None

    if existing is None and (email or phone):
        try:
            q2 = db.session.query(Contact)
            if workspace_id and "workspace_id" in colnames:
                q2 = q2.filter(getattr(Contact, "workspace_id") == workspace_id)
            if email and "email" in colnames:
                existing = q2.filter(Contact.email == email).first()
            if not existing and phone and "phone" in colnames:
                existing = q2.filter(Contact.phone == phone).first()
        except Exception:
            existing = None

    created = False
    if existing:
        # update fields
        changed = False
        for k in ("name", "email", "phone", "company", "role", "notes", "avatar"):
            if k in payload and getattr(existing, k, None) != payload.get(k):
                setattr(existing, k, payload.get(k))
                changed = True
        if external_source and "external_source" in colnames and not getattr(existing, "external_source", None):
            existing.external_source = external_source
            changed = True
        if external_id and "external_id" in colnames and not getattr(existing, "external_id", None):
            existing.external_id = str(external_id)
            changed = True
        if changed:
            try:
                if "last_sync_at" in colnames:
                    existing.last_sync_at = datetime.utcnow()
                if "sync_status" in colnames:
                    existing.sync_status = "in_sync"
                if "updated_at" in colnames:
                    existing.updated_at = datetime.utcnow()
                db.session.add(existing)
                db.session.commit()
            except Exception:
                try:
                    db.session.rollback()
                except Exception:
                    pass
        contact_id = getattr(existing, "id", None)
    else:
        # create new
        create_kwargs = {}
        for k, v in payload.items():
            if k in colnames:
                if k.endswith("date") or "date" in k or k.endswith("at") or k.endswith("time"):
                    try:
                        create_kwargs[k] = datetime.fromisoformat(v) if v else None
                    except Exception:
                        create_kwargs[k] = v
                else:
                    create_kwargs[k] = v
        if workspace_id and "workspace_id" in colnames:
            create_kwargs["workspace_id"] = workspace_id
        if external_source and "external_source" in colnames:
            create_kwargs["external_source"] = external_source
        if external_id and "external_id" in colnames:
            create_kwargs["external_id"] = str(external_id)
        if "sync_status" in colnames:
            create_kwargs["sync_status"] = "in_sync"
        if "last_sync_at" in colnames:
            create_kwargs["last_sync_at"] = datetime.utcnow()
        if "created_at" in colnames:
            create_kwargs["created_at"] = datetime.utcnow()
        if "updated_at" in colnames:
            create_kwargs["updated_at"] = datetime.utcnow()
        try:
            c = Contact(**create_kwargs)
            db.session.add(c)
            db.session.commit()
            created = True
            contact_id = getattr(c, "id", None)
            # log activity
            try:
                if Activity:
                    a_kwargs = {
                        "entity_type": "contact",
                        "entity_id": contact_id,
                        "type": "note_created",
                        "title": "Contact created (upsert)",
                        "description": "Created via upsert",
                        "timestamp": datetime.utcnow(),
                    }
                    if "workspace_id" in colnames:
                        a_kwargs["workspace_id"] = create_kwargs.get("workspace_id")
                    a = Activity(**a_kwargs)
                    db.session.add(a)
                    db.session.commit()
            except Exception:
                current_app.logger.exception("upsert_contact: failed to create activity")
                try:
                    db.session.rollback()
                except Exception:
                    pass
        except Exception:
            current_app.logger.exception("upsert_contact create failed")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"ok": False, "error": "DB error"}), 500

    return jsonify({"ok": True, "id": contact_id, "created": created}), (201 if created else 200)


# ---------------- Update contact ----------------
@bp.route("/<contact_id>", methods=["PUT", "PATCH"])
def update_contact(contact_id):
    db = current_app.db
    Contact = _get_contact_model()
    if not Contact:
        return jsonify({"error": "Contact model not configured"}), 500

    wid, err = _require_workspace_id()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    c = db.session.query(Contact).filter_by(id=contact_id, workspace_id=wid).one_or_none()
    if not c:
        return jsonify({"error": "not found"}), 404

    colnames = _model_columns(Contact)
    for k, v in body.items():
        if k in colnames:
            if k.endswith("date") or "date" in k or k.endswith("at") or k.endswith("time"):
                try:
                    setattr(c, k, datetime.fromisoformat(v) if v else None)
                except Exception:
                    setattr(c, k, v)
            else:
                setattr(c, k, v)

    # update updated_at if present
    if "updated_at" in colnames:
        try:
            setattr(c, "updated_at", datetime.utcnow())
        except Exception:
            pass

    try:
        db.session.add(c)
        db.session.commit()
        return jsonify({"ok": True, "contact": _serialize_contact(c, allowed_fields=colnames)})
    except Exception as e:
        current_app.logger.exception("update_contact failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# ---------------- Delete contact ----------------
@bp.route("/<contact_id>", methods=["DELETE"])
def delete_contact(contact_id):
    db = current_app.db
    Contact = _get_contact_model()
    if not Contact:
        return jsonify({"error": "Contact model not configured"}), 500

    wid, err = _require_workspace_id()
    if err:
        return err

    c = db.session.query(Contact).filter_by(id=contact_id, workspace_id=wid).one_or_none()
    if not c:
        return jsonify({"error": "not found"}), 404
    try:
        db.session.delete(c)
        db.session.commit()
        return jsonify({"ok": True})
    except Exception as e:
        current_app.logger.exception("delete_contact failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# ---------------- Contact history (GET) ----------------
@bp.route("/<contact_id>/history", methods=["GET"])
def contact_history(contact_id):
    db = current_app.db
    Activity = _get_activity_model()
    Contact = _get_contact_model()
    if not Activity:
        return jsonify([])

    wid, err = _require_workspace_id()
    if err:
        return err

    # Verify the contact belongs to the active workspace before exposing history.
    if Contact:
        contact_obj = db.session.query(Contact).filter_by(id=contact_id, workspace_id=wid).one_or_none()
        if not contact_obj:
            return jsonify({"error": "contact not found"}), 404

    aq = (
        db.session.query(Activity)
        .filter(Activity.entity_type == "contact", Activity.entity_id == contact_id)
    )
    if hasattr(Activity, "workspace_id"):
        aq = aq.filter(Activity.workspace_id == wid)
    acts = aq.order_by(Activity.timestamp.desc()).all()
    out = []
    for a in acts:
        out.append({
            "id": a.id,
            "title": a.title,
            "timestamp": a.timestamp.isoformat() if getattr(a, "timestamp", None) else None,
            "date": a.timestamp.isoformat() if getattr(a, "timestamp", None) else None,   # helpful alias for frontend
            "type": a.type,
            "description": a.description
        })
    return jsonify(out)


# ---------------- Contact history (POST) ----------------
@bp.route("/<contact_id>/history", methods=["POST"])
def add_contact_history(contact_id):
    db = current_app.db
    Activity = _get_activity_model()
    Contact = _get_contact_model()

    if not Activity or not Contact:
        return jsonify({"error": "Activity or Contact model not configured"}), 500

    wid, err = _require_workspace_id()
    if err:
        return err

    contact_obj = db.session.query(Contact).filter_by(id=contact_id, workspace_id=wid).one_or_none()
    if not contact_obj:
        return jsonify({"error": "contact not found"}), 404

    payload = request.get_json(silent=True) or {}
    try:
        # Allow client to specify a date; fall back to now
        date_str = payload.get("date") or payload.get("timestamp")
        try:
            ts = datetime.fromisoformat(date_str) if date_str else datetime.utcnow()
        except Exception:
            ts = datetime.utcnow()

        incoming_type = payload.get("type") or payload.get("activity_type")
        activity_kwargs = {
            "entity_type": "contact",
            "entity_id": contact_id,
            "type": normalize_activity_type(incoming_type),
            "title": payload.get("title") or (incoming_type or "History"),
            "description": payload.get("description"),
            "timestamp": ts,
        }

        # propagate workspace_id if Activity model has it
        if hasattr(Activity, "workspace_id"):
            ws = request.args.get("workspace_id") or request.headers.get("X-Workspace-ID")
            if not ws and hasattr(contact_obj, "workspace_id"):
                ws = getattr(contact_obj, "workspace_id", None)
            if ws is not None:
                activity_kwargs["workspace_id"] = ws

        a = Activity(**activity_kwargs)
        db.session.add(a)
        db.session.commit()
        return jsonify({
            "id": getattr(a, "id", None),
            "timestamp": a.timestamp.isoformat() if getattr(a, "timestamp", None) else None,
            "date": a.timestamp.isoformat() if getattr(a, "timestamp", None) else None
        }), 201
    except Exception:
        current_app.logger.exception("add_contact_history failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error"}), 500


# ---------------- Simple search endpoint ----------------
@bp.route("/search", methods=["GET"])
def search_contacts():
    """
    GET /contacts/search?q=...&workspace_id=...&limit=20
    """
    q = request.args.get("q", "").strip()
    workspace_id = request.args.get("workspace_id")
    limit = int(request.args.get("limit", 20))

    db = current_app.db
    Contact = _get_contact_model()
    if not Contact:
        return jsonify({"error": "Contact model not configured"}), 500

    if not q:
        return jsonify({"data": []})

    colnames = _model_columns(Contact)
    filters = []
    if "name" in colnames:
        try:
            filters.append(Contact.name.ilike(f"%{q}%"))
        except Exception:
            filters.append(Contact.name == q)
    if "email" in colnames:
        try:
            filters.append(Contact.email.ilike(f"%{q}%"))
        except Exception:
            filters.append(Contact.email == q)
    if "phone" in colnames:
        try:
            filters.append(Contact.phone.ilike(f"%{q}%"))
        except Exception:
            filters.append(Contact.phone == q)

    if not filters:
        return jsonify({"data": []})

    query = db.session.query(Contact).filter(or_(*filters))
    if workspace_id and "workspace_id" in colnames:
        try:
            query = query.filter(_col_compare_expr(getattr(Contact, "workspace_id"), workspace_id))
        except Exception:
            query = query.filter(getattr(Contact, "workspace_id") == workspace_id)

    results = query.limit(limit).all()
    return jsonify({"data": [_serialize_contact(r, allowed_fields=colnames) for r in results]})
