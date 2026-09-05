"""
Dataset Routes
==============

Flask blueprint with all endpoints for the Datasets feature.
Handles CRUD, CSV import, Google Sheets import, internal CRM import,
Sociovia CRM import, and external CRM import (HubSpot, Pipedrive).
"""

import os
import io
import csv
import json
import logging
from datetime import datetime
from urllib.parse import unquote

import requests as http_requests
from flask import Blueprint, request, jsonify, current_app
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.attributes import flag_modified

from .dataset_models import Dataset, DatasetRow
from models import db
from subscription.decorators import require_feature

logger = logging.getLogger(__name__)

dataset_bp = Blueprint("datasets", __name__)


# ============================================================
# Helpers
# ============================================================


def _get_workspace_id():
    return (
        request.args.get("workspace_id")
        or request.headers.get("X-Workspace-ID")
    )


def _resolve_owned_workspace_id(workspace_id):
    """Verify the authenticated user owns ``workspace_id``.

    Returns (int_workspace_id, error_response). Fails CLOSED: no user -> 401,
    not owned / bad id -> 403.
    """
    from tenant.context import get_current_user, resolve_owned_workspace

    workspace, err = resolve_owned_workspace(get_current_user(), workspace_id)
    if err:
        return None, err
    return int(workspace.id), None


def _resolve_owned_dataset(dataset_id):
    """Load a Dataset by id and verify the authenticated user owns its workspace.

    Returns (dataset, error_response). Fails CLOSED.
    """
    from tenant.context import get_current_user, user_owns_workspace

    ds = Dataset.query.get(dataset_id)
    if not ds:
        return None, _error("Dataset not found", 404)

    user = get_current_user()
    if not user:
        return None, _error("authentication_required", 401)
    if not user_owns_workspace(user, ds.workspace_id):
        return None, _error("forbidden", 403)
    return ds, None


@dataset_bp.before_request
def _enforce_dataset_ownership():
    """Blueprint-wide IDOR guard. A dataset belongs to a workspace. Every
    ``/datasets/<dataset_id>`` and ``/workspaces/<workspace_id>/...`` route must be
    owned by the caller; any client-supplied workspace_id is verified too. Fails
    closed. (Per-endpoint checks below are kept as defense in depth.)"""
    if request.method == "OPTIONS":
        return None
    va = request.view_args or {}
    if va.get("dataset_id") is not None:
        _ds, err = _resolve_owned_dataset(va["dataset_id"])
        if err:
            return err
        return None
    if va.get("workspace_id") is not None:
        _w, err = _resolve_owned_workspace_id(va["workspace_id"])
        if err:
            return err
        return None
    from tenant.context import get_current_user, user_owns_workspace
    user = get_current_user()
    if not user:
        return jsonify({"success": False, "error": "authentication_required"}), 401
    body = request.get_json(silent=True) if request.is_json else None
    body = body if isinstance(body, dict) else {}
    wid = (
        request.args.get("workspace_id")
        or request.headers.get("X-Workspace-ID")
        or body.get("workspace_id")
        or request.form.get("workspace_id")
    )
    if wid and not user_owns_workspace(user, wid):
        return jsonify({"success": False, "error": "forbidden"}), 403
    return None


def _success(data=None, **kwargs):
    resp = {"success": True}
    if data is not None:
        resp["data"] = data
    resp.update(kwargs)
    return jsonify(resp)


def _error(msg, status=400):
    return jsonify({"success": False, "error": msg}), status


# ============================================================
# Dataset CRUD
# ============================================================


@dataset_bp.route("/workspaces/<workspace_id>/datasets", methods=["GET"])
def list_datasets(workspace_id):
    owned_ws_id, err = _resolve_owned_workspace_id(workspace_id)
    if err:
        return err
    datasets = (
        Dataset.query
        .filter_by(workspace_id=owned_ws_id)
        .order_by(Dataset.updated_at.desc())
        .all()
    )
    return _success([d.to_dict() for d in datasets])


@dataset_bp.route("/workspaces/<workspace_id>/datasets", methods=["POST"])
@require_feature("whatsapp_datasets")
def create_dataset(workspace_id):
    owned_ws_id, err = _resolve_owned_workspace_id(workspace_id)
    if err:
        return err

    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return _error("name is required")

    ds = Dataset(
        workspace_id=owned_ws_id,
        name=name,
        description=body.get("description", ""),
        columns=body.get("columns", []),
        source_type=body.get("source_type", "manual"),
        source_config=body.get("source_config", {}),
    )
    db.session.add(ds)
    try:
        db.session.commit()
    except SQLAlchemyError as e:
        db.session.rollback()
        logger.exception("create_dataset error")
        return _error(str(e), 500)
    return _success(ds.to_dict())


@dataset_bp.route("/datasets/<int:dataset_id>", methods=["GET"])
def get_dataset(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err
    return _success(ds.to_dict())


@dataset_bp.route("/datasets/<int:dataset_id>", methods=["DELETE"])
def delete_dataset(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err
    db.session.delete(ds)
    db.session.commit()
    return _success({"deleted": True})


# ============================================================
# Row CRUD
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/rows", methods=["GET"])
def list_rows(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    page = int(request.args.get("page", 1))
    limit = int(request.args.get("limit", 50))

    q = DatasetRow.query.filter_by(dataset_id=dataset_id).order_by(DatasetRow.id.asc())
    total = q.count()
    rows = q.offset((page - 1) * limit).limit(limit).all()

    return _success(
        [r.to_dict() for r in rows],
        pagination={"page": page, "limit": limit, "total": total},
    )


@dataset_bp.route("/datasets/<int:dataset_id>/rows", methods=["POST"])
def add_row(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    row_data = body.get("data", {})

    # Auto-add columns that don't exist yet
    existing_cols = set(ds.columns or [])
    new_cols = [k for k in row_data.keys() if k not in existing_cols]
    if new_cols:
        ds.columns = list(existing_cols | set(new_cols))
        flag_modified(ds, "columns")

    row = DatasetRow(dataset_id=dataset_id, data=row_data)
    db.session.add(row)
    db.session.commit()
    return _success(row.to_dict())


@dataset_bp.route("/datasets/<int:dataset_id>/rows/<int:row_id>", methods=["PUT"])
def update_row(dataset_id, row_id):
    _ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    row = DatasetRow.query.filter_by(id=row_id, dataset_id=dataset_id).first()
    if not row:
        return _error("Row not found", 404)

    body = request.get_json(silent=True) or {}
    row.data = body.get("data", row.data)
    row.updated_at = datetime.utcnow()
    db.session.commit()
    return _success(row.to_dict())


@dataset_bp.route("/datasets/<int:dataset_id>/rows/<int:row_id>", methods=["DELETE"])
def delete_row(dataset_id, row_id):
    _ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    row = DatasetRow.query.filter_by(id=row_id, dataset_id=dataset_id).first()
    if not row:
        return _error("Row not found", 404)
    db.session.delete(row)
    db.session.commit()
    return _success({"deleted": True})


# ============================================================
# Column Management
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/columns", methods=["POST"])
def add_column(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    col_name = body.get("column_name", "").strip()
    if not col_name:
        return _error("column_name is required")

    cols = list(ds.columns or [])
    if col_name not in cols:
        cols.append(col_name)
        ds.columns = cols
        flag_modified(ds, "columns")
        db.session.commit()

    return _success(ds.to_dict())


@dataset_bp.route("/datasets/<int:dataset_id>/columns/<path:col_name>", methods=["DELETE"])
def remove_column(dataset_id, col_name):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    col_name = unquote(col_name).strip()
    cols = list(ds.columns or [])
    if col_name in cols:
        cols.remove(col_name)
        ds.columns = cols
        flag_modified(ds, "columns")

        # Remove column data from all rows
        for row in DatasetRow.query.filter_by(dataset_id=dataset_id).all():
            data = dict(row.data or {})
            data.pop(col_name, None)
            row.data = data
            flag_modified(row, "data")

        db.session.commit()

    return _success(ds.to_dict())


# ============================================================
# CSV Preview & Import
# ============================================================


@dataset_bp.route("/csv/preview", methods=["POST"])
def csv_preview():
    if "file" not in request.files:
        return _error("No file uploaded")

    file = request.files["file"]
    content = file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))
    columns = reader.fieldnames or []
    rows = []
    total = 0
    for i, row in enumerate(reader):
        total += 1
        if i < 10:
            rows.append(dict(row))

    return _success(columns=list(columns), preview_rows=rows, total_rows=total)


@dataset_bp.route("/datasets/<int:dataset_id>/upload-mapped", methods=["POST"])
def upload_csv_mapped(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    if "file" not in request.files:
        return _error("No file uploaded")

    file = request.files["file"]
    mapping_str = request.form.get("column_mapping", "{}")
    replace = request.form.get("replace", "false").lower() == "true"

    try:
        column_mapping = json.loads(mapping_str)
    except Exception:
        column_mapping = {}

    content = file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))

    if replace:
        DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

    mapped_cols = set()
    rows_added = 0
    for row_dict in reader:
        mapped_row = {}
        for orig_col, value in row_dict.items():
            mapped_name = column_mapping.get(orig_col, orig_col)
            mapped_row[mapped_name] = value
            mapped_cols.add(mapped_name)

        db_row = DatasetRow(dataset_id=dataset_id, data=mapped_row, row_order=rows_added)
        db.session.add(db_row)
        rows_added += 1

    # Update dataset columns and metadata
    ds.columns = sorted(mapped_cols)
    ds.column_mapping = column_mapping
    ds.source_type = "csv"
    ds.last_sync_at = datetime.utcnow()
    ds.sync_status = "done"
    db.session.commit()

    return _success(rows_added=rows_added)


@dataset_bp.route("/datasets/<int:dataset_id>/upload", methods=["POST"])
def upload_csv_simple(dataset_id):
    """Simple CSV upload (no column mapping)."""
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    if "file" not in request.files:
        return _error("No file uploaded")

    file = request.files["file"]
    replace = request.args.get("replace", "false").lower() == "true"

    content = file.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(content))

    if replace:
        DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

    all_cols = set()
    rows_added = 0
    for row_dict in reader:
        all_cols.update(row_dict.keys())
        db_row = DatasetRow(dataset_id=dataset_id, data=dict(row_dict), row_order=rows_added)
        db.session.add(db_row)
        rows_added += 1

    ds.columns = sorted(all_cols)
    ds.source_type = "csv"
    ds.last_sync_at = datetime.utcnow()
    ds.sync_status = "done"
    db.session.commit()

    return _success(rows_added=rows_added)


# ============================================================
# Google Sheets Preview & Import
# ============================================================


def _get_sheets_service():
    """Build a Google Sheets API service using service account credentials."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        creds_json = os.getenv("GOOGLE_SHEETS_ACCOUNT_JSON", "")
        if not creds_json:
            return None, "GOOGLE_SHEETS_ACCOUNT_JSON not configured"

        creds_dict = json.loads(creds_json)
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
        client = gspread.authorize(credentials)
        return client, None
    except ImportError:
        return None, "gspread library not installed"
    except Exception as e:
        return None, str(e)


def _extract_sheet_id(url):
    """Extract spreadsheet ID from a Google Sheets URL."""
    import re
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url)
    return match.group(1) if match else None


@dataset_bp.route("/sheets/preview", methods=["POST"])
def sheets_preview():
    body = request.get_json(silent=True) or {}
    sheet_url = body.get("sheet_url", "")
    sheet_name = body.get("sheet_name", "Sheet1")

    sheet_id = _extract_sheet_id(sheet_url)
    if not sheet_id:
        return _error("Invalid Google Sheets URL")

    client, err = _get_sheets_service()
    if not client:
        return _error(f"Sheets service error: {err}")

    try:
        spreadsheet = client.open_by_key(sheet_id)
        sheet_names = [ws.title for ws in spreadsheet.worksheets()]
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_records()

        headers = list(all_values[0].keys()) if all_values else []
        preview_rows = all_values[:10]
        total_rows = len(all_values)

        return _success(
            headers=headers,
            preview_rows=preview_rows,
            total_rows=total_rows,
            sheet_names=sheet_names,
        )
    except Exception as e:
        return _error(f"Sheet read error: {str(e)}")


@dataset_bp.route("/datasets/<int:dataset_id>/import-sheets", methods=["POST"])
def import_sheets(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    sheet_url = body.get("sheet_url", "")
    sheet_name = body.get("sheet_name", "Sheet1")
    column_mapping = body.get("column_mapping", {})
    replace = body.get("replace", False)

    sheet_id = _extract_sheet_id(sheet_url)
    if not sheet_id:
        return _error("Invalid Google Sheets URL")

    client, err = _get_sheets_service()
    if not client:
        return _error(f"Sheets service error: {err}")

    try:
        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_records()

        if replace:
            DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

        all_cols = set()
        rows_added = 0
        for row_dict in all_values:
            mapped_row = {}
            for k, v in row_dict.items():
                mapped_name = column_mapping.get(k, k)
                mapped_row[mapped_name] = str(v) if v is not None else ""
                all_cols.add(mapped_name)

            db_row = DatasetRow(dataset_id=dataset_id, data=mapped_row, row_order=rows_added)
            db.session.add(db_row)
            rows_added += 1

        ds.columns = sorted(all_cols)
        ds.column_mapping = column_mapping
        ds.source_type = "google_sheets"
        ds.source_config = {"sheet_url": sheet_url, "sheet_name": sheet_name}
        ds.last_sync_at = datetime.utcnow()
        ds.sync_status = "done"
        db.session.commit()

        return _success(rows_added=rows_added)
    except Exception as e:
        db.session.rollback()
        return _error(f"Sheet import error: {str(e)}")


@dataset_bp.route("/datasets/<int:dataset_id>/sync-sheets", methods=["POST"])
def sync_sheets(dataset_id):
    """Re-import from the same Google Sheet URL stored in source_config."""
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err
    if ds.source_type != "google_sheets" or not ds.source_config:
        return _error("Dataset is not a Google Sheets source")

    sheet_url = ds.source_config.get("sheet_url")
    sheet_name = ds.source_config.get("sheet_name", "Sheet1")

    if not sheet_url:
        return _error("No sheet URL stored for this dataset")

    sheet_id = _extract_sheet_id(sheet_url)
    client, err = _get_sheets_service()
    if not client:
        return _error(f"Sheets service error: {err}")

    try:
        ds.sync_status = "syncing"
        db.session.commit()

        spreadsheet = client.open_by_key(sheet_id)
        worksheet = spreadsheet.worksheet(sheet_name)
        all_values = worksheet.get_all_records()

        DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

        all_cols = set()
        rows_added = 0
        for row_dict in all_values:
            mapped_row = {}
            for k, v in row_dict.items():
                mapped_name = (ds.column_mapping or {}).get(k, k)
                mapped_row[mapped_name] = str(v) if v is not None else ""
                all_cols.add(mapped_name)
            db_row = DatasetRow(dataset_id=dataset_id, data=mapped_row, row_order=rows_added)
            db.session.add(db_row)
            rows_added += 1

        ds.columns = sorted(all_cols)
        ds.last_sync_at = datetime.utcnow()
        ds.sync_status = "done"
        ds.sync_error = None
        db.session.commit()

        return _success(rows_added=rows_added)
    except Exception as e:
        db.session.rollback()
        ds.sync_status = "error"
        ds.sync_error = str(e)
        db.session.commit()
        return _error(f"Sync error: {str(e)}")


# ============================================================
# Internal CRM Preview (WhatsApp Contacts from same DB)
# ============================================================


@dataset_bp.route("/crm/preview", methods=["POST"])
def crm_preview():
    """Preview data from internal CRM (WhatsApp contacts) or Sociovia CRM."""
    body = request.get_json(silent=True) or {}
    source = body.get("source", "contacts")
    ws_id = body.get("workspace_id")

    if not ws_id:
        return _error("workspace_id is required")

    # Fail CLOSED: the caller must own the workspace they are previewing.
    ws_id, err = _resolve_owned_workspace_id(ws_id)
    if err:
        return err

    if source == "whatsapp_contacts":
        return _preview_whatsapp_contacts(ws_id)
    elif source in ("contacts", "leads"):
        return _preview_sociovia_crm(source, ws_id, body)
    else:
        return _error(f"Unknown source: {source}")


def _preview_whatsapp_contacts(workspace_id):
    """Preview WhatsApp contacts from the local database."""
    try:
        from .models import WhatsAppConversation, WhatsAppAccount

        # Get all accounts for this workspace
        accounts = WhatsAppAccount.query.filter_by(workspace_id=int(workspace_id)).all()
        account_ids = [a.id for a in accounts]

        if not account_ids:
            return _success(
                fields=["name", "phone", "last_message_at"],
                preview_rows=[],
                total_records=0,
            )

        convos = (
            WhatsAppConversation.query
            .filter(WhatsAppConversation.account_id.in_(account_ids))
            .order_by(WhatsAppConversation.last_message_at.desc())
            .limit(100)
            .all()
        )

        fields = ["name", "phone", "last_message_at"]
        preview_rows = []
        for c in convos:
            preview_rows.append({
                "name": c.contact_name or c.wa_id or "Unknown",
                "phone": c.wa_id or "",
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else "",
            })

        return _success(
            fields=fields,
            preview_rows=preview_rows,
            total_records=len(preview_rows),
        )
    except Exception as e:
        logger.exception("WhatsApp contacts preview error")
        return _error(f"Error loading WhatsApp contacts: {str(e)}")


def _preview_sociovia_crm(source, workspace_id, body):
    """
    Preview contacts or leads from Sociovia CRM by calling its public API.
    The CRM URL can be provided in body or uses default from env.
    """
    crm_url = (
        body.get("crm_url")
        or os.getenv("SOCIOVIA_CRM_URL", "")
    )

    if not crm_url:
        # Fallback: try to read from the same DB if Sociovia tables exist
        return _preview_sociovia_from_db(source, workspace_id)

    # Call the Sociovia CRM API
    try:
        endpoint = f"{crm_url.rstrip('/')}/api/{source}"
        params = {"workspace_id": workspace_id}
        if source == "contacts":
            params["per_page"] = "100"

        resp = http_requests.get(endpoint, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        # Normalize response shape
        if source == "contacts":
            records = data.get("data", [])
        else:
            records = data if isinstance(data, list) else data.get("data", [])

        if not records:
            return _success(fields=[], preview_rows=[], total_records=0)

        fields = list(records[0].keys())
        # Exclude internal-only fields
        exclude = {"id", "workspace_id", "user_id", "details", "created_at", "updated_at"}
        fields = [f for f in fields if f not in exclude]

        preview_rows = []
        for rec in records[:50]:
            row = {f: str(rec.get(f, "") or "") for f in fields}
            preview_rows.append(row)

        total = data.get("meta", {}).get("total", len(records)) if source == "contacts" else len(records)

        return _success(
            fields=fields,
            preview_rows=preview_rows,
            total_records=total,
        )
    except http_requests.exceptions.RequestException as e:
        logger.warning("Sociovia CRM API call failed: %s", e)
        # Fallback to direct DB if API unreachable
        return _preview_sociovia_from_db(source, workspace_id)
    except Exception as e:
        logger.exception("CRM preview error")
        return _error(f"CRM preview error: {str(e)}")


def _preview_sociovia_from_db(source, workspace_id):
    """
    Fallback: query the Sociovia CRM tables directly if they exist in the same DB
    or via a second database connection.
    """
    try:
        sociovia_db_uri = os.getenv("SOCIOVIA_DATABASE_URI", "")
        if not sociovia_db_uri:
            return _error(
                "Sociovia CRM URL not configured. Set SOCIOVIA_CRM_URL in .env or provide crm_url in the request.",
                400,
            )

        from sqlalchemy import create_engine, text

        engine = create_engine(sociovia_db_uri, pool_pre_ping=True)

        table = "contacts" if source == "contacts" else "leads"
        with engine.connect() as conn:
            result = conn.execute(
                text(f"SELECT * FROM {table} WHERE workspace_id = :ws LIMIT 100"),
                {"ws": str(workspace_id)},
            )
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]

        exclude = {"workspace_id", "user_id", "details"}
        fields = [c for c in columns if c not in exclude]

        preview_rows = []
        for rec in rows:
            row = {}
            for f in fields:
                val = rec.get(f)
                if hasattr(val, "isoformat"):
                    row[f] = val.isoformat()
                else:
                    row[f] = str(val) if val is not None else ""
            preview_rows.append(row)

        return _success(
            fields=fields,
            preview_rows=preview_rows,
            total_records=len(preview_rows),
        )
    except Exception as e:
        logger.exception("Sociovia DB fallback error")
        return _error(f"Could not load CRM data: {str(e)}")


# ============================================================
# CRM Import (Sociovia / WhatsApp Contacts)
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/import-crm", methods=["POST"])
def import_crm(dataset_id):
    """Import data from internal CRM (WhatsApp contacts or Sociovia CRM)."""
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    source = body.get("source", "contacts")
    column_mapping = body.get("column_mapping", {})
    replace = body.get("replace", False)

    # Force the CRM query to the dataset's OWN workspace (server-derived) so a
    # client cannot pull another tenant's contacts into their dataset.
    ws_id = ds.workspace_id

    # Get preview data first
    if source == "whatsapp_contacts":
        preview_resp = _preview_whatsapp_contacts(ws_id)
    else:
        preview_resp = _preview_sociovia_crm(source, ws_id, body)

    preview_data = preview_resp.get_json()
    if not preview_data.get("success"):
        return preview_resp

    records = preview_data.get("preview_rows", [])
    fields = preview_data.get("fields", [])

    if not records:
        return _error("No records found to import")

    if replace:
        DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

    all_cols = set()
    rows_added = 0
    for rec in records:
        mapped_row = {}
        for k, v in rec.items():
            mapped_name = column_mapping.get(k, k)
            mapped_row[mapped_name] = v
            all_cols.add(mapped_name)
        db_row = DatasetRow(dataset_id=dataset_id, data=mapped_row, row_order=rows_added)
        db.session.add(db_row)
        rows_added += 1

    ds.columns = sorted(all_cols)
    ds.column_mapping = column_mapping
    ds.source_type = "sociovia" if source in ("contacts", "leads") else "crm"
    ds.source_config = {"source": source, "workspace_id": ws_id}
    ds.last_sync_at = datetime.utcnow()
    ds.sync_status = "done"
    db.session.commit()

    return _success(rows_added=rows_added)


# ============================================================
# External CRM Import — HubSpot
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/import-hubspot", methods=["POST"])
def import_hubspot(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    api_key = body.get("api_key", "").strip()
    replace = body.get("replace", False)

    if not api_key:
        return _error("api_key is required")

    try:
        # Fetch contacts from HubSpot API
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = http_requests.get(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers=headers,
            params={"limit": 100, "properties": "firstname,lastname,email,phone,company"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            return _error("No contacts found in HubSpot")

        if replace:
            DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

        all_cols = set()
        rows_added = 0
        for contact in results:
            props = contact.get("properties", {})
            row = {
                "name": f"{props.get('firstname', '')} {props.get('lastname', '')}".strip(),
                "email": props.get("email", ""),
                "phone": props.get("phone", ""),
                "company": props.get("company", ""),
            }
            all_cols.update(row.keys())
            db_row = DatasetRow(dataset_id=dataset_id, data=row, row_order=rows_added)
            db.session.add(db_row)
            rows_added += 1

        ds.columns = sorted(all_cols)
        ds.source_type = "hubspot"
        ds.last_sync_at = datetime.utcnow()
        ds.sync_status = "done"
        db.session.commit()

        return _success(rows_added=rows_added)
    except http_requests.exceptions.HTTPError as e:
        return _error(f"HubSpot API error: {e.response.status_code} - {e.response.text[:200]}")
    except Exception as e:
        db.session.rollback()
        return _error(f"HubSpot import error: {str(e)}")


# ============================================================
# External CRM Import — Pipedrive
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/import-pipedrive", methods=["POST"])
def import_pipedrive(dataset_id):
    ds, _owner_err = _resolve_owned_dataset(dataset_id)
    if _owner_err:
        return _owner_err

    body = request.get_json(silent=True) or {}
    api_key = body.get("api_key", "").strip()
    replace = body.get("replace", False)

    if not api_key:
        return _error("api_key is required")

    try:
        # Fetch persons from Pipedrive API
        resp = http_requests.get(
            "https://api.pipedrive.com/v1/persons",
            params={"api_token": api_key, "limit": 100},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()

        persons = data.get("data", [])
        if not persons:
            return _error("No contacts found in Pipedrive")

        if replace:
            DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

        all_cols = set()
        rows_added = 0
        for person in persons:
            # Extract primary email and phone
            emails = person.get("email", [])
            phones = person.get("phone", [])
            primary_email = emails[0].get("value", "") if emails else ""
            primary_phone = phones[0].get("value", "") if phones else ""

            row = {
                "name": person.get("name", ""),
                "email": primary_email,
                "phone": primary_phone,
                "company": (person.get("org_name") or ""),
            }
            all_cols.update(row.keys())
            db_row = DatasetRow(dataset_id=dataset_id, data=row, row_order=rows_added)
            db.session.add(db_row)
            rows_added += 1

        ds.columns = sorted(all_cols)
        ds.source_type = "pipedrive"
        ds.last_sync_at = datetime.utcnow()
        ds.sync_status = "done"
        db.session.commit()

        return _success(rows_added=rows_added)
    except http_requests.exceptions.HTTPError as e:
        return _error(f"Pipedrive API error: {e.response.status_code} - {e.response.text[:200]}")
    except Exception as e:
        db.session.rollback()
        return _error(f"Pipedrive import error: {str(e)}")


# ============================================================
# Sociovia CRM — Dedicated Import Endpoint
# ============================================================


@dataset_bp.route("/datasets/<int:dataset_id>/import-sociovia", methods=["POST"])
def import_sociovia(dataset_id):
    """
    Import contacts/leads/deals from Sociovia CRM.
    Body: { crm_url, workspace_id, data_type: "leads"|"contacts"|"deals" }
    """
    from .drip_models import WhatsAppDataset, WhatsAppDatasetRow
    from tenant.context import get_current_user, user_owns_workspace

    ds = WhatsAppDataset.query.get(dataset_id)
    if not ds:
        # Fallback: try the Dataset model too
        ds = Dataset.query.get(dataset_id)
    if not ds:
        return _error("Dataset not found", 404)

    # Ownership check: caller must own the dataset's workspace (fail closed).
    _user = get_current_user()
    if not _user:
        return _error("authentication_required", 401)
    if not user_owns_workspace(_user, getattr(ds, "workspace_id", None)):
        return _error("forbidden", 403)

    body = request.get_json(silent=True) or {}
    crm_url = body.get("crm_url", "").strip().rstrip("/")
    data_type = body.get("data_type", "contacts")
    replace = body.get("replace", False)

    if not crm_url:
        return _error("crm_url is required (Sociovia backend URL)")

    # Force the CRM query to the dataset's OWN workspace (server-derived) so a
    # client cannot pull another tenant's records into their dataset.
    ws_id = str(ds.workspace_id)

    try:
        endpoint = f"{crm_url}/api/{data_type}"
        params = {"workspace_id": ws_id}
        if data_type == "contacts":
            params["per_page"] = "500"

        resp = http_requests.get(endpoint, params=params, timeout=20)
        resp.raise_for_status()
        api_data = resp.json()

        # Normalize: contacts return {data: [...], meta: {...}}, leads return [...]
        if data_type == "contacts":
            records = api_data.get("data", [])
        else:
            records = api_data if isinstance(api_data, list) else api_data.get("data", [])

        if not records:
            return _error(f"No {data_type} found in Sociovia CRM")

        # Determine which row model to use based on the dataset model
        is_whatsapp_dataset = isinstance(ds, WhatsAppDataset)

        if replace:
            if is_whatsapp_dataset:
                WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).delete()
            else:
                DatasetRow.query.filter_by(dataset_id=dataset_id).delete()

        # Determine fields
        exclude = {"id", "workspace_id", "user_id", "details", "created_at", "updated_at"}
        fields = [k for k in records[0].keys() if k not in exclude]

        all_cols = set()
        rows_added = 0
        for rec in records:
            row = {}
            for f in fields:
                val = rec.get(f)
                row[f] = str(val) if val is not None else ""
                all_cols.add(f)
            if is_whatsapp_dataset:
                db_row = WhatsAppDatasetRow(dataset_id=dataset_id, data=row)
            else:
                db_row = DatasetRow(dataset_id=dataset_id, data=row, row_order=rows_added)
            db.session.add(db_row)
            rows_added += 1

        ds.columns = sorted(all_cols)
        ds.source_type = "sociovia"
        ds.source_config = {"crm_url": crm_url, "workspace_id": ws_id, "data_type": data_type}
        ds.last_sync_at = datetime.utcnow()
        ds.sync_status = "done" if not is_whatsapp_dataset else "synced"
        if is_whatsapp_dataset:
            ds.total_rows = rows_added
        db.session.commit()

        return _success(rows_added=rows_added)
    except http_requests.exceptions.RequestException as e:
        return _error(f"Sociovia CRM connection error: {str(e)}")
    except Exception as e:
        db.session.rollback()
        logger.exception("Sociovia import error")
        return _error(f"Sociovia import error: {str(e)}")
