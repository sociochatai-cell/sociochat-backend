import logging
import os
import json
import glob
from datetime import datetime, timezone, timedelta
from flask import Blueprint, request, jsonify

from models import db
from .models import WhatsAppAccount
from .drip_models import WhatsAppDripCampaign, WhatsAppDripStep, WhatsAppDripEnrollment, WhatsAppDataset, WhatsAppDatasetRow
from .flow_access import require_account_access
from rate_limit.decorator import rate_limit

logger = logging.getLogger(__name__)

drip_bp = Blueprint("drip", __name__, url_prefix="/api/whatsapp")


def get_sheets_credentials():
    """
    Get Google Sheets credentials from environment variable or file.
    Returns tuple: (creds_data_dict, error_message)
    If successful: (dict, None)
    If failed: (None, error_string)
    """
    # Priority 1: Check env variables for JSON string (supports multiple names)
    env_vars_to_check = [
        "GOOGLE_SHEETS_ACCOUNT_JSON",
        "SERVICE_ACCOUNT_JSON",  # Common alternative name
        "GOOGLE_SERVICE_ACCOUNT_JSON"
    ]
    
    for env_var in env_vars_to_check:
        json_str = os.environ.get(env_var, "")
        if json_str:
            try:
                creds_data = json.loads(json_str)
                logger.info(f"Using Google Sheets credentials from {env_var} env var")
                return creds_data, None
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse {env_var}: {e}")
                return None, f"Invalid JSON in {env_var}: {e}"
            
    # Priority 2: Check for file paths
    possible_paths = [
        os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", ""),
        "service_account.json",
        "angular-sorter-473216-k8-835a67a5574c.json",
    ]
    possible_paths.extend(glob.glob("*-k8-*.json"))
    
    for path in possible_paths:
        if path and os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    creds_data = json.load(f)
                logger.info(f"Using Google Sheets credentials from file: {path}")
                return creds_data, None
            except Exception as e:
                logger.error(f"Failed to read {path}: {e}")
                continue
    
    return None, "No Google Sheets credentials configured. Set GOOGLE_SHEETS_ACCOUNT_JSON in .env"


def get_gspread_client():
    """
    Get an authorized gspread client.
    Returns tuple: (gspread_client, error_message)
    If successful: (client, None)
    If failed: (None, error_string)
    """
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        return None, "gspread not installed. Run: pip install gspread"
    
    creds_data, error = get_sheets_credentials()
    if error:
        return None, error
    
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly"
        ]
        creds = Credentials.from_service_account_info(creds_data, scopes=scopes)
        gc = gspread.authorize(creds)
        return gc, None
    except Exception as e:
        logger.exception(f"Failed to authorize gspread: {e}")
        return None, f"Failed to authorize: {e}"


def _clean_row_data(row_data: dict) -> dict:
    """
    Clean a row dict: trim whitespace from keys AND values.
    Ensures consistent column names and data across all import sources.
    """
    cleaned = {}
    for key, val in row_data.items():
        clean_key = key.strip() if isinstance(key, str) else key
        if isinstance(val, str):
            cleaned[clean_key] = val.strip()
        else:
            cleaned[clean_key] = val
    return cleaned


def normalize_phone_number(phone_value) -> str:
    """
    Normalize phone number from various formats to a clean format.
    Delegates to the canonical normalize_phone_robust() from utils.
    
    Returns: normalized phone string or empty string if invalid
    """
    from .utils import normalize_phone_robust
    
    result = normalize_phone_robust(phone_value)
    return result if result else ""


def _get_crm_models():
    from flask import current_app
    return getattr(current_app, "crm_models", None)


def _crm_workspace_id_for_account(account: WhatsAppAccount):
    ws = account.workspace_id
    if ws is None or ws == "":
        return None
    try:
        return int(ws)
    except (TypeError, ValueError):
        return ws


def _enrolled_phones_for_campaign(campaign_id: int) -> set:
    phones = set()
    enrollments = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id).all()
    for enrollment in enrollments:
        norm = normalize_phone_number(enrollment.phone_number or "")
        if norm:
            phones.add(norm)
    return phones


def _serialize_crm_record(record, enrolled_phones: set, include_without_phone: bool = False):
    raw_phone = record.phone or ""
    norm_phone = normalize_phone_number(raw_phone) if raw_phone else ""
    if not include_without_phone and not raw_phone.strip():
        return None
    return {
        "id": str(record.id),
        "name": record.name,
        "phone": raw_phone,
        "phone_normalized": norm_phone or None,
        "email": getattr(record, "email", None),
        "company": getattr(record, "company", None),
        "status": getattr(record, "status", None),
        "whatsapp_ready": bool(norm_phone),
        "already_enrolled": norm_phone in enrolled_phones if norm_phone else False,
        "created_at": record.created_at.isoformat() if getattr(record, "created_at", None) else None,
    }


def _crm_audience_stats(leads, contacts, enrolled_phones: set):
    leads_with_phone = sum(1 for r in leads if (r.phone or "").strip())
    contacts_with_phone = sum(1 for r in contacts if (r.phone or "").strip())

    available_phones = set()
    for record in leads + contacts:
        norm = normalize_phone_number(record.phone or "")
        if norm:
            available_phones.add(norm)

    already_enrolled = len(available_phones & enrolled_phones)
    estimated_enrollable = len(available_phones - enrolled_phones)

    return {
        "leads_with_phone": leads_with_phone,
        "contacts_with_phone": contacts_with_phone,
        "total_available": leads_with_phone + contacts_with_phone,
        "suppressed": 0,
        "already_enrolled": already_enrolled,
        "estimated_enrollable": estimated_enrollable,
    }


# ============================================================
# Campaign Management
# ============================================================

@drip_bp.route("/accounts/<int:account_id>/drip-campaigns", methods=["GET"])
@require_account_access
def list_campaigns(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """List drip campaigns (excludes bulk campaigns with trigger_type='manual')."""
    try:
        # Exclude bulk campaigns (trigger_type='manual') - those are shown in /dashboard/whatsapp/bulk
        campaigns = WhatsAppDripCampaign.query.filter_by(
            account_id=account_id,
            workspace_id=workspace_id
        ).filter(
            WhatsAppDripCampaign.trigger_type != "manual"
        ).order_by(WhatsAppDripCampaign.created_at.desc()).all()
        
        return jsonify({
            "success": True,
            "campaigns": [c.to_dict() for c in campaigns]
        })
    except Exception as e:
        logger.exception(f"Error listing drip campaigns: {e}")
        return jsonify({"error": "Failed to list campaigns"}), 500

@drip_bp.route("/accounts/<int:account_id>/drip-campaigns", methods=["POST"])
@rate_limit("whatsapp.drip.create")
@require_account_access
def create_campaign(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """Create a new drip campaign with steps."""
    data = request.get_json() or {}
    
    name = data.get("name")
    if not name:
        return jsonify({"error": "Name is required"}), 400
        
    try:
        trigger_type = data.get("trigger_type", "manual")
        
        campaign = WhatsAppDripCampaign(
            workspace_id=workspace_id,
            account_id=account_id,
            name=name,
            description=data.get("description"),
            trigger_type=trigger_type,
            status=data.get("status", "draft")
        )
        
        # Save Google Sheets config if applicable
        if trigger_type == "google_sheet_row":
            campaign.sheet_id = data.get("sheet_id")
            campaign.sheet_name = data.get("sheet_name", "Sheet1")
            campaign.phone_column = data.get("phone_column", "phone")
            campaign.last_synced_row = 0
        
        # Build column_mapping from step template_params
        # Works for: google_sheet_row, new_lead, new_contact
        # This maps step_X_Y -> field/column name
        if trigger_type in ("google_sheet_row", "new_lead", "new_contact"):
            column_mapping = {}
            fallback_values = {}
            steps_data = data.get("steps", [])
            for i, step_data in enumerate(steps_data):
                step_order = i + 1
                template_params = step_data.get("template_params", {})
                for var_num, var_config in template_params.items():
                    if isinstance(var_config, dict):
                        key = f"step_{step_order}_{var_num}"
                        source = var_config.get("source", "")
                        field_value = var_config.get("value", "")
                        fallback_val = var_config.get("fallback", "")
                        
                        if source == "field" and field_value:
                            # Map step_X_Y -> column/field name
                            column_mapping[key] = field_value
                            # Also store fallback if provided
                            if fallback_val:
                                fallback_values[key] = fallback_val
                        elif source == "static" and field_value:
                            # Static/default value - store as fallback
                            fallback_values[key] = field_value
            
            if column_mapping:
                campaign.column_mapping = column_mapping
                logger.info(f"Created column_mapping for campaign (trigger={trigger_type}): {column_mapping}")
            
            if fallback_values:
                campaign.fallback_values = fallback_values
                logger.info(f"Created fallback_values for campaign (trigger={trigger_type}): {fallback_values}")
        
        db.session.add(campaign)
        db.session.flush() # get ID
        
        # Add Steps
        steps_data = data.get("steps", [])
        for i, step_data in enumerate(steps_data):
            step = WhatsAppDripStep(
                campaign_id=campaign.id,
                step_order=i + 1,
                delay_seconds=step_data.get("delay_seconds", 0),
                template_name=step_data.get("template_name", ""),
                language=step_data.get("language", "en_US")
            )
            db.session.add(step)
            
        db.session.commit()
        
        return jsonify({
            "success": True,
            "campaign": campaign.to_dict(),
            "message": "Campaign created"
        }), 201
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error creating drip campaign: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>", methods=["PATCH"])
@require_account_access
def update_campaign(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Update an existing drip campaign (name, description, sheet config)."""
    data = request.get_json() or {}
    
    try:
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        # Update basic fields if provided
        if "name" in data:
            campaign.name = data["name"]
        if "description" in data:
            campaign.description = data["description"]
        if "trigger_type" in data:
            campaign.trigger_type = data["trigger_type"]
        if "status" in data:
            campaign.status = data["status"]
            
        # Update sheet configuration
        if "sheet_id" in data:
            campaign.sheet_id = data["sheet_id"]
        if "sheet_name" in data:
            campaign.sheet_name = data["sheet_name"]
        if "phone_column" in data:
            campaign.phone_column = data["phone_column"]
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "campaign": campaign.to_dict(),
            "message": "Campaign updated"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating campaign: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>", methods=["DELETE"])
@require_account_access
def delete_campaign(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Delete a drip campaign and all associated steps, enrollments, and syncs."""
    try:
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        campaign_name = campaign.name  # Store before delete
        
        # Delete associated enrollments first (use synchronize_session='fetch' for proper cascade)
        deleted_enrollments = WhatsAppDripEnrollment.query.filter_by(
            campaign_id=campaign_id
        ).delete(synchronize_session='fetch')
        logger.info(f"Deleted {deleted_enrollments} enrollments for campaign {campaign_id}")
        
        # Delete associated steps
        deleted_steps = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id
        ).delete(synchronize_session='fetch')
        logger.info(f"Deleted {deleted_steps} steps for campaign {campaign_id}")
        
        # Delete any sheet syncs (raw SQL since model may not exist)
        try:
            db.session.execute(
                db.text("DELETE FROM whatsapp_sheet_syncs WHERE campaign_id = :cid"),
                {"cid": campaign_id}
            )
            logger.info(f"Deleted sheet syncs for campaign {campaign_id}")
        except Exception as sync_err:
            logger.debug(f"No sheet syncs to delete or table doesn't exist: {sync_err}")
        
        # Flush to ensure deletes are executed before campaign delete
        db.session.flush()
        
        # Delete the campaign
        db.session.delete(campaign)
        db.session.commit()
        
        logger.info(f"Campaign {campaign_id} '{campaign_name}' deleted successfully")
        
        return jsonify({
            "success": True,
            "message": f"Campaign '{campaign_name}' deleted successfully"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error deleting campaign: {e}")
        return jsonify({"error": str(e)}), 500
# ============================================================
# Google Sheets Integration Endpoints
# ============================================================

@drip_bp.route("/sheets/config", methods=["GET"])
def get_sheets_config():
    """Return Google Sheets configuration including service account email."""
    creds_data, error = get_sheets_credentials()
    
    if error:
        return jsonify({
            "configured": False,
            "error": error
        })
    
    return jsonify({
        "configured": True,
        "service_account_email": creds_data.get("client_email", ""),
        "project_id": creds_data.get("project_id", "")
    })


@drip_bp.route("/datasets", methods=["GET"])
def list_datasets():
    """List available datasets for enrollment."""
    account_id = request.args.get("account_id")
    workspace_id = request.args.get("workspace_id")
    
    if not workspace_id and not account_id:
        return jsonify({"datasets": [], "error": "account_id or workspace_id required"}), 400
        
    if account_id and not workspace_id:
        account = WhatsAppAccount.query.get(account_id)
        if account:
            workspace_id = account.workspace_id
            
    if not workspace_id:
        return jsonify({"datasets": [], "error": "Workspace not found"}), 404
        
    # Use canonical datasets table (same as /dashboard/datasets)
    from .dataset_models import Dataset

    try:
        ws_id = int(workspace_id)
    except (TypeError, ValueError):
        return jsonify({"datasets": [], "error": "Invalid workspace_id"}), 400

    datasets = (
        Dataset.query.filter_by(workspace_id=ws_id)
        .order_by(Dataset.updated_at.desc())
        .all()
    )

    return jsonify({
        "datasets": [d.to_dict() for d in datasets],
        "count": len(datasets),
    })


@drip_bp.route("/workspaces/<int:workspace_id>/datasets", methods=["GET", "POST"])
def workspace_datasets(workspace_id: int):
    """List or create datasets for a workspace."""
    
    if request.method == "POST":
        data = request.get_json() or {}
        name = data.get("name", "").strip()
        if not name:
            return jsonify({"success": False, "error": "Dataset name is required"}), 400
        
        # Extract configuration
        columns = data.get("columns", [])
        description = data.get("description", "")
        source_type = data.get("source_type", "manual")
        source_config = data.get("source_config", {})
        rows_data = data.get("rows", [])
        
        try:
            dataset = WhatsAppDataset(
                workspace_id=str(workspace_id),
                name=name,
                description=description,
                columns=columns,
                source_type=source_type,
                source_config=source_config,
                total_rows=len(rows_data),
                sync_status="synced"
            )
            db.session.add(dataset)
            db.session.flush()  # Get the ID
            
            # Add rows if provided
            for row in rows_data:
                row_obj = WhatsAppDatasetRow(
                    dataset_id=dataset.id,
                    data=row.get("data", row) if isinstance(row, dict) else row
                )
                db.session.add(row_obj)
            
            db.session.commit()
            logger.info(f"Created dataset '{name}' with {len(rows_data)} rows for workspace {workspace_id}")
            
            return jsonify({
                "success": True,
                "data": dataset.to_dict()
            }), 201
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to create dataset: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
    
    # GET - List all datasets for workspace
    try:
        datasets = WhatsAppDataset.query.filter_by(workspace_id=str(workspace_id)).order_by(WhatsAppDataset.created_at.desc()).all()
        return jsonify({
            "success": True,
            "data": [d.to_dict() for d in datasets],
            "datasets": [d.to_dict() for d in datasets],  # Backward compat
            "total": len(datasets)
        })
    except Exception as e:
        logger.error(f"Failed to list datasets: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/datasets/<int:dataset_id>", methods=["GET", "PUT", "DELETE"])
def dataset_detail(dataset_id: int):
    """Get, update, or delete a specific dataset."""
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    if request.method == "DELETE":
        try:
            db.session.delete(dataset)
            db.session.commit()
            logger.info(f"Deleted dataset {dataset_id}")
            return jsonify({"success": True, "message": "Dataset deleted"})
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to delete dataset: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
    
    if request.method == "PUT":
        data = request.get_json() or {}
        try:
            if "name" in data:
                dataset.name = data["name"]
            if "description" in data:
                dataset.description = data["description"]
            if "columns" in data:
                dataset.columns = data["columns"]
            
            db.session.commit()
            logger.info(f"Updated dataset {dataset_id}")
            return jsonify({"success": True, "data": dataset.to_dict()})
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to update dataset: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
    
    # GET
    return jsonify({"success": True, "data": dataset.to_dict(include_rows=True)})


@drip_bp.route("/datasets/<int:dataset_id>/rows", methods=["GET", "POST"])
def dataset_rows(dataset_id: int):
    """List or add rows to a dataset."""
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    if request.method == "POST":
        data = request.get_json() or {}
        row_data = data.get("data", data)
        
        try:
            row = WhatsAppDatasetRow(dataset_id=dataset_id, data=row_data)
            db.session.add(row)
            dataset.total_rows = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).count() + 1
            db.session.commit()
            
            return jsonify({"success": True, "data": row.to_dict()}), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
    
    # GET - paginated
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    
    pagination = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).paginate(page=page, per_page=per_page, error_out=False)
    
    return jsonify({
        "success": True,
        "data": [r.to_dict() for r in pagination.items],
        "total": pagination.total,
        "page": page,
        "pages": pagination.pages
    })


@drip_bp.route("/datasets/<int:dataset_id>/rows/<int:row_id>", methods=["GET", "PUT", "DELETE"])
def dataset_row_detail(dataset_id: int, row_id: int):
    """Get, update, or delete a specific row."""
    row = WhatsAppDatasetRow.query.filter_by(id=row_id, dataset_id=dataset_id).first()
    if not row:
        return jsonify({"success": False, "error": "Row not found"}), 404
    
    if request.method == "DELETE":
        try:
            db.session.delete(row)
            dataset = WhatsAppDataset.query.get(dataset_id)
            if dataset:
                dataset.total_rows = max(0, dataset.total_rows - 1)
            db.session.commit()
            return jsonify({"success": True, "message": "Row deleted"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
    
    if request.method == "PUT":
        data = request.get_json() or {}
        try:
            row.data = data.get("data", data)
            db.session.commit()
            return jsonify({"success": True, "data": row.to_dict()})
        except Exception as e:
            db.session.rollback()
            return jsonify({"success": False, "error": str(e)}), 500
    
    return jsonify({"success": True, "data": row.to_dict()})


@drip_bp.route("/datasets/<int:dataset_id>/import-csv", methods=["POST"])
def dataset_import_csv(dataset_id: int):
    """Import rows from CSV file."""
    import csv
    import io
    
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400
    
    file = request.files["file"]
    
    try:
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        
        rows_added = 0
        columns = []
        
        for row in reader:
            cleaned = _clean_row_data(dict(row))
            if not columns:
                columns = list(cleaned.keys())
                dataset.columns = columns
            
            row_obj = WhatsAppDatasetRow(dataset_id=dataset_id, data=cleaned)
            db.session.add(row_obj)
            rows_added += 1
        
        dataset.total_rows = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).count()
        dataset.source_type = "csv"
        db.session.commit()
        
        logger.info(f"Imported {rows_added} rows into dataset {dataset_id}")
        return jsonify({
            "success": True,
            "rows_added": rows_added,
            "columns": columns,
            "data": dataset.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to import CSV: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/datasets/<int:dataset_id>/upload-mapped", methods=["POST"])
def dataset_upload_mapped(dataset_id: int):
    """Import CSV with column mapping (Multipart Form Data)."""
    import csv
    import io
    import json
    
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    if "file" not in request.files:
        return jsonify({"success": False, "error": "No file provided"}), 400
        
    file = request.files["file"]
    mapping_str = request.form.get("column_mapping", "{}")
    replace = request.form.get("replace", "false").lower() == "true"
    
    try:
        column_mapping = json.loads(mapping_str)
    except:
        column_mapping = {}
        
    try:
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        
        # Clear existing rows if replace is true
        if replace:
            WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).delete()
            
        rows_added = 0
        raw_cols = list(column_mapping.values()) if column_mapping else (reader.fieldnames or [])
        dataset.columns = [c.strip() if isinstance(c, str) else c for c in raw_cols]
        dataset.column_mapping = column_mapping
        
        for row in reader:
            # Remap keys based on mapping
            # Mapping is {csv_col: dataset_col}
            # We want stored data keyed by dataset_col
            row_data = {}
            for csv_col, val in row.items():
                target_col = column_mapping.get(csv_col, csv_col)
                row_data[target_col] = val
                
            row_obj = WhatsAppDatasetRow(dataset_id=dataset_id, data=_clean_row_data(row_data))
            db.session.add(row_obj)
            rows_added += 1
        
        dataset.total_rows = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).count() # Recount
        dataset.source_type = "csv"
        db.session.commit()
        
        return jsonify({
            "success": True,
            "rows_added": rows_added,
            "data": dataset.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to upload mapped CSV: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/datasets/<int:dataset_id>/import-sheets", methods=["POST"])
def dataset_import_sheets(dataset_id: int):
    """Import rows from Google Sheets."""
    import re
    import gspread
    
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    data = request.get_json() or {}
    sheet_id = data.get("sheet_url") or data.get("sheet_id")
    sheet_name = data.get("sheet_name", "Sheet1")
    replace = data.get("replace", False)
    column_mapping = data.get("column_mapping", {})
    
    if not sheet_id:
        return jsonify({"success": False, "error": "Sheet URL is required"}), 400
        
    gc, error = get_gspread_client()
    if error:
        return jsonify({"success": False, "error": error}), 500
        
    try:
        # Extract ID
        actual_sheet_id = sheet_id
        if "docs.google.com" in sheet_id:
            match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_id)
            if match:
                actual_sheet_id = match.group(1)
        
        spreadsheet = gc.open_by_key(actual_sheet_id)
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
        except gspread.exceptions.WorksheetNotFound:
            # Fallback to first sheet if name mismatch or default
            worksheet = spreadsheet.sheet1
            
        # Use UNFORMATTED_VALUE to get raw numbers (avoids 9.19E+11 string loss)
        try:
            all_values = worksheet.get(value_render_option='UNFORMATTED_VALUE')
        except Exception:
            all_values = worksheet.get_all_values()
        
        if not all_values:
            return jsonify({"success": False, "error": "Sheet is empty"}), 400
            
        headers = [h.strip() if isinstance(h, str) else h for h in all_values[0]]
        data_rows = all_values[1:]
        
        if replace:
            WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).delete()
            
        clean_mapped_cols = [v.strip() if isinstance(v, str) else v for v in column_mapping.values()] if column_mapping else headers
        dataset.columns = clean_mapped_cols
        dataset.source_type = "google_sheets"
        dataset.source_config = {
            "sheet_id": sheet_id,
            "sheet_name": sheet_name,
            "column_mapping": column_mapping
        }
        
        rows_added = 0
        for row in data_rows:
            # Create dict from row + headers
            row_dict = {}
            for i, h in enumerate(headers):
                if i < len(row):
                    row_dict[h] = row[i]
            
            # Map if needed
            final_data = {}
            if column_mapping:
                for src_col, target_col in column_mapping.items():
                    if src_col in row_dict:
                        final_data[target_col] = row_dict[src_col]
            else:
                final_data = row_dict
                
            row_obj = WhatsAppDatasetRow(dataset_id=dataset_id, data=_clean_row_data(final_data))
            db.session.add(row_obj)
            rows_added += 1
            
        dataset.total_rows = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).count() # Recount
        db.session.commit()
        
        return jsonify({
            "success": True,
            "rows_added": rows_added,
            "data": dataset.to_dict()
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Sheet import failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/datasets/<int:dataset_id>/import-crm", methods=["POST"])
def dataset_import_crm(dataset_id: int):
    """Import leads or contacts from CRM into a dataset."""
    # START FIX: Access dynamic models from current_app
    from flask import current_app
    CrmLead = current_app.crm_models.get("Lead")
    CrmContact = current_app.crm_models.get("Contact")
    
    # Needs WhatsApp models too for 'whatsapp_contacts' source
    from .models import WhatsAppAccount, WhatsAppConversation
    
    dataset = WhatsAppDataset.query.get(dataset_id)
    if not dataset:
        return jsonify({"success": False, "error": "Dataset not found"}), 404
    
    data = request.get_json() or {}
    entity_type = data.get("source", data.get("type", "leads"))
    workspace_id = str(dataset.workspace_id)
    replace = data.get("replace", False)
    
    try:
        from sqlalchemy import cast, String
        records = []
        columns = []
        
        if entity_type == "whatsapp_contacts":
            # Fetch from Inbox Conversations
            # Find accounts for this workspace
            accounts = WhatsAppAccount.query.filter_by(workspace_id=workspace_id).all()
            account_ids = [a.id for a in accounts]
            
            if account_ids:
                convs = WhatsAppConversation.query.filter(WhatsAppConversation.account_id.in_(account_ids)).all()
                records = convs
                columns = ["name", "phone", "status", "last_message_at"]
            else:
                records = []
                
        elif entity_type in ["lead", "leads"]:
            if not CrmLead: return jsonify({"success": False, "error": "CRM Lead model missing"}), 500
            records = CrmLead.query.filter(cast(CrmLead.workspace_id, String) == workspace_id).all()
            columns = ["name", "email", "phone", "company", "source", "status", "job_title", "value"]
        else:
            if not CrmContact: return jsonify({"success": False, "error": "CRM Contact model missing"}), 500
            records = CrmContact.query.filter(cast(CrmContact.workspace_id, String) == workspace_id).all()
            columns = ["name", "email", "phone", "company", "role"]
        
        if replace:
            WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).delete()
            
        rows_added = 0
        dataset.columns = columns
        
        for record in records:
            row_data = {}
            
            if entity_type == "whatsapp_contacts":
                # Manual mapping for Conversation object
                # record is WhatsAppConversation
                row_data["name"] = record.user_name or ""
                row_data["phone"] = record.user_phone
                row_data["status"] = record.status
                row_data["last_message_at"] = record.last_message_at.isoformat() if record.last_message_at else ""
            else:
                # Reflection for CRM objects
                for col in columns:
                    val = getattr(record, col, None)
                    row_data[col] = str(val) if val is not None else ""
            
            row_obj = WhatsAppDatasetRow(dataset_id=dataset_id, data=_clean_row_data(row_data))
            db.session.add(row_obj)
            rows_added += 1
        
        dataset.total_rows = WhatsAppDatasetRow.query.filter_by(dataset_id=dataset_id).count() # Recount
        dataset.source_type = "crm"
        dataset.source_config = {"entity_type": entity_type}
        db.session.commit()
        
        logger.info(f"Imported {rows_added} {entity_type} into dataset {dataset_id}")
        return jsonify({
            "success": True,
            "rows_added": rows_added,
            "columns": columns,
            "data": dataset.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        logger.error(f"Failed to import CRM data: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/workspaces/<int:workspace_id>/contacts", methods=["GET", "POST"])
def list_workspace_contacts(workspace_id: int):
    """
    List or create WhatsApp contacts (Conversations) for a workspace.
    Source: WhatsAppConversation table (matches Inbox).
    Applies strict filtering: Unique numbers only, Valid numbers only, Name required.
    """
    from .models import WhatsAppAccount, WhatsAppConversation
    from datetime import datetime, timezone
    
    # Resolve accounts for this workspace
    # workspace_id is stored as String in WhatsAppAccount
    accounts = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).all()
    account_ids = [a.id for a in accounts]
    
    if not account_ids:
        # No WhatsApp accounts linked to this workspace
        if request.method == "POST":
             return jsonify({"success": False, "error": "No WhatsApp account linked to this workspace. Please connect one first."}), 400
        return jsonify({
            "success": True, 
            "data": [], 
            "total": 0, 
            "page": 1, 
            "pages": 1,
            "message": "No WhatsApp accounts found for this workspace"
        })
    
    # Helper for phone number normalization (assuming it's defined elsewhere or added)
    def normalize_phone_number(phone_str):
        return "".join(filter(str.isdigit, phone_str))

    # Helper for serialization (Conversation -> Contact View)
    def serialize_conversation_as_contact(conv):
        # Calculate window status
        is_open = conv.is_session_open
        
        return {
            "id": conv.id, # Using conversation ID as contact ID for this view
            "conversation_id": conv.id,
            "account_id": conv.account_id,
            "workspace_id": str(workspace_id),
            "name": conv.user_name or conv.user_phone,
            "phone": conv.user_phone,
            "phone_display": conv.user_phone, # Should format this better if possible
            "phone_normalized": normalize_phone_number(conv.user_phone) or conv.user_phone,
            "email": "", # Not available in conversation
            "company": "",
            "role": "WhatsApp User",
            "status": "opted_in" if conv.last_inbound_at else "unknown", # Heuristic
            "avatar": None,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "last_contacted": conv.last_message_at.isoformat() if conv.last_message_at else None,
            "unread_count": conv.unread_count,
            "has_conversation": True,
            "window_open": is_open,
            "session_expires_at": conv.session_expires_at.isoformat() if conv.session_expires_at else None,
        }

    # Helper: Get Unique, Valid, Named Contacts
    def get_unique_valid_contacts(acc_ids):
        # 1. Fetch all
        all_convs = WhatsAppConversation.query.filter(WhatsAppConversation.account_id.in_(acc_ids)).all()
        
        # 2. Filter & Dedupe
        phone_map = {} # normalized_phone -> {conv, has_name}
        
        for c in all_convs:
            raw_phone = c.user_phone
            # Use global robust normalizer
            norm_phone = normalize_phone_number(raw_phone)
            
            # Filter invalid/short numbers (e.g. 93909)
            if not norm_phone or len(norm_phone) < 10:
                continue
                
            name = c.user_name
            # Check if name exists and is not just the phone number
            has_real_name = name and name.strip() and name.strip() != raw_phone and name.strip() != norm_phone
            
            # Deduplication logic
            if norm_phone not in phone_map:
                phone_map[norm_phone] = {"conv": c, "has_real_name": has_real_name}
            else:
                # If current has real name and existing doesn't, upgrade
                if has_real_name and not phone_map[norm_phone]["has_real_name"]:
                    phone_map[norm_phone] = {"conv": c, "has_real_name": has_real_name}
                # If both have real name, maybe prefer most recent? (optional refinement)
                elif has_real_name and phone_map[norm_phone]["has_real_name"]:
                    # Prefer one with more recent activity
                    curr_date = c.last_message_at or datetime.min.replace(tzinfo=timezone.utc)
                    exist_date = phone_map[norm_phone]["conv"].last_message_at or datetime.min.replace(tzinfo=timezone.utc)
                    if curr_date > exist_date:
                        phone_map[norm_phone] = {"conv": c, "has_real_name": has_real_name}

        # 3. Flatten and Strict Filter (Must have name)
        results = []
        for data in phone_map.values():
            if data["has_real_name"]:
                results.append(data["conv"])
        
        # Sort by Name
        results.sort(key=lambda x: (x.user_name or "").lower())
        return results

    if request.method == "POST":
        try:
            data = request.get_json() or {}
            phone = data.get("phone", "").strip()
            name = data.get("name", "").strip()
            
            if not phone:
                return jsonify({"success": False, "error": "Phone number is required"}), 400
                
            # Use global normalizer
            phone_normalized = normalize_phone_number(phone)
            if not phone_normalized or len(phone_normalized) < 10:
                 return jsonify({"success": False, "error": "Invalid phone number"}), 400
                 
            # Pick the primary account (first one for now, or could pass account_id in body)
            target_account_id = data.get("account_id")
            if target_account_id:
                target_account_id = int(target_account_id)
                if target_account_id not in account_ids:
                     return jsonify({"success": False, "error": "Invalid account_id for this workspace"}), 403
            else:
                target_account_id = account_ids[0]
            
            # Check for existing conversation in this account
            existing = WhatsAppConversation.query.filter_by(
                account_id=target_account_id,
                user_phone=phone_normalized
            ).first()
            
            if existing:
                # Update name if provided
                if name and name != existing.user_name:
                    existing.user_name = name
                    db.session.commit()
                return jsonify({"success": True, "data": serialize_conversation_as_contact(existing), "message": "Contact already exists"})
            
            # Create new conversation
            new_conv = WhatsAppConversation(
                account_id=target_account_id,
                user_phone=phone_normalized,
                user_name=name or phone_normalized,
                status="open",
                unread_count=0,
                created_at=datetime.now(timezone.utc),
                last_message_at=datetime.now(timezone.utc)
            )
            # Cannot open 24h window manually without inbound, so session_expires_at is None/past
            
            db.session.add(new_conv)
            db.session.commit()
            
            return jsonify({"success": True, "data": serialize_conversation_as_contact(new_conv)}), 201
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Failed to create whatsapp contact: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    # GET: List filtered contacts
    try:
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 50, type=int)
        q = request.args.get("q", "").strip().lower()
        
        # Get filtered list (in-memory)
        valid_conversations = get_unique_valid_contacts(account_ids)
        
        # Apply Search
        if q:
            valid_conversations = [
                c for c in valid_conversations 
                if q in (c.user_name or "").lower() or q in (c.user_phone or "")
            ]
            
        total = len(valid_conversations)
        start = (page - 1) * per_page
        end = start + per_page
        
        sliced_items = valid_conversations[start:end]
        
        return jsonify({
            "success": True,
            "data": [serialize_conversation_as_contact(c) for c in sliced_items],
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page
        })
    except Exception as e:
        logger.error(f"Failed to list whatsapp contacts: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/crm/preview", methods=["POST"])
def preview_crm_data():
    """Preview CRM lead/contact data for enrollment mapping."""
    data = request.get_json() or {}
    # Support both 'type' and 'source' parameters (frontend uses 'source')
    entity_type = data.get("source") or data.get("type", "leads")
    workspace_id = data.get("workspace_id")
    limit = data.get("limit", 50)
    
    # Normalize entity type
    if entity_type in ("leads", "lead"):
        entity_type = "lead"
    elif entity_type in ("contacts", "contact"):
        entity_type = "contact"
    elif entity_type == "whatsapp_contacts":
        pass # Handle below

    
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id is required"}), 400
    
    try:
        from flask import current_app
        
        # Try to get CRM models
        if hasattr(current_app, 'crm_models'):
            if entity_type == "whatsapp_contacts":
                from .models import WhatsAppAccount, WhatsAppConversation
                # Fetch from Inbox Conversations
                accounts = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).all()
                account_ids = [a.id for a in accounts]
                
                fields = ["name", "phone", "status", "last_message_at"]
                preview_rows = []
                total = 0
                
                if account_ids:
                    # Use same fetching/filtering logic
                    all_convs = WhatsAppConversation.query.filter(WhatsAppConversation.account_id.in_(account_ids)).all()
                    
                    # Filter & Dedupe using global normalizer
                    phone_map = {} 
                    for c in all_convs:
                        norm_phone = normalize_phone_number(c.user_phone) 
                        if not norm_phone or len(norm_phone) < 10: continue
                        
                        name = c.user_name
                        has_real_name = name and name.strip() and name.strip() != c.user_phone and name.strip() != norm_phone
                        
                        if norm_phone not in phone_map:
                            phone_map[norm_phone] = {"conv": c, "has_real_name": has_real_name}
                        elif has_real_name and not phone_map[norm_phone]["has_real_name"]:
                            phone_map[norm_phone] = {"conv": c, "has_real_name": has_real_name}
                    
                    valid_convs = [d["conv"] for d in phone_map.values() if d["has_real_name"]]
                    valid_convs.sort(key=lambda x: (x.user_name or "").lower())
                    
                    total = len(valid_convs)
                    # Apply limit
                    sliced = valid_convs[:limit]
                    
                    for c in sliced:
                        preview_rows.append({
                            "name": c.user_name or "",
                            "phone": c.user_phone,
                            "status": c.status,
                            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else ""
                        })
                
                return jsonify({
                    "success": True,
                    "fields": fields,
                    "headers": fields,
                    "preview_rows": preview_rows,
                    "total_records": total,
                    "type": "whatsapp_contacts"
                })
            
            if entity_type == "lead":
                Lead = current_app.crm_models.get("Lead")
                if Lead:
                    leads = Lead.query.filter_by(workspace_id=workspace_id).limit(limit).all()
                    
                    # Build fields list and preview_rows
                    fields = ["name", "email", "phone", "company", "source", "status"]
                    preview_rows = []
                    for lead in leads:
                        preview_rows.append({
                            "name": getattr(lead, 'name', '') or '',
                            "email": getattr(lead, 'email', '') or '',
                            "phone": getattr(lead, 'phone', '') or '',
                            "company": getattr(lead, 'company', '') or '',
                            "source": getattr(lead, 'source', '') or '',
                            "status": getattr(lead, 'status', '') or '',
                        })
                    
                    return jsonify({
                        "success": True,
                        "fields": fields,
                        "headers": fields,  # Include both for compatibility
                        "preview_rows": preview_rows,
                        "total_records": Lead.query.filter_by(workspace_id=workspace_id).count(),
                        "type": "lead"
                    })
            
            elif entity_type == "contact":
                Contact = current_app.crm_models.get("Contact")
                if Contact:
                    contacts = Contact.query.filter_by(workspace_id=workspace_id).limit(limit).all()
                    
                    fields = ["name", "email", "phone", "company", "role"]
                    preview_rows = []
                    for contact in contacts:
                        preview_rows.append({
                            "name": getattr(contact, 'name', '') or '',
                            "email": getattr(contact, 'email', '') or '',
                            "phone": getattr(contact, 'phone', '') or '',
                            "company": getattr(contact, 'company', '') or '',
                            "role": getattr(contact, 'role', '') or '',
                        })
                    
                    return jsonify({
                        "success": True,
                        "fields": fields,
                        "headers": fields,
                        "preview_rows": preview_rows,
                        "total_records": Contact.query.filter_by(workspace_id=workspace_id).count(),
                        "type": "contact"
                    })
            
            elif entity_type == "whatsapp_contacts":
                from .models import WhatsAppAccount, WhatsAppConversation
                # Fetch from Inbox Conversations
                accounts = WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id)).all()
                account_ids = [a.id for a in accounts]
                
                fields = ["name", "phone", "status", "last_message_at"]
                preview_rows = []
                total = 0
                
                if account_ids:
                    convs = WhatsAppConversation.query.filter(WhatsAppConversation.account_id.in_(account_ids)).limit(limit).all()
                    total = WhatsAppConversation.query.filter(WhatsAppConversation.account_id.in_(account_ids)).count()
                    for c in convs:
                        preview_rows.append({
                            "name": c.user_name or "",
                            "phone": c.user_phone,
                            "status": c.status,
                            "last_message_at": c.last_message_at.isoformat() if c.last_message_at else ""
                        })
                
                return jsonify({
                    "success": True,
                    "fields": fields,
                    "headers": fields,
                    "preview_rows": preview_rows,
                    "total_records": total,
                    "type": "whatsapp_contacts"
                })
        
        # Fallback if CRM models not available
        return jsonify({
            "success": True,
            "fields": ["name", "email", "phone", "company", "source", "status"],
            "headers": ["name", "email", "phone", "company", "source", "status"],
            "preview_rows": [],
            "total_records": 0,
            "type": entity_type,
            "message": "CRM models not initialized"
        })
        
    except Exception as e:
        logger.exception(f"CRM preview error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/csv/preview", methods=["POST"])
def preview_csv_data():
    """Preview uploaded CSV data for column mapping."""
    if 'file' not in request.files:
        return jsonify({"success": False, "error": "No file uploaded"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"success": False, "error": "No file selected"}), 400
    
    try:
        import csv
        import io
        
        # Read CSV content
        content = file.read().decode('utf-8-sig')  # Handle BOM
        reader = csv.reader(io.StringIO(content))
        
        rows = list(reader)
        if not rows:
            return jsonify({"success": False, "error": "Empty CSV file"}), 400
        
        columns = [c.strip() for c in rows[0]]  # Trim column headers
        # Build preview_rows as list of dicts
        preview_rows = []
        for row in rows[1:6]:
            row_dict = {}
            for i, col in enumerate(columns):
                val = row[i] if i < len(row) else ''
                row_dict[col] = val.strip() if isinstance(val, str) else val
            preview_rows.append(row_dict)
        
        return jsonify({
            "success": True,
            "columns": columns,
            "headers": columns,  # Include both for compatibility
            "preview_rows": preview_rows,
            "total_rows": len(rows) - 1  # Exclude header
        })
        
    except Exception as e:
        logger.exception(f"CSV preview error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/variables", methods=["GET"])
@require_account_access
def get_campaign_variables(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Get variable schema for a drip campaign - describes what variables each step needs."""
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    
    # Get all steps
    steps = WhatsAppDripStep.query.filter_by(campaign_id=campaign_id).order_by(WhatsAppDripStep.step_order).all()
    
    step_schemas = []
    union_field_names = set()
    
    from .models import WhatsAppTemplate  # Import here to avoid circular dependencies if any
    
    for step in steps:
        # Parse template to find variable count
        # Priority: 
        # 1. Fetch from WhatsAppTemplate table (authoritative)
        # 2. Use step.variable_count if stored
        # 3. Default to 0 (safest)
        
        variable_count = 0
        template_def = None
        if step.template_name:
            # Try to find the template definition
            template_def = WhatsAppTemplate.query.filter_by(
                name=step.template_name, 
                account_id=account_id,
                status='APPROVED'
            ).first()
            
            if template_def:
                variable_count = template_def.variable_count
            elif hasattr(step, 'variable_count') and step.variable_count is not None:
                variable_count = step.variable_count
            else:
                # Fallback for templates not in DB (e.g. test templates)
                # Default to 3 to ensure UI shows inputs.
                # 'order_confirmation' typically has 3 variables.
                variable_count = 3
        
        # Prepare texts
        body_text = None
        header_text = None
        footer_text = None
        
        if template_def:
            body_text = template_def.body_text
            header_text = template_def.header_text
            footer_text = template_def.footer_text
            
        # If body_text is still None (fallback case or empty in DB), generate a placeholder with variables
        if not body_text:
            var_placeholders = [f"{{{{{i}}}}}" for i in range(1, variable_count + 1)]
            body_text = f"Template: {step.template_name}\n\n(Preview text unavailable, showing variables:)\n" + " ".join(var_placeholders)

        step_info = {
            "step_order": step.step_order,
            "template_name": step.template_name,
            "variable_count": variable_count,
            "delay_seconds": step.delay_seconds,
            "body_text": body_text,
            "header_text": header_text,
            "footer_text": footer_text
        }
        
        # Check for named parameters in body_text if available
        # This supports hybrid {{1}} or {{name}} detection
        variable_mapping = {}
        if body_text:
            import re
            # Find all {{...}} patterns
            matches = re.findall(r'\{\{([^}]+)\}\}', body_text)
            
            # Filter valid vars (exclude internal like {{1}} if it's strictly just that, 
            # actually we want to map sequential 1..N to these)
            unique_vars = []
            for m in matches:
                clean_var = m.strip()
                if clean_var not in unique_vars:
                    unique_vars.append(clean_var)
            
            # Logic: If variables are NOT digits (e.g. "name", "order_id"), map them
            # If variables ARE digits "1", "2", we don't strictly need mapping but can provide it for alias
            if unique_vars:
                # Update count to match actual found variables if > 0
                # This overrides the stored count which might be stale
                variable_count = len(unique_vars)
                step_info["variable_count"] = variable_count
                
                for idx, var_name in enumerate(unique_vars):
                    # Map input index "1" (string) -> actual var name "name"
                    variable_mapping[str(idx + 1)] = var_name
                    
                step_info["variable_mapping"] = variable_mapping
        
        logger.info(f"Step {step.step_order}: Template {step.template_name} -> VarCount: {variable_count}, Mapping: {variable_mapping}")
        
        # Add step field names to union
        for i in range(1, variable_count + 1):
            union_field_names.add(f"step_{step.step_order}_{i}")
        
        step_schemas.append(step_info)
    
    logger.info(f"Campaign Variables Response: {len(step_schemas)} steps, Union Fields: {list(union_field_names)}")
    
    # Frontend expects { "schema": { ... } }
    return jsonify({
        "schema": {
            "campaign_id": campaign_id,
            "campaign_name": campaign.name,
            "trigger_type": campaign.trigger_type,
            "column_mapping": campaign.column_mapping,
            "steps": step_schemas,
            "union_field_names": list(union_field_names)
        }
    })


@drip_bp.route("/sheets/preview", methods=["GET", "POST"])
def preview_sheet():
    """Preview data from a Google Sheet."""
    import re
    import gspread
    
    # Support both GET query params and POST JSON body
    if request.method == "POST":
        data = request.get_json() or {}
        # Support both sheet_id and sheet_url (frontend uses sheet_url)
        sheet_id = data.get("sheet_url") or data.get("sheet_id", "")
        sheet_name = data.get("sheet_name", "Sheet1")
        limit = data.get("limit", 5)
    else:
        sheet_id = request.args.get("sheet_url") or request.args.get("sheet_id", "")
        sheet_name = request.args.get("sheet_name", "Sheet1")
        limit = request.args.get("limit", 5, type=int)
    
    if not sheet_id:
        return jsonify({"success": False, "error": "sheet_url or sheet_id is required"}), 400
    
    gc, error = get_gspread_client()
    if error:
        return jsonify({"error": error}), 500
    
    try:
        # Extract sheet ID from URL if needed
        actual_sheet_id = sheet_id
        if "docs.google.com" in sheet_id:
            match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_id)
            if match:
                actual_sheet_id = match.group(1)
        
        spreadsheet = gc.open_by_key(actual_sheet_id)
        if sheet_name:
            worksheet = spreadsheet.worksheet(sheet_name)
        else:
            # Default to first sheet if not specified
            worksheet = spreadsheet.sheet1
        
        # Get headers and sample rows
        # Use UNFORMATTED_VALUE to get raw numbers instead of scientific notation strings (e.g. 9.16E+11)
        all_values = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
        
        if not all_values:
            return jsonify({
                "success": True,
                "headers": [],
                "preview_rows": [],
                "total_rows": 0
            })
        
        headers = [str(h) for h in (all_values[0] if all_values else [])]
        data_rows = all_values[1:limit+1] if len(all_values) > 1 else []
        
        # Build preview_rows as list of dicts
        preview_rows = []
        for row in data_rows:
            row_dict = {}
            for i, col in enumerate(headers):
                val = row[i] if i < len(row) else ''
                # Clean up numbers (ensure string, remove .0)
                if isinstance(val, (int, float)):
                    val = str(val)
                    if val.endswith('.0'):
                        val = val[:-2]
                row_dict[col] = str(val)
            preview_rows.append(row_dict)
        
        return jsonify({
            "success": True,
            "headers": headers,
            "preview_rows": preview_rows,
            "rows": data_rows,  # Include raw rows for compatibility
            "total_rows": len(all_values) - 1  # Exclude header
        })
        
    except gspread.exceptions.SpreadsheetNotFound:
        return jsonify({
            "success": False,
            "error": "Spreadsheet not found. Make sure it's shared with the service account email."
        }), 404
    except gspread.exceptions.WorksheetNotFound:
        return jsonify({
            "success": False,
            "error": f"Worksheet '{sheet_name}' not found in the spreadsheet."
        }), 404
    except Exception as e:
        logger.exception(f"Error previewing sheet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@drip_bp.route("/sheets/sync/<int:campaign_id>", methods=["POST"])
@rate_limit("whatsapp.sheets.sync")
def sync_sheet_campaign(campaign_id: int):
    """
    Manually trigger sync from Google Sheet for a drip campaign.
    Fetches new rows from the linked sheet and enrolls them into the campaign.
    """
    import re
    import gspread
    
    try:
        campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
        
        # Check if campaign has Google Sheet trigger
        if campaign.trigger_type != 'google_sheet_row':
            return jsonify({
                "success": False,
                "error": "Campaign is not linked to a Google Sheet"
            }), 400
        
        if not campaign.sheet_id:
            return jsonify({
                "success": False,
                "error": "No Google Sheet URL configured for this campaign"
            }), 400
        
        gc, error = get_gspread_client()
        if error:
            return jsonify({
                "success": False,
                "error": error
            }), 500
        
        # Open the spreadsheet
        try:
            # Sheet ID can be a full URL or just the ID
            sheet_id = campaign.sheet_id
            if "docs.google.com" in sheet_id:
                # Extract ID from URL
                match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_id)
                if match:
                    sheet_id = match.group(1)
            
            spreadsheet = gc.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet(campaign.sheet_name or "Sheet1")
        except gspread.exceptions.SpreadsheetNotFound:
            return jsonify({
                "success": False,
                "error": "Spreadsheet not found. Make sure it's shared with the service account."
            }), 404
        except gspread.exceptions.WorksheetNotFound:
            return jsonify({
                "success": False,
                "error": f"Worksheet '{campaign.sheet_name}' not found in the spreadsheet."
            }), 404
        
        # Use get() with UNFORMATTED_VALUE to get raw numeric values instead of formatted display
        # This preserves full precision for large numbers (avoids 9.19E+11 -> 919000000000 precision loss)
        try:
            all_values = worksheet.get(value_render_option='UNFORMATTED_VALUE')
        except Exception as e:
            logger.warning(f"Failed to get unformatted values, falling back to get_all_values(): {e}")
            all_values = worksheet.get_all_values()
        
        if len(all_values) <= 1:  # Only header or empty
            return jsonify({
                "success": True,
                "message": "No data in sheet",
                "enrolled": 0,
                "skipped": 0
            })
        
        # First row is headers
        headers = all_values[0]
        data_rows = all_values[1:]
        
        # Find phone column index
        phone_col_name = campaign.phone_column or "phone"
        phone_col_idx = None
        for i, h in enumerate(headers):
            if h.lower().strip() == phone_col_name.lower().strip():
                phone_col_idx = i
                break
        
        if phone_col_idx is None:
            return jsonify({
                "success": False,
                "error": f"Column '{phone_col_name}' not found in sheet. Available: {headers}"
            }), 400
        
        # Get first step for enrollment timing
        first_step = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id,
            step_order=1
        ).first()
        
        enrolled = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        
        # Process rows after last_synced_row
        start_row = campaign.last_synced_row or 0
        
        for idx, row in enumerate(data_rows):
            if idx < start_row:
                continue  # Skip already processed rows
            
            # Get raw phone value (preserved as string via get_all_values)
            raw_phone = row[phone_col_idx] if phone_col_idx < len(row) else ""
            
            # Normalize phone number (handles any format, adds 91 for 10-digit)
            phone = normalize_phone_number(raw_phone)
            
            if not phone:
                logger.debug(f"Skipping row {idx+2}: invalid phone '{raw_phone}'")
                skipped += 1
                continue
            
            # Check if already enrolled
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id,
                phone_number=phone
            ).filter(
                WhatsAppDripEnrollment.status.in_(["active", "completed"])
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            # Build row data as dict (header -> value)
            row_data = {}
            for i, header in enumerate(headers):
                if i < len(row):
                    row_data[header] = row[i]
            
            # Transform row_data to step_X_Y format using column_mapping
            # column_mapping: { "step_1_1": "Name", "step_1_2": "OrderID", ... }
            # We need to map: step_1_1 -> value from "Name" column
            variables = {}
            
            # 1. Apply column mappings
            if campaign.column_mapping:
                for step_key, col_name in campaign.column_mapping.items():
                    val = row_data.get(col_name)
                    if val is not None and str(val).strip():
                        variables[step_key] = str(val)
                logger.debug(f"Transformed variables: {variables} from row_data columns: {list(row_data.keys())}")
            else:
                # No column mapping - use raw row data (backwards compatibility)
                # drip_engine will try to use column names directly 
                variables = row_data
            
            # 2. Apply fallback values for missing keys
            if campaign.fallback_values:
                for step_key, default_val in campaign.fallback_values.items():
                    if step_key not in variables or not variables.get(step_key):
                        variables[step_key] = str(default_val)
                        logger.debug(f"Applied fallback for {step_key}: {default_val}")
            
            # Calculate next run time
            next_run = None
            if first_step:
                next_run = now_utc + timedelta(seconds=first_step.delay_seconds)
            
            # Create enrollment with transformed variables
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=phone,
                phone_original=str(raw_phone),  # Store original for debugging
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,  # Store transformed step_X_Y mapping
                variables_source="google_sheet",
                variables_row_index=idx + 2,  # +2 for 1-indexed and header row
                variables_sheet_id=sheet_id,
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled += 1
            
            logger.info(f"Enrolled {phone} (row {idx+2}) with variables: {list(variables.keys())}")
        
        # Update sync tracking
        campaign.enrolled_count += enrolled
        campaign.last_synced_row = len(data_rows)  # Mark all rows as processed
        
        db.session.commit()
        
        logger.info(f"Synced campaign {campaign_id}: {enrolled} enrolled, {skipped} skipped")
        
        return jsonify({
            "success": True,
            "message": f"Synced {enrolled} new contacts",
            "enrolled": enrolled,
            "skipped": skipped
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error syncing sheet for campaign {campaign_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def sync_sheet_campaign_internal(campaign_id: int) -> dict:
    """
    Internal function for auto-sync scheduler.
    Returns dict instead of Flask response.
    """
    import re
    import gspread
    
    try:
        campaign = WhatsAppDripCampaign.query.get(campaign_id)
        if not campaign:
            return {"success": False, "error": "Campaign not found"}
        
        if campaign.trigger_type != 'google_sheet_row':
            return {"success": False, "error": "Campaign not linked to Google Sheet"}
        
        if not campaign.sheet_id:
            return {"success": False, "error": "No sheet URL configured"}
        
        gc, error = get_gspread_client()
        if error:
            return {"success": False, "error": error}
        
        try:
            sheet_id = campaign.sheet_id
            if "docs.google.com" in sheet_id:
                match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_id)
                if match:
                    sheet_id = match.group(1)
            
            spreadsheet = gc.open_by_key(sheet_id)
            worksheet = spreadsheet.worksheet(campaign.sheet_name or "Sheet1")
        except gspread.exceptions.SpreadsheetNotFound:
            return {"success": False, "error": "Spreadsheet not found"}
        except gspread.exceptions.WorksheetNotFound:
            return {"success": False, "error": f"Worksheet '{campaign.sheet_name}' not found"}
        
        try:
            all_values = worksheet.get(value_render_option='UNFORMATTED_VALUE')
        except Exception:
            all_values = worksheet.get_all_values()
        
        if len(all_values) <= 1:
            return {"success": True, "enrolled": 0, "skipped": 0, "message": "No data in sheet"}
        
        headers = all_values[0]
        data_rows = all_values[1:]
        
        phone_col_name = campaign.phone_column or "phone"
        phone_col_idx = None
        for i, h in enumerate(headers):
            if h.lower().strip() == phone_col_name.lower().strip():
                phone_col_idx = i
                break
        
        if phone_col_idx is None:
            return {"success": False, "error": f"Column '{phone_col_name}' not found"}
        
        first_step = WhatsAppDripStep.query.filter_by(campaign_id=campaign_id, step_order=1).first()
        
        enrolled = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        start_row = campaign.last_synced_row or 0
        
        for idx, row in enumerate(data_rows):
            if idx < start_row:
                continue
            
            raw_phone = row[phone_col_idx] if phone_col_idx < len(row) else ""
            phone = normalize_phone_number(raw_phone)
            
            if not phone:
                skipped += 1
                continue
            
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id, phone_number=phone
            ).filter(WhatsAppDripEnrollment.status.in_(["active", "completed"])).first()
            
            if existing:
                skipped += 1
                continue
            
            row_data = {headers[i]: row[i] for i in range(len(headers)) if i < len(row)}
            
            # Transform using column_mapping
            variables = {}
            
            # 1. Apply column mappings
            if campaign.column_mapping:
                for step_key, col_name in campaign.column_mapping.items():
                    val = row_data.get(col_name)
                    if val is not None and str(val).strip():
                        variables[step_key] = str(val)
            else:
                variables = row_data
            
            # 2. Apply fallback values for missing keys
            if campaign.fallback_values:
                for step_key, default_val in campaign.fallback_values.items():
                    if step_key not in variables or not variables.get(step_key):
                        variables[step_key] = str(default_val)
            
            next_run = now_utc + timedelta(seconds=first_step.delay_seconds) if first_step else None
            
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=phone,
                phone_original=str(raw_phone),
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,
                variables_source="google_sheet",
                variables_row_index=idx + 2,
                variables_sheet_id=sheet_id,
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled += 1
        
        campaign.enrolled_count += enrolled
        campaign.last_synced_row = len(data_rows)
        db.session.commit()
        
        return {"success": True, "enrolled": enrolled, "skipped": skipped}
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Internal sync error for campaign {campaign_id}: {e}")
        return {"success": False, "error": str(e)}


def enroll_from_crm(entity_type: str, entity_data: dict, workspace_id: int) -> dict:
    """
    Auto-enroll a CRM Lead or Contact into matching drip campaigns.
    
    Args:
        entity_type: 'lead' or 'contact'
        entity_data: Dict with entity fields (name, phone, email, company, etc.)
        workspace_id: The workspace ID
    
    Returns:
        dict with 'enrolled_campaigns' count and 'errors' list
    """
    trigger_type = f"new_{entity_type}"  # 'new_lead' or 'new_contact'
    
    # Normalize phone number
    phone = entity_data.get('phone', '')
    if not phone:
        logger.warning(f"[CRM Drip] No phone number for {entity_type}, skipping enrollment")
        return {"enrolled_campaigns": 0, "errors": ["No phone number"]}
    
    phone = normalize_phone_number(phone)
    if not phone:
        return {"enrolled_campaigns": 0, "errors": ["Invalid phone number"]}
    
    # Find all active campaigns with matching trigger type
    campaigns = WhatsAppDripCampaign.query.filter_by(
        trigger_type=trigger_type,
        status='active'
    ).all()
    
    if not campaigns:
        logger.debug(f"[CRM Drip] No active campaigns for trigger_type={trigger_type}")
        return {"enrolled_campaigns": 0, "errors": []}
    
    enrolled_campaigns = 0
    errors = []
    now_utc = datetime.now(timezone.utc)
    
    for campaign in campaigns:
        try:
            # Check for duplicate enrollment
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign.id,
                phone_number=phone
            ).filter(WhatsAppDripEnrollment.status.in_(["active", "completed"])).first()
            
            if existing:
                logger.debug(f"[CRM Drip] Phone {phone} already enrolled in campaign {campaign.id}")
                continue
            
            # Transform entity_data using column_mapping
            # column_mapping: {"step_1_1": "name", "step_1_2": "company", ...}
            logger.info(f"[CRM Drip] Campaign {campaign.id} column_mapping: {campaign.column_mapping}")
            logger.info(f"[CRM Drip] Campaign {campaign.id} fallback_values: {campaign.fallback_values}")
            logger.info(f"[CRM Drip] Entity data keys: {list(entity_data.keys())}")
            
            variables = {}
            
            # 1. Apply column mappings from CRM entity data
            if campaign.column_mapping:
                for step_key, field_name in campaign.column_mapping.items():
                    val = None
                    if field_name in entity_data:
                        val = entity_data[field_name]
                    elif field_name.lower() in entity_data:
                        val = entity_data[field_name.lower()]
                    
                    if val is not None and str(val).strip():
                        variables[step_key] = str(val)
                logger.info(f"[CRM Drip] Variables after column_mapping: {variables}")
            else:
                # No mapping configured - campaigns may have been created before fix
                logger.warning(f"[CRM Drip] Campaign {campaign.id} has no column_mapping! Please re-create the campaign.")
                # Use raw data as is, but it won't work with extract_step_params
                variables = entity_data.copy()
            
            # 2. Apply fallback values for any missing keys
            if campaign.fallback_values:
                for step_key, default_val in campaign.fallback_values.items():
                    if step_key not in variables or not variables.get(step_key):
                        variables[step_key] = str(default_val)
                        logger.debug(f"[CRM Drip] Applied fallback for {step_key}: {default_val}")
            
            logger.info(f"[CRM Drip] Final variables for enrollment: {variables}")
            
            # Get first step for delay calculation
            first_step = WhatsAppDripStep.query.filter_by(
                campaign_id=campaign.id, 
                step_order=1
            ).first()
            
            next_run = now_utc + timedelta(seconds=first_step.delay_seconds) if first_step else None
            
            # Create enrollment
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign.id,
                phone_number=phone,
                phone_original=entity_data.get('phone', ''),
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,
                variables_source=f"crm_{entity_type}",
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled_campaigns += 1
            
            logger.info(f"[CRM Drip] Enrolled {phone} from {entity_type} into campaign {campaign.id} ({campaign.name})")
            
        except Exception as e:
            logger.exception(f"[CRM Drip] Error enrolling into campaign {campaign.id}: {e}")
            errors.append(str(e))
    
    if enrolled_campaigns > 0:
        db.session.commit()
        campaign.enrolled_count += enrolled_campaigns
        db.session.commit()
    
    return {"enrolled_campaigns": enrolled_campaigns, "errors": errors}


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/enroll", methods=["POST"])
@require_account_access
def enroll_user(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Manually enroll a user into a drip campaign."""
    data = request.get_json() or {}
    phone_number = data.get("phone_number")
    
    if not phone_number:
        return jsonify({"error": "Phone number is required"}), 400
        
    logger.info(f"ENROLL_USER RAW PAYLOAD: {json.dumps(data)}") # DEBUG LOG
        
    try:
        campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
        
        # Check if already enrolled
        existing = WhatsAppDripEnrollment.query.filter_by(
            campaign_id=campaign_id,
            phone_number=phone_number,
            status="active"
        ).first()
        
        if existing:
            return jsonify({"error": "User already enrolled in this campaign"}), 400
            
        # Create enrollment
        # First step runs after its delay relative to NOW
        first_step = WhatsAppDripStep.query.filter_by(campaign_id=campaign_id, step_order=1).first()
        
        next_run = None
        if first_step:
            next_run = datetime.now(timezone.utc) + timedelta(seconds=first_step.delay_seconds)
            
        enrollment = WhatsAppDripEnrollment(
            campaign_id=campaign_id,
            phone_number=phone_number,
            current_step_order=0, # Not strictly on step 1 yet (waiting for step 1)
            next_run_at=next_run,
            status="active"
        )
        
        # Process variables
        variables = {}
        
        # 1. Add profile data (legacy support)
        profile_data = data.get("profile_data", {})
        if profile_data:
            variables.update(profile_data)
            
        # 2. Add per-step variables (flattened)
        # Frontend sends: {"step_1": {"name": "val"}, "step_2": ...}
        # Backend needs: {"step_1_name": "val", ...}
        per_step_vars = data.get("per_step_variables", {})
        for step_key, step_vars in per_step_vars.items():
            for key, val in step_vars.items():
                flat_key = f"{step_key}_{key}" # e.g. step_1_name
                variables[flat_key] = str(val)
                
        enrollment.variables = variables
        
        campaign.enrolled_count += 1
        db.session.add(enrollment)
        db.session.commit()
        
        return jsonify({
            "success": True, 
            "message": f"Enrolled {phone_number}",
            "next_run_at": next_run.isoformat() if next_run else None
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error enrolling user: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Enrollments Management
# ============================================================

@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/enrollments", methods=["GET"])
@require_account_access
def list_enrollments(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """List all enrollments for a drip campaign with pagination."""
    try:
        page = request.args.get('page', 1, type=int)
        limit = request.args.get('limit', 20, type=int)
        
        # Verify campaign belongs to this account
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id, 
            account_id=account_id
        ).first_or_404()
        
        query = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id)
        total = query.count()
        
        enrollments = query.order_by(
            WhatsAppDripEnrollment.created_at.desc()
        ).offset((page - 1) * limit).limit(limit).all()
        
        return jsonify({
            "success": True,
            "enrollments": [{
                "id": e.id,
                "phone_number": e.phone_number,
                "current_step_order": e.current_step_order,
                "status": e.status,
                "next_run_at": e.next_run_at.isoformat() if e.next_run_at else None,
                "created_at": e.created_at.isoformat() if e.created_at else None
            } for e in enrollments],
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total,
                "pages": (total + limit - 1) // limit
            }
        })
    except Exception as e:
        logger.exception(f"Error listing enrollments: {e}")
        return jsonify({"error": "Failed to list enrollments"}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/bulk-enroll", methods=["POST"])
@require_account_access
def bulk_enroll(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Bulk enroll multiple phone numbers into a drip campaign."""
    data = request.get_json() or {}
    phone_numbers = data.get("phone_numbers", [])
    
    if not phone_numbers:
        return jsonify({"error": "No phone numbers provided"}), 400
    
    try:
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        first_step = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id, 
            step_order=1
        ).first()
        
        enrolled = 0
        skipped = 0
        
        for phone in phone_numbers:
            phone = str(phone).strip()
            if not phone:
                continue
                
            # Basic validation
            clean_phone = "".join(filter(str.isdigit, phone))
            if len(clean_phone) < 10:
                skipped += 1
                continue
                
            # Check if already enrolled
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id,
                phone_number=phone,
                status="active"
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            next_run = None
            if first_step:
                next_run = datetime.now(timezone.utc) + timedelta(seconds=first_step.delay_seconds)
            
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=phone,
                current_step_order=0,
                next_run_at=next_run,
                status="active"
            )
            db.session.add(enrollment)
            enrolled += 1
        
        campaign.enrolled_count += enrolled
        db.session.commit()
        
        return jsonify({
            "success": True,
            "enrolled": enrolled,
            "skipped": skipped,
            "message": f"Enrolled {enrolled} contacts ({skipped} skipped)"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error in bulk enroll: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/import-contacts", methods=["POST"])
@require_account_access
def import_contacts_csv(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Import contacts from CSV into a drip campaign with variable mapping."""
    import csv
    import io
    import json
    
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
        
    file = request.files["file"]
    mapping_str = request.form.get("column_mapping", "{}")
    
    try:
        column_mapping = json.loads(mapping_str)
    except:
        column_mapping = {}
        
    fallback_str = request.form.get("fallback_values", "{}")
    try:
        fallback_values = json.loads(fallback_str)
    except:
        fallback_values = {}

    try:
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))
        
        first_step = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id, 
            step_order=1
        ).first()
        
        enrolled = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        
        phone_col = column_mapping.get("phone") or request.form.get("phone_column")

        for row in reader:
            # Find phone from mapping, explicit column, or common header names
            phone_val = None
            if phone_col and phone_col in row:
                phone_val = row[phone_col]
            else:
                for k in row.keys():
                    key_lower = str(k).lower().strip()
                    if key_lower in ("phone", "mobile", "whatsapp", "phone_number", "phonenumber", "contact"):
                        phone_val = row[k]
                        break
                    if "phone" in key_lower or key_lower == "msisdn":
                        phone_val = row[k]
                        break
            
            # If still no phone, skip
            if not phone_val:
                skipped += 1
                continue
                
            norm_phone = normalize_phone_number(phone_val)
            if not norm_phone or len(norm_phone) < 10:
                skipped += 1
                continue
                
            # Check existing
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                status="active"
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            # Build variables
            variables = {}
            # 1. Map from CSV
            if column_mapping:
                for var_name, csv_col in column_mapping.items():
                    if csv_col and csv_col in row:
                        variables[var_name] = row[csv_col]
            
            # 2. Apply fallbacks
            if fallback_values:
                for var_name, default_val in fallback_values.items():
                    current_val = variables.get(var_name)
                    if not current_val:
                         variables[var_name] = str(default_val)
            
            next_run = None
            if first_step:
                next_run = now_utc + timedelta(seconds=first_step.delay_seconds)
                
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,
                variables_source="csv_import",
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled += 1
            
        campaign.enrolled_count += enrolled
        db.session.commit()
        
        return jsonify({
            "success": True,
            "enrolled": enrolled,
            "skipped": skipped,
            "message": f"Imported {enrolled} contacts"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error importing CSV contacts: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/enrollments/<int:enrollment_id>", methods=["PATCH"])
@require_account_access
def update_enrollment(account_id: int, campaign_id: int, enrollment_id: int, account: WhatsAppAccount, workspace_id: str):
    """Update enrollment status (pause, resume, restart)."""
    data = request.get_json() or {}
    action = data.get("action")
    
    if action not in ["pause", "resume", "restart"]:
        return jsonify({"error": "Invalid action. Must be pause, resume, or restart"}), 400
    
    try:
        enrollment = WhatsAppDripEnrollment.query.filter_by(
            id=enrollment_id,
            campaign_id=campaign_id
        ).first_or_404()
        
        if action == "pause":
            enrollment.status = "paused"
            message = "Enrollment paused"
        elif action == "resume":
            enrollment.status = "active"
            message = "Enrollment resumed"
        elif action == "restart":
            first_step = WhatsAppDripStep.query.filter_by(
                campaign_id=campaign_id,
                step_order=1
            ).first()
            enrollment.current_step_order = 0
            enrollment.status = "active"
            if first_step:
                enrollment.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=first_step.delay_seconds)
            message = "Enrollment restarted"
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": message
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error updating enrollment: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/enrollments/<int:enrollment_id>", methods=["DELETE"])
@require_account_access
def delete_enrollment(account_id: int, campaign_id: int, enrollment_id: int, account: WhatsAppAccount, workspace_id: str):
    """Remove an enrollment from a campaign."""
    try:
        enrollment = WhatsAppDripEnrollment.query.filter_by(
            id=enrollment_id,
            campaign_id=campaign_id
        ).first_or_404()
        
        campaign = WhatsAppDripCampaign.query.get(campaign_id)
        if campaign and campaign.enrolled_count > 0:
            campaign.enrolled_count -= 1
        
        db.session.delete(enrollment)
        db.session.commit()
        
        return jsonify({
            "success": True,
            "message": "Enrollment removed"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error deleting enrollment: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/import-sheet", methods=["POST"])
@require_account_access
def import_contacts_sheet(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Import contacts from Google Sheet into a drip campaign with variable mapping."""
    import gspread
    import re
    
    data = request.get_json() or {}
    sheet_url = data.get("sheet_url")
    sheet_name = data.get("sheet_name", "Sheet1")
    phone_col_name = data.get("phone_column", "phone")
    column_mapping = data.get("column_mapping", {})
    
    if not sheet_url:
        return jsonify({"error": "Sheet URL is required"}), 400
        
    gc, error = get_gspread_client()
    if error:
        return jsonify({"error": error}), 500
        
    try:
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        # Extract sheet ID
        actual_sheet_id = sheet_url
        if "docs.google.com" in sheet_url:
            match = re.search(r'/d/([a-zA-Z0-9-_]+)', sheet_url)
            if match:
                actual_sheet_id = match.group(1)
        
        spreadsheet = gc.open_by_key(actual_sheet_id)
        if sheet_name:
            worksheet = spreadsheet.worksheet(sheet_name)
        else:
            worksheet = spreadsheet.sheet1
            
        # Get raw values
        all_values = worksheet.get_all_values(value_render_option='UNFORMATTED_VALUE')
        
        if not all_values:
            return jsonify({
                "success": True, 
                "message": "Sheet is empty",
                "enrolled": 0,
                "skipped": 0
            })
            
        headers = [str(h) for h in (all_values[0] if all_values else [])]
        data_rows = all_values[1:]
        
        # Find phone column index
        phone_col_idx = -1
        for i, h in enumerate(headers):
            if h.lower().strip() == phone_col_name.lower().strip():
                phone_col_idx = i
                break
        
        if phone_col_idx == -1:
            return jsonify({"error": f"Phone column '{phone_col_name}' not found in sheet"}), 400
            
        first_step = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id, 
            step_order=1
        ).first()
        
        enrolled = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)
        
        for row in data_rows:
            # Get phone value
            if phone_col_idx >= len(row):
                skipped += 1
                continue
                
            raw_phone = row[phone_col_idx]
            
            # Clean scientific notation if string
            raw_phone_str = str(raw_phone)
            if raw_phone_str.endswith(".0"):
                raw_phone_str = raw_phone_str[:-2]
                
            norm_phone = normalize_phone_number(raw_phone_str)
            if not norm_phone or len(norm_phone) < 10:
                skipped += 1
                continue
                
            # Check existing
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                status="active"
            ).first()
            
            if existing:
                skipped += 1
                continue
                
            # Build row dict for mapping
            row_dict = {}
            for i, val in enumerate(row):
                if i < len(headers):
                    val_str = str(val)
                    if val_str.endswith(".0"):
                        val_str = val_str[:-2]
                    row_dict[headers[i]] = val_str
            
            # Build variables
            variables = {}
            fallback_values = data.get("fallback_values", {})
            
            # 1. Apply from sheet columns
            if column_mapping:
                for var_name, csv_col in column_mapping.items():
                    if csv_col and csv_col in row_dict:
                        variables[var_name] = row_dict[csv_col]
            
            # 2. Apply fallbacks for missing/empty values
            if fallback_values:
                for var_name, default_val in fallback_values.items():
                    current_val = variables.get(var_name)
                    if not current_val: # None or empty string
                         variables[var_name] = str(default_val)
            
            next_run = None
            if first_step:
                next_run = now_utc + timedelta(seconds=first_step.delay_seconds)
                
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,
                variables_source="sheet_import",
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled += 1
            
        db.session.commit()
        
        return jsonify({
            "success": True,
            "enrolled": enrolled,
            "skipped": skipped,
            "message": f"Successfully enrolled {enrolled} contacts ({skipped} skipped)"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error importing sheet: {e}")
        return jsonify({"error": str(e)}), 500


@drip_bp.route("/accounts/<int:account_id>/drip-campaigns/<int:campaign_id>/enroll-dataset", methods=["POST"])
@require_account_access
def enroll_dataset(account_id: int, campaign_id: int, account: WhatsAppAccount, workspace_id: str):
    """Enroll contacts from a dataset into a drip campaign."""
    data = request.get_json() or {}
    dataset_id = data.get("dataset_id")
    # workspace_id is already provided by decorator but frontend might send it too
    phone_col = data.get("phone_column", "phone")
    name_col = data.get("name_column", "name")
    column_mapping = data.get("column_mapping", {})
    fallback_values = data.get("fallback_values", {})
    
    if not dataset_id:
        return jsonify({"error": "dataset_id is required"}), 400
        
    try:
        # ... fetch campaign, dataset, first_step ...
        campaign = WhatsAppDripCampaign.query.filter_by(
            id=campaign_id,
            account_id=account_id
        ).first_or_404()
        
        from .dataset_models import Dataset, DatasetRow

        try:
            ws_id = int(workspace_id)
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid workspace_id"}), 400

        dataset = Dataset.query.filter_by(
            id=dataset_id,
            workspace_id=ws_id,
        ).first_or_404()

        first_step = WhatsAppDripStep.query.filter_by(
            campaign_id=campaign_id,
            step_order=1,
        ).first()

        enrolled = 0
        skipped = 0
        now_utc = datetime.now(timezone.utc)

        rows = DatasetRow.query.filter_by(dataset_id=dataset_id).all()
        
        for row_obj in rows:
            row_data = _clean_row_data(row_obj.data or {})
            
            # Get phone — try exact column, then trimmed match
            raw_phone = row_data.get(phone_col, "")
            if not raw_phone:
                # Fallback: try trimmed column name match
                phone_col_trimmed = phone_col.strip() if isinstance(phone_col, str) else phone_col
                for k, v in row_data.items():
                    if k.strip() == phone_col_trimmed:
                        raw_phone = v
                        break
            raw_phone_str = str(raw_phone)
            if raw_phone_str.endswith(".0"):
                raw_phone_str = raw_phone_str[:-2]
            
            norm_phone = normalize_phone_number(raw_phone_str)
            if not norm_phone or len(norm_phone) < 10:
                skipped += 1
                continue
                
            # Check existing
            existing = WhatsAppDripEnrollment.query.filter_by(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                status="active"
            ).first()
            
            if existing:
                skipped += 1
                continue
            
            # Build variables
            variables = {}
            
            # 1. Apply column mappings
            if column_mapping:
                for variable_key, dataset_col in column_mapping.items():
                    val = None
                    if dataset_col and dataset_col in row_data:
                        val = row_data[dataset_col]
                    elif dataset_col:
                        # Fallback: try trimmed column match
                        dc_trimmed = dataset_col.strip() if isinstance(dataset_col, str) else dataset_col
                        for k, v in row_data.items():
                            if k.strip() == dc_trimmed:
                                val = v
                                break
                    
                    if val is not None and str(val).strip():
                        variables[variable_key] = str(val).strip()
            
            # 2. Apply fallbacks for missing keys - iterate over known fallbacks
            if fallback_values:
                for var_key, default_val in fallback_values.items():
                    if var_key not in variables or not variables[var_key]:
                        variables[var_key] = str(default_val)
            
            # 3. Add name if mapped
            name_val = row_data.get(name_col, "") if name_col else ""
            if not name_val and name_col:
                # Trimmed fallback
                nc_trimmed = name_col.strip() if isinstance(name_col, str) else name_col
                for k, v in row_data.items():
                    if k.strip() == nc_trimmed:
                        name_val = v
                        break
            if name_val:
                variables["name"] = str(name_val).strip()

            next_run = None
            if first_step:
                next_run = now_utc + timedelta(seconds=first_step.delay_seconds)
                
            enrollment = WhatsAppDripEnrollment(
                campaign_id=campaign_id,
                phone_number=norm_phone,
                current_step_order=0,
                next_run_at=next_run,
                status="active",
                variables=variables,
                variables_source="dataset",
                variables_sheet_id=str(dataset_id),
                variables_last_synced_at=now_utc
            )
            db.session.add(enrollment)
            enrolled += 1
            
        if enrolled > 0:
            campaign.enrolled_count += enrolled
            
        db.session.commit()
        
        return jsonify({
            "success": True, 
            "enrolled": enrolled, 
            "skipped": skipped,
            "message": f"Successfully enrolled {enrolled} contacts ({skipped} skipped)"
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error enrolling from dataset: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Drip Analytics
# ============================================================


def _drip_campaign_query(workspace_id=None):
    q = WhatsAppDripCampaign.query.filter(WhatsAppDripCampaign.trigger_type != "manual")
    if workspace_id:
        q = q.filter_by(workspace_id=str(workspace_id))
    return q


def _message_stats_for_campaign(campaign_id: int):
    from sqlalchemy import func
    from .models import WhatsAppMessage

    rows = (
        db.session.query(WhatsAppMessage.status, func.count(WhatsAppMessage.id))
        .filter(WhatsAppMessage.campaign_id == campaign_id, WhatsAppMessage.direction == "outgoing")
        .group_by(WhatsAppMessage.status)
        .all()
    )
    counts = {status: cnt for status, cnt in rows}
    sent = sum(counts.values())
    delivered = counts.get("delivered", 0) + counts.get("read", 0)
    read = counts.get("read", 0)
    failed = counts.get("failed", 0)
    return {"sent": sent, "delivered": delivered, "read": read, "failed": failed, "replied": 0}


@drip_bp.route("/drip-campaigns/analytics/overview", methods=["GET"])
def drip_analytics_overview():
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"error": "workspace_id required"}), 400

    campaigns = _drip_campaign_query(workspace_id).all()
    total_enrollments = 0
    total_sent = total_delivered = total_read = total_replied = 0
    campaign_rows = []

    for c in campaigns:
        stats = _message_stats_for_campaign(c.id)
        total_enrollments += int(c.enrolled_count or 0)
        total_sent += stats["sent"]
        total_delivered += stats["delivered"]
        total_read += stats["read"]
        total_replied += stats["replied"]
        campaign_rows.append({
            "id": c.id,
            "name": c.name,
            "status": c.status,
            "enrolled_count": int(c.enrolled_count or 0),
            "sent_count": stats["sent"],
            "delivered_count": stats["delivered"],
            "read_count": stats["read"],
            "replied_count": stats["replied"],
        })

    now = datetime.now(timezone.utc)
    daily_trends = []
    from sqlalchemy import func, case
    from .models import WhatsAppMessage, WhatsAppConversation, WhatsAppAccount

    account_ids = [a.id for a in WhatsAppAccount.query.filter_by(workspace_id=str(workspace_id), is_active=True).all()]
    if account_ids:
        start = now - timedelta(days=29)
        msg_rows = (
            db.session.query(
                func.date(WhatsAppMessage.created_at).label("day"),
                func.count(WhatsAppMessage.id).label("sent"),
                func.sum(case((WhatsAppMessage.status.in_(["delivered", "read"]), 1), else_=0)).label("delivered"),
                func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read"),
            )
            .join(WhatsAppConversation, WhatsAppMessage.conversation_id == WhatsAppConversation.id)
            .filter(
                WhatsAppMessage.created_at >= start,
                WhatsAppMessage.direction == "outgoing",
                WhatsAppConversation.account_id.in_(account_ids),
            )
            .group_by(func.date(WhatsAppMessage.created_at))
            .all()
        )
        by_day = {str(r.day): r for r in msg_rows}
        for offset in range(29, -1, -1):
            day = (now - timedelta(days=offset)).date()
            key = str(day)
            row = by_day.get(key)
            daily_trends.append({
                "date": key,
                "sent": int(row.sent or 0) if row else 0,
                "delivered": int(row.delivered or 0) if row else 0,
                "read": int(row.read or 0) if row else 0,
            })

    active = sum(1 for c in campaigns if c.status in ("active", "running", "scheduled"))
    return jsonify({
        "summary": {
            "total_campaigns": len(campaigns),
            "active_campaigns": active,
            "total_enrollments": total_enrollments,
            "total_sent": total_sent,
            "total_delivered": total_delivered,
            "total_read": total_read,
            "total_replied": total_replied,
        },
        "campaigns": campaign_rows,
        "daily_trends": daily_trends,
    })


@drip_bp.route("/drip-campaigns/<int:campaign_id>/analytics/summary", methods=["GET"])
def drip_campaign_analytics_summary(campaign_id: int):
    campaign = WhatsAppDripCampaign.query.get_or_404(campaign_id)
    enrollments = WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id).all()
    enrollment_stats = {
        "total": len(enrollments),
        "active": sum(1 for e in enrollments if e.status == "active"),
        "completed": sum(1 for e in enrollments if e.status == "completed"),
        "paused": sum(1 for e in enrollments if e.status == "paused"),
        "failed": sum(1 for e in enrollments if e.status in ("failed", "blocked_missing_data")),
    }
    msg_stats = _message_stats_for_campaign(campaign_id)
    return jsonify({
        "enrollment": enrollment_stats,
        "messages": msg_stats,
        "campaign": {"id": campaign.id, "name": campaign.name, "status": campaign.status},
    })


@drip_bp.route("/drip-campaigns/<int:campaign_id>/analytics/daily", methods=["GET"])
def drip_campaign_analytics_daily(campaign_id: int):
    from sqlalchemy import func, case
    from .models import WhatsAppMessage

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=29)
    rows = (
        db.session.query(
            func.date(WhatsAppMessage.created_at).label("day"),
            func.count(WhatsAppMessage.id).label("sent"),
            func.sum(case((WhatsAppMessage.status == "read", 1), else_=0)).label("read"),
        )
        .filter(
            WhatsAppMessage.campaign_id == campaign_id,
            WhatsAppMessage.direction == "outgoing",
            WhatsAppMessage.created_at >= start,
        )
        .group_by(func.date(WhatsAppMessage.created_at))
        .order_by(func.date(WhatsAppMessage.created_at))
        .all()
    )
    by_day = {str(r.day): r for r in rows}
    daily = []
    for offset in range(29, -1, -1):
        day = (now - timedelta(days=offset)).date()
        key = str(day)
        row = by_day.get(key)
        daily.append({
            "date": key,
            "sent": int(row.sent or 0) if row else 0,
            "read": int(row.read or 0) if row else 0,
        })
    return jsonify(daily)


@drip_bp.route("/drip-campaigns/<int:campaign_id>/analytics/funnel", methods=["GET"])
def drip_campaign_analytics_funnel(campaign_id: int):
    steps = (
        WhatsAppDripStep.query.filter_by(campaign_id=campaign_id)
        .order_by(WhatsAppDripStep.step_order.asc())
        .all()
    )
    funnel = []
    for step in steps:
        active_at_step = WhatsAppDripEnrollment.query.filter(
            WhatsAppDripEnrollment.campaign_id == campaign_id,
            WhatsAppDripEnrollment.current_step_order >= step.step_order,
        ).count()
        completed_past = WhatsAppDripEnrollment.query.filter(
            WhatsAppDripEnrollment.campaign_id == campaign_id,
            WhatsAppDripEnrollment.current_step_order > step.step_order,
        ).count()
        funnel.append({
            "step_order": step.step_order,
            "step_name": f"Step {step.step_order}",
            "template_name": step.template_name,
            "sent": active_at_step + completed_past,
            "read": completed_past,
        })
    return jsonify(funnel)


@drip_bp.route("/drip-campaigns/<int:campaign_id>/analytics/enrollments", methods=["GET"])
def drip_campaign_analytics_enrollments(campaign_id: int):
    per_page = min(max(int(request.args.get("per_page", 10)), 1), 100)
    rows = (
        WhatsAppDripEnrollment.query.filter_by(campaign_id=campaign_id)
        .order_by(WhatsAppDripEnrollment.created_at.desc())
        .limit(per_page)
        .all()
    )
    enrollments = []
    for e in rows:
        variables = e.variables if isinstance(e.variables, dict) else {}
        name = variables.get("name") or variables.get("full_name") or variables.get("customer_name")
        enrollments.append({
            "id": e.id,
            "contact_name": name or e.phone_number,
            "phone_number": e.phone_number,
            "status": e.status,
            "current_step": e.current_step_order or 0,
            "joined_at": e.created_at.isoformat() if e.created_at else None,
            "next_run_at": e.next_run_at.isoformat() if e.next_run_at else None,
        })
    return jsonify({"enrollments": enrollments})


# ============================================================
# CRM Audience (drip enrollment from CRM leads/contacts)
# ============================================================

@drip_bp.route("/accounts/<int:account_id>/crm-audience/summary", methods=["GET"])
@require_account_access
def crm_audience_summary(account_id: int, account: WhatsAppAccount, workspace_id: str):
    campaign_id = request.args.get("campaign_id", type=int)
    if not campaign_id:
        return jsonify({"error": "campaign_id required"}), 400

    campaign = WhatsAppDripCampaign.query.filter_by(
        id=campaign_id,
        account_id=account_id,
    ).first()
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    crm_models = _get_crm_models()
    if not crm_models:
        return jsonify({"error": "CRM not initialized"}), 503

    crm_ws_id = _crm_workspace_id_for_account(account)
    if crm_ws_id is None:
        return jsonify({"error": "Account has no workspace"}), 400

    Lead = crm_models["Lead"]
    Contact = crm_models["Contact"]
    leads = Lead.query.filter_by(workspace_id=crm_ws_id).all()
    contacts = Contact.query.filter_by(workspace_id=crm_ws_id).all()
    enrolled_phones = _enrolled_phones_for_campaign(campaign_id)

    return jsonify(_crm_audience_stats(leads, contacts, enrolled_phones))


@drip_bp.route("/accounts/<int:account_id>/crm-audience/leads", methods=["GET"])
@require_account_access
def crm_audience_leads(account_id: int, account: WhatsAppAccount, workspace_id: str):
    campaign_id = request.args.get("campaign_id", type=int)
    if not campaign_id:
        return jsonify({"error": "campaign_id required"}), 400

    campaign = WhatsAppDripCampaign.query.filter_by(
        id=campaign_id,
        account_id=account_id,
    ).first()
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    crm_models = _get_crm_models()
    if not crm_models:
        return jsonify({"error": "CRM not initialized"}), 503

    crm_ws_id = _crm_workspace_id_for_account(account)
    if crm_ws_id is None:
        return jsonify({"error": "Account has no workspace"}), 400

    limit = min(request.args.get("limit", 100, type=int), 500)
    include_all = request.args.get("include_all", "false").lower() in ("1", "true", "yes")
    enrolled_phones = _enrolled_phones_for_campaign(campaign_id)

    Lead = crm_models["Lead"]
    query = Lead.query.filter_by(workspace_id=crm_ws_id).order_by(Lead.created_at.desc())
    records = query.limit(limit).all()

    leads = []
    for record in records:
        item = _serialize_crm_record(record, enrolled_phones, include_without_phone=include_all)
        if item:
            leads.append(item)

    return jsonify({"success": True, "leads": leads, "total": len(leads)})


@drip_bp.route("/accounts/<int:account_id>/crm-audience/contacts", methods=["GET"])
@require_account_access
def crm_audience_contacts(account_id: int, account: WhatsAppAccount, workspace_id: str):
    campaign_id = request.args.get("campaign_id", type=int)
    if not campaign_id:
        return jsonify({"error": "campaign_id required"}), 400

    campaign = WhatsAppDripCampaign.query.filter_by(
        id=campaign_id,
        account_id=account_id,
    ).first()
    if not campaign:
        return jsonify({"error": "Campaign not found"}), 404

    crm_models = _get_crm_models()
    if not crm_models:
        return jsonify({"error": "CRM not initialized"}), 503

    crm_ws_id = _crm_workspace_id_for_account(account)
    if crm_ws_id is None:
        return jsonify({"error": "Account has no workspace"}), 400

    limit = min(request.args.get("limit", 100, type=int), 500)
    enrolled_phones = _enrolled_phones_for_campaign(campaign_id)

    Contact = crm_models["Contact"]
    records = (
        Contact.query.filter_by(workspace_id=crm_ws_id)
        .order_by(Contact.created_at.desc())
        .limit(limit)
        .all()
    )

    contacts = []
    for record in records:
        item = _serialize_crm_record(record, enrolled_phones, include_without_phone=False)
        if item:
            contacts.append(item)

    return jsonify({"success": True, "contacts": contacts, "total": len(contacts)})
