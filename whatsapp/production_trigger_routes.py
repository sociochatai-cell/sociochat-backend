"""
Production Trigger Routes - Enhanced API endpoints for real-world usage.

Features:
- Bulk sending to multiple recipients
- Webhook receiver for e-commerce/CRM integrations  
- Trigger logs and analytics
- Contact list management
- Variable mapping from external data
"""
import json
import secrets
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from flask import Blueprint, request, jsonify, url_for
from sqlalchemy import func, and_, desc

from shared_models import db
from .models import WhatsAppAccount, WhatsAppTemplate
from .trigger_models import WhatsAppTrigger
from .trigger_logs_model import TriggerLog, TriggerContact
from .services import WhatsAppService
from .token_helper import get_account_with_token
from .flow_access import require_account_access

logger = logging.getLogger(__name__)

production_trigger_bp = Blueprint("production_triggers", __name__, url_prefix="/api/whatsapp")


# ============================================================
# Helper Functions
# ============================================================

def _get_client_ip():
    """Get client IP from request, handling proxies."""
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr


def _log_trigger_invocation(trigger: WhatsAppTrigger, phone: str, variables: list, 
                            success: bool, message_id: str = None, error: str = None,
                            source_type: str = "api", source_ref: str = None):
    """Log a trigger invocation for analytics and debugging."""
    try:
        log = TriggerLog(
            trigger_id=trigger.id,
            workspace_id=trigger.workspace_id,
            account_id=trigger.account_id,
            recipient_phone=phone,
            variables_json=json.dumps(variables) if variables else None,
            source_ip=_get_client_ip(),
            source_type=source_type,
            source_reference=source_ref,
            success=success,
            message_id=message_id,
            error_message=error,
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as e:
        logger.error(f"Failed to log trigger invocation: {e}")
        return None


def _send_via_trigger(trigger: WhatsAppTrigger, phone: str, variables: list, 
                      source_type: str = "api", source_ref: str = None,
                      header_image_url: str = None, header_video_url: str = None,
                      header_document_url: str = None, header_document_id: str = None,
                      header_document_filename: str = None,
                      copy_code_value: str = None):
    """
    Core function to send a message via trigger.
    Returns (success, message_id_or_error)
    """
    # Get account
    account, error = get_account_with_token(trigger.account_id)
    if error or not account:
        return False, "Configuration error: Account not found or invalid token"
    
    # Get template for variable mapping
    template_obj = WhatsAppTemplate.query.filter_by(
        account_id=trigger.account_id,
        name=trigger.template_name,
        language=trigger.language
    ).first()
    
    mapping = template_obj.get_variable_mapping() if template_obj else {}

    # If the template requires a DOCUMENT header and caller didn't pass one,
    # use template example header_handle so Meta doesn't reject with 132012.
    if template_obj and isinstance(template_obj.components, list):
        requires_document_header = False
        example_document_url = None

        for comp in template_obj.components:
            comp_type = str((comp or {}).get("type", "")).upper()
            comp_format = str((comp or {}).get("format", "")).upper()
            if comp_type == "HEADER" and comp_format == "DOCUMENT":
                requires_document_header = True
                example = (comp or {}).get("example") or {}
                handles = example.get("header_handle") or []
                if isinstance(handles, list) and handles:
                    example_document_url = str(handles[0] or "").strip()
                break

        if requires_document_header and not (header_document_id or header_document_url):
            # Don't use example_document_url - it's a temporary CDN link that causes "media upload error"
            return False, "template_requires_document_header_but_no_header_document_id_provided"
    
    # Build components
    components = []
    if variables:
        body_params = []
        for idx, val in enumerate(variables):
            param = {"type": "text", "text": str(val)}
            pos_key = str(idx + 1)
            if pos_key in mapping:
                param["parameter_name"] = mapping[pos_key]
            body_params.append(param)
        
        components.append({
            "type": "body",
            "parameters": body_params
        })

    # Optional media header
    if header_image_url:
        components.append({
            "type": "header",
            "parameters": [{"type": "image", "image": {"link": header_image_url}}]
        })
    elif header_video_url:
        components.append({
            "type": "header",
            "parameters": [{"type": "video", "video": {"link": header_video_url}}]
        })
    elif header_document_id or header_document_url:
        doc_param = {"id": header_document_id} if header_document_id else {"link": header_document_url}
        if header_document_filename:
            doc_param["filename"] = header_document_filename
        components.append({
            "type": "header",
            "parameters": [{"type": "document", "document": doc_param}]
        })

    # Send message
    service = WhatsAppService(db.session, account.phone_number_id, account.get_access_token())
    result = service.send_template(
        to=phone,
        template_name=trigger.template_name,
        language_code=trigger.language,
        components=components,
        copy_code_value=copy_code_value
    )
    
    success = result.get("success", False)
    message_id = result.get("message_id") if success else None
    error_msg = result.get("error") if not success else None
    
    # Log the invocation
    _log_trigger_invocation(
        trigger, phone, variables, success, message_id, error_msg, source_type, source_ref
    )
    
    # Update trigger stats
    if success:
        trigger.trigger_count += 1
        trigger.last_triggered_at = datetime.now(timezone.utc)
        db.session.commit()
    
    return success, message_id if success else error_msg


# ============================================================
# Bulk Sending Endpoint
# ============================================================

@production_trigger_bp.route("/hooks/<int:trigger_id>/bulk", methods=["POST"])
def invoke_trigger_bulk(trigger_id: int):
    """
    Send template to multiple recipients at once.
    
    Requires 'secret' query param or 'X-Trigger-Secret' header.
    
    Body:
    {
        "recipients": [
            {"phone": "1234567890", "variables": ["John", "Order #123"]},
            {"phone": "0987654321", "variables": ["Jane", "Order #456"]}
        ],
        "source_type": "bulk_campaign",  // Optional
        "source_reference": "campaign_001"  // Optional
    }
    
    Response:
    {
        "success": true,
        "summary": {
            "total": 2,
            "sent": 2,
            "failed": 0
        },
        "results": [
            {"phone": "1234567890", "success": true, "message_id": "wamid.xxx"},
            {"phone": "0987654321", "success": true, "message_id": "wamid.yyy"}
        ]
    }
    """
    secret = request.args.get("secret") or request.headers.get("X-Trigger-Secret")
    data = request.get_json(silent=True) or {}
    
    if not secret:
        return jsonify({"error": "Missing secret"}), 401
    
    try:
        trigger = WhatsAppTrigger.query.get(trigger_id)
        
        if not trigger or trigger.secret_key != secret:
            return jsonify({"error": "Invalid trigger or secret"}), 401
        
        if not trigger.is_active:
            return jsonify({"error": "Trigger is inactive"}), 400
        
        recipients = data.get("recipients", [])
        if not recipients:
            return jsonify({"error": "'recipients' array is required"}), 400
        
        if len(recipients) > 100:
            return jsonify({"error": "Maximum 100 recipients per request"}), 400
        
        source_type = data.get("source_type", "bulk")
        source_ref = data.get("source_reference")

        # Global media params — applied to all recipients unless per-recipient override provided
        global_img = data.get("header_image_url")
        global_vid = data.get("header_video_url")
        global_doc = data.get("header_document_url")
        global_doc_id = data.get("header_document_id")
        global_doc_fname = data.get("header_document_filename")
        global_copy = data.get("copy_code_value")

        results = []
        sent_count = 0
        failed_count = 0
        
        for recipient in recipients:
            phone = recipient.get("phone")
            if not phone:
                results.append({"phone": None, "success": False, "error": "Missing phone"})
                failed_count += 1
                continue
            
            variables = recipient.get("variables", [])

            success, result = _send_via_trigger(
                trigger, phone, variables, source_type, source_ref,
                header_image_url=recipient.get("header_image_url", global_img),
                header_video_url=recipient.get("header_video_url", global_vid),
                header_document_url=recipient.get("header_document_url", global_doc),
                header_document_id=recipient.get("header_document_id", global_doc_id),
                header_document_filename=recipient.get("header_document_filename", global_doc_fname),
                copy_code_value=recipient.get("copy_code_value", global_copy),
            )
            
            if success:
                results.append({"phone": phone, "success": True, "message_id": result})
                sent_count += 1
            else:
                results.append({"phone": phone, "success": False, "error": result})
                failed_count += 1
        
        logger.info(f"[Bulk Trigger] '{trigger.name}' - Sent: {sent_count}, Failed: {failed_count}")
        
        return jsonify({
            "success": True,
            "summary": {
                "total": len(recipients),
                "sent": sent_count,
                "failed": failed_count
            },
            "results": results
        })
        
    except Exception as e:
        logger.exception(f"Error in bulk trigger {trigger_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# E-Commerce Webhook Receiver
# ============================================================

@production_trigger_bp.route("/hooks/<int:trigger_id>/ecommerce", methods=["POST"])
def ecommerce_webhook(trigger_id: int):
    """
    Webhook receiver for e-commerce platforms (Shopify, WooCommerce, etc.)
    
    Requires 'secret' query param or 'X-Trigger-Secret' header.
    
    Supports automatic field mapping from:
    - Shopify Order webhooks
    - WooCommerce Order webhooks
    - Generic JSON with field_mapping
    
    Body (Shopify format):
    {
        "id": 123456,
        "order_number": "#1001",
        "customer": {
            "phone": "+1234567890",
            "first_name": "John",
            "last_name": "Doe"
        },
        "total_price": "99.99",
        ...
    }
    
    Body (Generic format with mapping):
    {
        "data": { ... your data ... },
        "field_mapping": {
            "phone": "customer.mobile",
            "variables": ["customer.name", "order.total", "order.id"]
        }
    }
    """
    secret = request.args.get("secret") or request.headers.get("X-Trigger-Secret")
    data = request.get_json(silent=True) or {}
    platform = request.args.get("platform", "auto")  # shopify, woocommerce, generic
    
    if not secret:
        return jsonify({"error": "Missing secret"}), 401
    
    try:
        trigger = WhatsAppTrigger.query.get(trigger_id)
        
        if not trigger or trigger.secret_key != secret:
            return jsonify({"error": "Invalid trigger or secret"}), 401
        
        if not trigger.is_active:
            return jsonify({"error": "Trigger is inactive"}), 400
        
        # Extract phone and variables based on platform
        phone = None
        variables = []
        source_ref = None
        
        if platform == "shopify" or ("customer" in data and "id" in data):
            # Shopify Order format
            customer = data.get("customer", {})
            phone = customer.get("phone") or data.get("phone") or data.get("billing_address", {}).get("phone")
            
            # Common variables for order templates
            variables = [
                customer.get("first_name", "Customer"),
                f"#{data.get('order_number', data.get('id', 'N/A'))}",
                data.get("total_price", "0"),
                data.get("currency", "USD"),
            ]
            source_ref = f"shopify_order_{data.get('id')}"
            
        elif platform == "woocommerce" or "billing" in data:
            # WooCommerce Order format
            billing = data.get("billing", {})
            phone = billing.get("phone")
            
            variables = [
                f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or "Customer",
                f"#{data.get('number', data.get('id', 'N/A'))}",
                data.get("total", "0"),
                data.get("currency", "USD"),
            ]
            source_ref = f"woo_order_{data.get('id')}"
            
        elif "field_mapping" in data:
            # Generic format with explicit mapping
            mapping = data.get("field_mapping", {})
            actual_data = data.get("data", data)
            
            phone = _extract_nested_value(actual_data, mapping.get("phone", "phone"))
            
            var_paths = mapping.get("variables", [])
            for path in var_paths:
                val = _extract_nested_value(actual_data, path)
                variables.append(str(val) if val is not None else "")
            
            source_ref = str(_extract_nested_value(actual_data, mapping.get("reference", "id")))
            
        else:
            # Try auto-detection
            phone = (data.get("phone") or 
                     data.get("mobile") or 
                     data.get("customer_phone") or
                     data.get("to"))
            variables = data.get("variables", [])
            source_ref = str(data.get("id") or data.get("reference") or "")
        
        # Validate phone
        if not phone:
            return jsonify({
                "error": "Could not extract phone number from webhook data",
                "hint": "Use ?platform=shopify|woocommerce or provide field_mapping"
            }), 400
        
        # Clean phone number
        phone = _clean_phone(phone)
        
        # Send message
        success, result = _send_via_trigger(
            trigger, phone, variables, f"ecommerce_{platform}", source_ref,
            header_image_url=data.get("header_image_url"),
            header_video_url=data.get("header_video_url"),
            header_document_url=data.get("header_document_url"),
            header_document_id=data.get("header_document_id"),
            header_document_filename=data.get("header_document_filename"),
            copy_code_value=data.get("copy_code_value"),
        )
        
        if success:
            return jsonify({
                "success": True,
                "message": "Trigger fired successfully",
                "message_id": result,
                "recipient": phone
            })
        else:
            return jsonify({
                "success": False,
                "error": result,
                "recipient": phone
            }), 502
            
    except Exception as e:
        logger.exception(f"Error in ecommerce webhook for trigger {trigger_id}: {e}")
        return jsonify({"error": str(e)}), 500


def _extract_nested_value(data: dict, path: str):
    """Extract value from nested dict using dot notation. e.g. 'customer.phone'"""
    if not path:
        return None
    keys = path.split(".")
    value = data
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def _clean_phone(phone: str) -> str:
    """Clean phone number - remove spaces, dashes, etc."""
    if not phone:
        return ""
    # Remove common formatting characters
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    # Remove leading + for WhatsApp API
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    return cleaned


# ============================================================
# Zapier/Make.com Compatible Endpoint
# ============================================================

@production_trigger_bp.route("/hooks/<int:trigger_id>/zapier", methods=["POST"])
def zapier_webhook(trigger_id: int):
    """
    Zapier/Make.com compatible endpoint with flat field structure.
    
    Requires 'secret' query param or 'X-Trigger-Secret' header.
    
    Body:
    {
        "phone": "1234567890",
        "var1": "John Doe",
        "var2": "Order #123",
        "var3": "99.99",
        "reference": "zap_12345"  // Optional
    }
    
    Variables are extracted from var1, var2, var3... varN in order.
    """
    secret = request.args.get("secret") or request.headers.get("X-Trigger-Secret")
    data = request.get_json(silent=True) or {}
    
    if not secret:
        return jsonify({"error": "Missing secret"}), 401
    
    try:
        trigger = WhatsAppTrigger.query.get(trigger_id)
        
        if not trigger or trigger.secret_key != secret:
            return jsonify({"error": "Invalid trigger or secret"}), 401
        
        if not trigger.is_active:
            return jsonify({"error": "Trigger is inactive"}), 400
        
        phone = data.get("phone") or data.get("to") or data.get("mobile")
        if not phone:
            return jsonify({"error": "'phone' field is required"}), 400
        
        phone = _clean_phone(phone)
        
        # Extract variables from var1, var2, var3... or variables array
        variables = data.get("variables", [])
        if not variables:
            # Try var1, var2, var3... pattern
            i = 1
            while True:
                val = data.get(f"var{i}")
                if val is None:
                    break
                variables.append(str(val))
                i += 1
        
        source_ref = data.get("reference") or data.get("zap_id")
        
        success, result = _send_via_trigger(
            trigger, phone, variables, "zapier", source_ref,
            header_image_url=data.get("header_image_url"),
            header_video_url=data.get("header_video_url"),
            header_document_url=data.get("header_document_url"),
            header_document_id=data.get("header_document_id"),
            header_document_filename=data.get("header_document_filename"),
            copy_code_value=data.get("copy_code_value"),
        )
        
        if success:
            return jsonify({
                "success": True,
                "message_id": result
            })
        else:
            return jsonify({
                "success": False,
                "error": result
            }), 502
            
    except Exception as e:
        logger.exception(f"Error in zapier webhook for trigger {trigger_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# Trigger Logs Endpoints
# ============================================================

@production_trigger_bp.route("/accounts/<int:account_id>/trigger-logs", methods=["GET"])
@require_account_access
def list_trigger_logs(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Get trigger invocation logs for debugging and analytics.
    
    Query params:
    - trigger_id: Filter by specific trigger
    - success: Filter by success (true/false)
    - phone: Filter by recipient phone
    - limit: Max results (default 50, max 200)
    - offset: Pagination offset
    - from_date: Filter from date (ISO format)
    - to_date: Filter to date (ISO format)
    """
    try:
        trigger_id = request.args.get("trigger_id", type=int)
        success = request.args.get("success")
        phone = request.args.get("phone")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        from_date = request.args.get("from_date")
        to_date = request.args.get("to_date")
        
        query = TriggerLog.query.filter_by(
            workspace_id=workspace_id,
            account_id=account_id
        )
        
        if trigger_id:
            query = query.filter_by(trigger_id=trigger_id)
        
        if success is not None:
            query = query.filter_by(success=success.lower() == "true")
        
        if phone:
            query = query.filter(TriggerLog.recipient_phone.contains(phone))
        
        if from_date:
            try:
                from_dt = datetime.fromisoformat(from_date.replace("Z", "+00:00"))
                query = query.filter(TriggerLog.created_at >= from_dt)
            except:
                pass
        
        if to_date:
            try:
                to_dt = datetime.fromisoformat(to_date.replace("Z", "+00:00"))
                query = query.filter(TriggerLog.created_at <= to_dt)
            except:
                pass
        
        total = query.count()
        logs = query.order_by(desc(TriggerLog.created_at)).offset(offset).limit(limit).all()
        
        return jsonify({
            "success": True,
            "logs": [log.to_dict() for log in logs],
            "total": total,
            "limit": limit,
            "offset": offset
        })
        
    except Exception as e:
        logger.exception(f"Error listing trigger logs: {e}")
        return jsonify({"error": "Failed to list trigger logs"}), 500


@production_trigger_bp.route("/accounts/<int:account_id>/trigger-analytics", methods=["GET"])
@require_account_access
def trigger_analytics(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Get analytics summary for triggers.
    
    Query params:
    - days: Number of days to analyze (default 7, max 30)
    - trigger_id: Filter by specific trigger (optional)
    """
    try:
        days = min(int(request.args.get("days", 7)), 30)
        trigger_id = request.args.get("trigger_id", type=int)
        
        from_date = datetime.now(timezone.utc) - timedelta(days=days)
        
        # Base query
        base_filter = and_(
            TriggerLog.workspace_id == workspace_id,
            TriggerLog.account_id == account_id,
            TriggerLog.created_at >= from_date
        )
        
        if trigger_id:
            base_filter = and_(base_filter, TriggerLog.trigger_id == trigger_id)
        
        # Summary stats
        total_sent = db.session.query(func.count(TriggerLog.id)).filter(
            base_filter, TriggerLog.success == True
        ).scalar() or 0
        
        total_failed = db.session.query(func.count(TriggerLog.id)).filter(
            base_filter, TriggerLog.success == False
        ).scalar() or 0
        
        delivered = db.session.query(func.count(TriggerLog.id)).filter(
            base_filter, TriggerLog.delivery_status == "delivered"
        ).scalar() or 0
        
        read = db.session.query(func.count(TriggerLog.id)).filter(
            base_filter, TriggerLog.delivery_status == "read"
        ).scalar() or 0
        
        # Per-trigger breakdown
        trigger_stats = db.session.query(
            TriggerLog.trigger_id,
            func.count(TriggerLog.id).label("total"),
            func.sum(func.cast(TriggerLog.success, db.Integer)).label("success_count")
        ).filter(base_filter).group_by(TriggerLog.trigger_id).all()
        
        # Get trigger names
        trigger_ids = [t[0] for t in trigger_stats]
        triggers = WhatsAppTrigger.query.filter(WhatsAppTrigger.id.in_(trigger_ids)).all()
        trigger_names = {t.id: t.name for t in triggers}
        
        per_trigger = []
        for t_id, total, success in trigger_stats:
            per_trigger.append({
                "trigger_id": t_id,
                "trigger_name": trigger_names.get(t_id, "Unknown"),
                "total": total,
                "success": success or 0,
                "failed": total - (success or 0)
            })
        
        # Daily breakdown
        daily_stats = db.session.query(
            func.date(TriggerLog.created_at).label("date"),
            func.count(TriggerLog.id).label("total"),
            func.sum(func.cast(TriggerLog.success, db.Integer)).label("success")
        ).filter(base_filter).group_by(func.date(TriggerLog.created_at)).order_by(func.date(TriggerLog.created_at)).all()
        
        daily = []
        for date, total, success in daily_stats:
            daily.append({
                "date": str(date),
                "total": total,
                "success": success or 0,
                "failed": total - (success or 0)
            })
        
        return jsonify({
            "success": True,
            "period_days": days,
            "summary": {
                "total_sent": total_sent,
                "total_failed": total_failed,
                "total": total_sent + total_failed,
                "delivered": delivered,
                "read": read,
                "delivery_rate": round(delivered / total_sent * 100, 1) if total_sent > 0 else 0,
                "read_rate": round(read / delivered * 100, 1) if delivered > 0 else 0
            },
            "per_trigger": per_trigger,
            "daily": daily
        })
        
    except Exception as e:
        logger.exception(f"Error getting trigger analytics: {e}")
        return jsonify({"error": "Failed to get analytics"}), 500


# ============================================================
# Contact List Management
# ============================================================

@production_trigger_bp.route("/accounts/<int:account_id>/contacts", methods=["GET"])
@require_account_access
def list_contacts(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """List contacts with optional filtering."""
    try:
        list_name = request.args.get("list")
        tag = request.args.get("tag")
        search = request.args.get("search")
        limit = min(int(request.args.get("limit", 50)), 200)
        offset = int(request.args.get("offset", 0))
        
        query = TriggerContact.query.filter_by(
            workspace_id=workspace_id,
            is_active=True
        )
        
        if list_name:
            query = query.filter_by(list_name=list_name)
        
        if tag:
            query = query.filter(TriggerContact.tags.contains(f'"{tag}"'))
        
        if search:
            query = query.filter(
                db.or_(
                    TriggerContact.phone.contains(search),
                    TriggerContact.name.contains(search),
                    TriggerContact.email.contains(search)
                )
            )
        
        total = query.count()
        contacts = query.order_by(TriggerContact.created_at.desc()).offset(offset).limit(limit).all()
        
        return jsonify({
            "success": True,
            "contacts": [c.to_dict() for c in contacts],
            "total": total,
            "limit": limit,
            "offset": offset
        })
        
    except Exception as e:
        logger.exception(f"Error listing contacts: {e}")
        return jsonify({"error": "Failed to list contacts"}), 500


@production_trigger_bp.route("/accounts/<int:account_id>/contacts/import", methods=["POST"])
@require_account_access
def import_contacts(account_id: int, account: WhatsAppAccount, workspace_id: str):
    """
    Import contacts from JSON array.
    
    Body:
    {
        "contacts": [
            {
                "phone": "1234567890",
                "name": "John Doe",
                "email": "john@example.com",
                "custom_fields": {"order_count": 5, "vip": true}
            }
        ],
        "list_name": "VIP Customers",  // Optional
        "tags": ["vip", "repeat"]  // Optional tags for all
    }
    """
    try:
        data = request.get_json() or {}
        contacts_data = data.get("contacts", [])
        list_name = data.get("list_name")
        tags = data.get("tags", [])
        
        if not contacts_data:
            return jsonify({"error": "No contacts provided"}), 400
        
        if len(contacts_data) > 1000:
            return jsonify({"error": "Maximum 1000 contacts per import"}), 400
        
        imported = 0
        updated = 0
        errors = []
        
        for idx, c_data in enumerate(contacts_data):
            phone = _clean_phone(c_data.get("phone", ""))
            if not phone:
                errors.append(f"Row {idx + 1}: Missing phone")
                continue
            
            # Check if exists
            existing = TriggerContact.query.filter_by(
                workspace_id=workspace_id,
                phone=phone
            ).first()
            
            if existing:
                # Update existing
                existing.name = c_data.get("name") or existing.name
                existing.email = c_data.get("email") or existing.email
                if c_data.get("custom_fields"):
                    existing.custom_fields = json.dumps(c_data["custom_fields"])
                if list_name:
                    existing.list_name = list_name
                if tags:
                    existing_tags = json.loads(existing.tags) if existing.tags else []
                    existing.tags = json.dumps(list(set(existing_tags + tags)))
                updated += 1
            else:
                # Create new
                contact = TriggerContact(
                    workspace_id=workspace_id,
                    phone=phone,
                    name=c_data.get("name"),
                    email=c_data.get("email"),
                    custom_fields=json.dumps(c_data.get("custom_fields", {})),
                    list_name=list_name,
                    tags=json.dumps(tags) if tags else None,
                    source="api_import"
                )
                db.session.add(contact)
                imported += 1
        
        db.session.commit()
        
        return jsonify({
            "success": True,
            "imported": imported,
            "updated": updated,
            "errors": errors[:10]  # Return first 10 errors
        })
        
    except Exception as e:
        db.session.rollback()
        logger.exception(f"Error importing contacts: {e}")
        return jsonify({"error": str(e)}), 500


@production_trigger_bp.route("/hooks/<int:trigger_id>/send-to-list", methods=["POST"])
def send_to_contact_list(trigger_id: int):
    """
    Send trigger to all contacts in a list.
    
    Requires 'secret' query param or 'X-Trigger-Secret' header.
    
    Body:
    {
        "list_name": "VIP Customers",
        "variable_mapping": {
            "1": "name",       // Map {{1}} to contact's name field
            "2": "custom_fields.order_id"  // Map {{2}} to custom_fields.order_id
        },
        "limit": 100  // Optional, max recipients
    }
    """
    secret = request.args.get("secret") or request.headers.get("X-Trigger-Secret")
    data = request.get_json(silent=True) or {}
    
    if not secret:
        return jsonify({"error": "Missing secret"}), 401
    
    try:
        trigger = WhatsAppTrigger.query.get(trigger_id)
        
        if not trigger or trigger.secret_key != secret:
            return jsonify({"error": "Invalid trigger or secret"}), 401
        
        if not trigger.is_active:
            return jsonify({"error": "Trigger is inactive"}), 400
        
        list_name = data.get("list_name")
        if not list_name:
            return jsonify({"error": "'list_name' is required"}), 400
        
        variable_mapping = data.get("variable_mapping", {})
        limit = min(int(data.get("limit", 100)), 500)
        
        # Get contacts
        contacts = TriggerContact.query.filter_by(
            workspace_id=trigger.workspace_id,
            list_name=list_name,
            is_active=True,
            opted_out=False
        ).limit(limit).all()
        
        if not contacts:
            return jsonify({"error": f"No active contacts in list '{list_name}'"}), 404
        
        results = []
        sent_count = 0
        failed_count = 0
        
        for contact in contacts:
            # Build variables from mapping
            variables = []
            for pos in sorted(variable_mapping.keys(), key=lambda x: int(x)):
                field_path = variable_mapping[pos]
                
                # Check standard fields first
                if field_path == "name":
                    val = contact.name
                elif field_path == "email":
                    val = contact.email
                elif field_path == "phone":
                    val = contact.phone
                elif field_path.startswith("custom_fields."):
                    sub_field = field_path.replace("custom_fields.", "")
                    val = contact.get_variable(sub_field)
                else:
                    val = contact.get_variable(field_path)
                
                variables.append(str(val) if val else "")
            
            success, result = _send_via_trigger(
                trigger, contact.phone, variables, "list_send", list_name
            )
            
            if success:
                results.append({"phone": contact.phone, "success": True})
                sent_count += 1
            else:
                results.append({"phone": contact.phone, "success": False, "error": result})
                failed_count += 1
        
        return jsonify({
            "success": True,
            "summary": {
                "total": len(contacts),
                "sent": sent_count,
                "failed": failed_count
            },
            "results": results
        })
        
    except Exception as e:
        logger.exception(f"Error sending to list via trigger {trigger_id}: {e}")
        return jsonify({"error": str(e)}), 500


# ============================================================
# API Documentation Endpoint
# ============================================================

@production_trigger_bp.route("/triggers/api-docs", methods=["GET"])
def get_api_docs():
    """
    Get API documentation for triggers.
    Useful for integrators and developers.
    """
    base_url = request.host_url.rstrip("/")
    
    return jsonify({
        "title": "WhatsApp Triggered Messages API",
        "version": "1.0",
        "base_url": f"{base_url}/api/whatsapp",
        "authentication": {
            "type": "Query Parameter or Header",
            "query_param": "secret",
            "header": "X-Trigger-Secret",
            "description": "Include your trigger's secret key in every request"
        },
        "endpoints": {
            "single_message": {
                "method": "POST",
                "path": "/hooks/{trigger_id}",
                "description": "Send a single template message",
                "body": {
                    "to": "string (required) - Recipient phone number",
                    "variables": "array (optional) - Template variables in order"
                }
            },
            "bulk_send": {
                "method": "POST",
                "path": "/hooks/{trigger_id}/bulk",
                "description": "Send to multiple recipients (max 100)",
                "body": {
                    "recipients": "[{phone, variables}]",
                    "source_type": "string (optional)",
                    "source_reference": "string (optional)"
                }
            },
            "ecommerce_webhook": {
                "method": "POST",
                "path": "/hooks/{trigger_id}/ecommerce?platform=shopify|woocommerce|generic",
                "description": "Receive webhooks from e-commerce platforms",
                "supported_platforms": ["shopify", "woocommerce", "generic with field_mapping"]
            },
            "zapier_webhook": {
                "method": "POST",
                "path": "/hooks/{trigger_id}/zapier",
                "description": "Zapier/Make.com compatible endpoint",
                "body": {
                    "phone": "string (required)",
                    "var1": "string (optional) - First variable",
                    "var2": "string (optional) - Second variable",
                    "varN": "... more variables"
                }
            },
            "send_to_list": {
                "method": "POST",
                "path": "/hooks/{trigger_id}/send-to-list",
                "description": "Send to all contacts in a list",
                "body": {
                    "list_name": "string (required)",
                    "variable_mapping": "{position: field_path}",
                    "limit": "number (optional, max 500)"
                }
            }
        },
        "examples": {
            "curl_single": 'curl -X POST "https://your-api.com/api/whatsapp/hooks/1?secret=YOUR_SECRET" -H "Content-Type: application/json" -d \'{"to": "1234567890", "variables": ["John", "Order #123"]}\'',
            "python": """
import requests

response = requests.post(
    "https://your-api.com/api/whatsapp/hooks/1?secret=YOUR_SECRET",
    json={"to": "1234567890", "variables": ["John", "Order #123"]}
)
print(response.json())
""",
            "javascript": """
fetch("https://your-api.com/api/whatsapp/hooks/1?secret=YOUR_SECRET", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({to: "1234567890", variables: ["John", "Order #123"]})
}).then(r => r.json()).then(console.log);
"""
        }
    })
