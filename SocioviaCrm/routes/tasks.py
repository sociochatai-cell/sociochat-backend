# crm_management/routes/tasks.py
import inspect
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, current_app, session
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import desc, or_

bp = Blueprint("tasks", __name__, url_prefix="/tasks")


# --------- Utilities ---------
def parse_date(s):
    """Parse a date or datetime string; return None on failure."""
    if not s:
        return None
    try:
        # If there's a 'T', assume ISO datetime; otherwise YYYY-MM-DD
        return datetime.fromisoformat(s) if "T" in s else datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return None


def _get_task_model():
    """Safely fetch the Task model from current_app.crm_models."""
    try:
        return current_app.crm_models.get("Task")
    except Exception:
        return None


def _get_activity_model():
    """Safely fetch the Activity model (optional)."""
    try:
        return current_app.crm_models.get("Activity")
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

    Returns None when authorized, else an (response, status) tuple. Fails CLOSED.
    """
    from tenant.context import get_current_user, user_owns_workspace
    user = get_current_user()
    if not user:
        return (jsonify({"success": False, "error": "authentication_required"}), 401)
    if not user_owns_workspace(user, getattr(row, "workspace_id", None)):
        return (jsonify({"success": False, "error": "forbidden"}), 403)
    return None


def _model_columns(model):
    """Return list of column names for this ORM model (or empty list)."""
    try:
        return list(model.__table__.columns.keys())
    except Exception:
        return []


def _serialize_task(t, allowed_fields=None):
    """Serialize a Task row to a dict, converting datetimes to ISO strings."""
    if not allowed_fields:
        allowed_fields = getattr(t.__class__, "__table__", None)
        if allowed_fields:
            allowed_fields = list(t.__class__.__table__.columns.keys())
        else:
            allowed_fields = []

    out = {}
    for k in allowed_fields:
        val = getattr(t, k, None)
        if isinstance(val, datetime):
            out[k] = val.isoformat()
        else:
            out[k] = val

    # Always include id if present
    if hasattr(t, "id"):
        out.setdefault("id", getattr(t, "id"))
    return out


# --------- Helpers for Activity logging ---------
def _log_activity(entity_type, entity_id, activity_type, title, description=None, workspace_id=None):
    Activity = _get_activity_model()
    if not Activity:
        return
    try:
        a_kwargs = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "type": activity_type,
            "title": title,
            "description": description,
            "timestamp": datetime.utcnow(),
        }
        if workspace_id and hasattr(Activity, "workspace_id"):
            a_kwargs["workspace_id"] = workspace_id
        a = Activity(**a_kwargs)
        db = current_app.db
        db.session.add(a)
        db.session.commit()
    except Exception:
        current_app.logger.exception("Failed to log activity")


# --------- List + Create ---------
@bp.route("", methods=["GET", "POST"])
def tasks_handler():
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    colnames = _model_columns(Task)

    # ---------- GET /tasks ----------
    if request.method == "GET":
        workspace_id = request.args.get("workspace_id")
        user_id = request.args.get("user_id")

        # SECURITY: resolve + verify the caller owns the workspace and ALWAYS
        # scope to it. Was fail-OPEN: with no workspace_id it returned every
        # tenant's tasks.
        ws, err = _require_owned_workspace(workspace_id)
        if err:
            return err
        workspace_id = ws.id

        q = db.session.query(Task)

        if "workspace_id" in colnames:
            q = q.filter(getattr(Task, "workspace_id") == workspace_id)

        # user_id can still be int if the column is int
        if user_id is not None and "user_id" in colnames:
            try:
                uval = int(user_id)
            except Exception:
                uval = user_id
            q = q.filter(getattr(Task, "user_id") == uval)

        # Optional filters
        completed = request.args.get("completed")
        if completed is not None and "completed" in colnames:
            if completed.lower() in ("true", "1", "yes"):
                q = q.filter(getattr(Task, "completed") == True)   # noqa: E712
            elif completed.lower() in ("false", "0", "no"):
                q = q.filter(getattr(Task, "completed") == False)  # noqa: E712

        related_to_type = request.args.get("related_to_type")
        if related_to_type and "related_to_type" in colnames:
            q = q.filter(getattr(Task, "related_to_type") == related_to_type)

        related_to_id = request.args.get("related_to_id")
        if related_to_id and "related_to_id" in colnames:
            q = q.filter(getattr(Task, "related_to_id") == related_to_id)

        # Order: due_date (nulls last) if present, else created_at desc, else id desc
        try:
            if "due_date" in colnames:
                q = q.order_by(getattr(Task, "due_date").nullslast())
            elif "created_at" in colnames:
                q = q.order_by(desc(getattr(Task, "created_at")))
            elif hasattr(Task, "id"):
                q = q.order_by(desc(getattr(Task, "id")))
        except Exception:
            pass

        try:
            tasks = q.all()
        except SQLAlchemyError as e:
            current_app.logger.exception("GET /tasks DB error")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"error": "DB error", "details": str(e)}), 500

        return jsonify([_serialize_task(t, allowed_fields=colnames) for t in tasks])

    # ---------- POST /tasks ----------
    payload = request.get_json(silent=True) or {}

    title = payload.get("title")
    if not title:
        return jsonify({"error": "title required"}), 400

    # workspace_id is required if the column exists (your DB has NOT NULL)
    workspace_id_param = request.args.get("workspace_id")
    ws_val = None
    if "workspace_id" in colnames:
        if not workspace_id_param:
            return jsonify({"error": "workspace_id query param required"}), 400
        # SECURITY: verify the caller owns the workspace they're writing into.
        ws, err = _require_owned_workspace(workspace_id_param)
        if err:
            return err
        ws_val = ws.id

    # user_id: optional if column doesn't exist, otherwise require query or session/header
    user_id_param = request.args.get("user_id")
    user_val = None
    if "user_id" in colnames:
        if user_id_param:
            try:
                user_val = int(user_id_param)
            except Exception:
                user_val = user_id_param
        else:
            # fallback to session/header
            fallback = _get_request_user_id()
            if fallback is None:
                return jsonify({"error": "user_id query param required or session/header present"}), 400
            user_val = fallback

    # Build kwargs based on model columns
    create_kwargs = {}

    for k, v in payload.items():
        if k not in colnames:
            continue
        # Handle date/datetime-ish fields
        if k in ("due_date", "created_at", "updated_at") or k.endswith("date") or k.endswith("at"):
            create_kwargs[k] = parse_date(v) if v else None
        else:
            create_kwargs[k] = v

    # Force required / default fields
    create_kwargs["title"] = title

    if ws_val is not None and "workspace_id" in colnames:
        create_kwargs["workspace_id"] = ws_val

    if user_val is not None and "user_id" in colnames:
        create_kwargs["user_id"] = user_val

    if "priority" in colnames and "priority" not in create_kwargs:
        create_kwargs["priority"] = payload.get("priority") or "medium"

    if "completed" in colnames and "completed" not in create_kwargs:
        create_kwargs["completed"] = bool(payload.get("completed", False))

    if "related_to_type" in colnames and "related_to_type" not in create_kwargs:
        create_kwargs["related_to_type"] = payload.get("related_to_type") or "general"

    if "related_to_id" in colnames and "related_to_id" not in create_kwargs:
        create_kwargs["related_to_id"] = payload.get("related_to_id")

    if "created_at" in colnames and "created_at" not in create_kwargs:
        create_kwargs["created_at"] = datetime.utcnow()

    try:
        t = Task(**create_kwargs)
        db.session.add(t)
        db.session.commit()

        # log activity if possible
        try:
            _log_activity("task", getattr(t, "id", None), "task_created", "Task created", description=f"Task '{t.title}' created", workspace_id=create_kwargs.get("workspace_id"))
        except Exception:
            pass

        return jsonify({"id": getattr(t, "id", None)}), 201

    except TypeError as te:
        current_app.logger.debug("TypeError on Task(**kwargs), trying filtered constructor: %s", te)
        try:
            sig_names = []
            try:
                sig = inspect.signature(Task)
                sig_names = [p for p in sig.parameters.keys() if p != "self"]
            except Exception:
                sig_names = colnames

            filtered = {k: v for k, v in create_kwargs.items() if k in sig_names}
            t = Task(**filtered)
            db.session.add(t)
            db.session.commit()
            try:
                _log_activity("task", getattr(t, "id", None), "task_created", "Task created", description=f"Task '{t.title}' created", workspace_id=filtered.get("workspace_id"))
            except Exception:
                pass
            return jsonify({"id": getattr(t, "id", None)}), 201
        except Exception as e:
            current_app.logger.exception("create_task failed after filtering")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"error": "DB error", "details": str(e)}), 500

    except SQLAlchemyError as e:
        current_app.logger.exception("create_task DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500


# --------- Convenience endpoints: complete / bulk-complete / snooze / reassign ---------

@bp.route("/<task_id>/complete", methods=["POST"])
def complete_task(task_id):
    """
    POST /tasks/<task_id>/complete  -> marks completed = True (if column exists) and logs Activity
    Optional body: { "completed": true/false } to toggle
    """
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    t = db.session.get(Task, task_id)
    if not t:
        return jsonify({"error": "not found"}), 404

    # SECURITY (IDOR): verify the caller owns this task's workspace.
    auth_err = _authorize_row(t)
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    completed_val = payload.get("completed", True)

    if "completed" in _model_columns(Task):
        setattr(t, "completed", bool(completed_val))

    # optionally set completed_at if available
    if completed_val and "completed_at" in _model_columns(Task):
        try:
            setattr(t, "completed_at", datetime.utcnow())
        except Exception:
            pass

    if "updated_at" in _model_columns(Task):
        try:
            setattr(t, "updated_at", datetime.utcnow())
        except Exception:
            pass

    try:
        db.session.add(t)
        db.session.commit()
        # log
        try:
            _log_activity("task", task_id, "task_completed" if completed_val else "task_uncompleted",
                          "Task completed" if completed_val else "Task marked incomplete",
                          description=f"Task {task_id} {'completed' if completed_val else 'marked incomplete'}",
                          workspace_id=getattr(t, "workspace_id", None))
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        current_app.logger.exception("complete_task failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


@bp.route("/bulk-complete", methods=["POST"])
def bulk_complete():
    """
    POST /tasks/bulk-complete
    Body: { "task_ids": ["id1","id2"...], "completed": true/false }
    """
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    payload = request.get_json(silent=True) or {}
    ids = payload.get("task_ids") or []
    completed_val = payload.get("completed", True)

    if not ids:
        return jsonify({"error": "task_ids required"}), 400

    # SECURITY (IDOR): only mutate tasks in a workspace the caller owns.
    from tenant.context import get_current_user, user_owns_workspace
    _user = get_current_user()
    if not _user:
        return jsonify({"success": False, "error": "authentication_required"}), 401

    try:
        q = db.session.query(Task).filter(Task.id.in_(ids))
        updated = 0
        for t in q.all():
            # Skip any task whose workspace the caller does not own.
            if not user_owns_workspace(_user, getattr(t, "workspace_id", None)):
                continue
            if "completed" in _model_columns(Task):
                setattr(t, "completed", bool(completed_val))
            if completed_val and "completed_at" in _model_columns(Task):
                try:
                    setattr(t, "completed_at", datetime.utcnow())
                except Exception:
                    pass
            if "updated_at" in _model_columns(Task):
                try:
                    setattr(t, "updated_at", datetime.utcnow())
                except Exception:
                    pass
            db.session.add(t)
            updated += 1
        db.session.commit()
        # log one aggregate activity (workspace cannot be assumed)
        try:
            _log_activity("task", ",".join(ids), "bulk_task_completed", "Multiple tasks completed",
                          description=f"{updated} tasks marked {'completed' if completed_val else 'incomplete'}")
        except Exception:
            pass
        return jsonify({"ok": True, "updated": updated})
    except Exception as e:
        current_app.logger.exception("bulk_complete failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


@bp.route("/<task_id>/snooze", methods=["POST"])
def snooze_task(task_id):
    """
    POST /tasks/<task_id>/snooze
    Body: { "days": 1 }  -> pushes due_date forward by N days. If no due_date, sets due_date = now + days.
    """
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    t = db.session.get(Task, task_id)
    if not t:
        return jsonify({"error": "not found"}), 404

    # SECURITY (IDOR): verify the caller owns this task's workspace.
    auth_err = _authorize_row(t)
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    days = int(payload.get("days", 1))
    if "due_date" not in _model_columns(Task):
        return jsonify({"error": "due_date column not available"}), 400

    try:
        if getattr(t, "due_date", None):
            new_due = getattr(t, "due_date") + timedelta(days=days)
        else:
            new_due = datetime.utcnow() + timedelta(days=days)
        setattr(t, "due_date", new_due)
        if "updated_at" in _model_columns(Task):
            setattr(t, "updated_at", datetime.utcnow())
        db.session.add(t)
        db.session.commit()
        # log
        try:
            _log_activity("task", task_id, "task_snoozed", "Task snoozed", description=f"Snoozed by {days} day(s)", workspace_id=getattr(t, "workspace_id", None))
        except Exception:
            pass
        return jsonify({"ok": True, "due_date": new_due.isoformat()})
    except Exception as e:
        current_app.logger.exception("snooze_task failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


@bp.route("/<task_id>/reassign", methods=["POST"])
def reassign_task(task_id):
    """
    POST /tasks/<task_id>/reassign
    Body: { "user_id": <new owner id> }
    """
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    t = db.session.get(Task, task_id)
    if not t:
        return jsonify({"error": "not found"}), 404

    # SECURITY (IDOR): verify the caller owns this task's workspace.
    auth_err = _authorize_row(t)
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    new_user = payload.get("user_id")
    if new_user is None:
        return jsonify({"error": "user_id required"}), 400

    try:
        if "user_id" in _model_columns(Task):
            try:
                new_user_val = int(new_user)
            except Exception:
                new_user_val = new_user
            setattr(t, "user_id", new_user_val)
        if "updated_at" in _model_columns(Task):
            setattr(t, "updated_at", datetime.utcnow())
        db.session.add(t)
        db.session.commit()
        # log
        try:
            _log_activity("task", task_id, "task_reassigned", "Task reassigned", description=f"Reassigned to {new_user}", workspace_id=getattr(t, "workspace_id", None))
        except Exception:
            pass
        return jsonify({"ok": True})
    except Exception as e:
        current_app.logger.exception("reassign_task failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# --------- Get / Update / Delete single task ---------
@bp.route("/<task_id>", methods=["GET", "PUT", "PATCH", "DELETE"])
def task_detail(task_id):
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    colnames = _model_columns(Task)

    t = db.session.get(Task, task_id)
    if not t:
        return jsonify({"error": "not found"}), 404

    # SECURITY (IDOR): verify the caller owns this task's workspace before any op.
    auth_err = _authorize_row(t)
    if auth_err:
        return auth_err

    # ---------- GET /tasks/<task_id> ----------
    if request.method == "GET":
        return jsonify(_serialize_task(t, allowed_fields=colnames))

    # ---------- DELETE /tasks/<task_id> ----------
    if request.method == "DELETE":
        try:
            db.session.delete(t)
            db.session.commit()
            try:
                _log_activity("task", task_id, "task_deleted", "Task deleted", description="Deleted via API", workspace_id=getattr(t, "workspace_id", None))
            except Exception:
                pass
            return jsonify({"ok": True})
        except SQLAlchemyError as e:
            current_app.logger.exception("delete_task DB error")
            try:
                db.session.rollback()
            except Exception:
                pass
            return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500

    # ---------- PUT/PATCH /tasks/<task_id> ----------
    payload = request.get_json(silent=True) or {}

    for k, v in payload.items():
        if k not in colnames:
            continue
        if k in ("due_date", "created_at", "updated_at") or k.endswith("date") or k.endswith("at"):
            setattr(t, k, parse_date(v) if v else None)
        else:
            setattr(t, k, v)

    try:
        db.session.commit()
        try:
            _log_activity("task", task_id, "task_updated", "Task updated", description="Updated via API", workspace_id=getattr(t, "workspace_id", None))
        except Exception:
            pass
        return jsonify({"ok": True, "task": _serialize_task(t, allowed_fields=colnames)})
    except SQLAlchemyError as e:
        current_app.logger.exception("update_task DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"ok": False, "error": "DB error", "details": str(e)}), 500


# --------- Search tasks ---------
@bp.route("/search", methods=["GET"])
def search_tasks():
    """
    GET /tasks/search?q=...&workspace_id=...&limit=20
    Search in title / description (case-insensitive), scoped by workspace if provided.
    """
    db = current_app.db
    Task = _get_task_model()
    if not Task:
        return jsonify({"error": "Task model not configured"}), 500

    colnames = _model_columns(Task)

    qtext = request.args.get("q", "").strip()
    if not qtext:
        return jsonify({"data": []})

    workspace_id = request.args.get("workspace_id")
    limit = int(request.args.get("limit", 20))

    # SECURITY: resolve + verify the caller owns the workspace and ALWAYS scope
    # the search to it (was fail-OPEN when workspace_id was omitted).
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    filters = []
    if "title" in colnames:
        try:
            filters.append(getattr(Task, "title").ilike(f"%{qtext}%"))
        except Exception:
            filters.append(getattr(Task, "title") == qtext)
    if "description" in colnames:
        try:
            filters.append(getattr(Task, "description").ilike(f"%{qtext}%"))
        except Exception:
            filters.append(getattr(Task, "description") == qtext)

    if not filters:
        return jsonify({"data": []})

    query = db.session.query(Task).filter(or_(*filters))

    # 🔧 same: workspace_id is TEXT
    if workspace_id and "workspace_id" in colnames:
        query = query.filter(getattr(Task, "workspace_id") == workspace_id)

    try:
        results = query.limit(limit).all()
    except SQLAlchemyError as e:
        current_app.logger.exception("search_tasks DB error")
        try:
            db.session.rollback()
        except Exception:
            pass
        return jsonify({"error": "DB error", "details": str(e)}), 500

    return jsonify({"data": [_serialize_task(r, allowed_fields=colnames) for r in results]})
