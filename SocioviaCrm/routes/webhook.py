# crm_management/routes/webhook.py
from flask import Blueprint, request, jsonify, current_app
from datetime import datetime
from typing import Optional, Tuple, Dict, Any
import time
from collections import defaultdict

bp = Blueprint("webhook", __name__, url_prefix="/webhook")


RATE_LIMIT_BUCKET = defaultdict(list)
MAX_REQUESTS_PER_MIN = 60   # per workspace


def _rate_limit_check(workspace_id: str):
    """
    Simple in-memory sliding-window rate limit.
    60 requests per workspace per minute.
    """
    if not workspace_id:
        return True, None   

    now = time.time()
    cutoff = now - 60

    bucket = RATE_LIMIT_BUCKET[workspace_id]
    RATE_LIMIT_BUCKET[workspace_id] = [t for t in bucket if t > cutoff]

    if len(RATE_LIMIT_BUCKET[workspace_id]) >= MAX_REQUESTS_PER_MIN:
        return False, f"Rate limit exceeded: {MAX_REQUESTS_PER_MIN} requests/min"

    RATE_LIMIT_BUCKET[workspace_id].append(now)
    return True, None


# ------------------- SECURITY: API KEY CHECK -------------------
def _validate_api_key(workspace_id):
    Setting = current_app.crm_models["Setting"]
    if not workspace_id:
        return False, "Missing workspace_id"

    ws_key = request.headers.get("X-Webhook-Key")
    if not ws_key:
        return False, "Missing X-Webhook-Key"

    db = current_app.db
    rec = (
        db.session.query(Setting)
        .filter_by(workspace_id=workspace_id, name="webhook_api_key")
        .first()
    )
    if not rec:
        return False, "Workspace webhook API key not set"

    if rec.value != ws_key:
        return False, "Invalid webhook key"

    return True, None


# ------------------- HELPERS -------------------
def _get_workspace_id_from_request(payload: dict) -> Optional[str]:
    ws = payload.get("workspace_id") or request.headers.get("X-Workspace-ID")
    return str(ws) if ws else None


def _get_lead_and_activity_models():
    models = getattr(current_app, "crm_models", {}) or {}
    return models.get("Lead"), models.get("Activity")


def _now():
    return datetime.utcnow()


def _safe_str(v):
    return None if v is None else str(v)


# ------------------- UPSERT LOGIC -------------------
def _upsert_lead_from_provider(
    *,
    external_source: str,
    external_id: Optional[str],
    payload_fields: Dict[str, Any],
    workspace_id: Optional[str],
):
    current_app.logger.info(f"Upserting lead. Payload fields: {payload_fields}")
    db = current_app.db
    Lead, _ = _get_lead_and_activity_models()
    if Lead is None:
        return None, False, "Lead model missing"

    now = _now()
    existing = None

    # 1) Try external_id match
    if external_id and hasattr(Lead, "external_id"):
        try:
            q = db.session.query(Lead)
            if workspace_id:
                q = q.filter(Lead.workspace_id == workspace_id)
            existing = q.filter(
                Lead.external_source == external_source,
                Lead.external_id == str(external_id),
            ).one_or_none()
        except Exception:
            existing = None

    # 2) Fallback match by email / phone
    email = payload_fields.get("email")
    phone = payload_fields.get("phone")

    if existing is None and (email or phone):
        try:
            q = db.session.query(Lead)
            if workspace_id:
                q = q.filter(Lead.workspace_id == workspace_id)
            if email:
                existing = q.filter(Lead.email == email).first()
            if not existing and phone:
                existing = q.filter(Lead.phone == phone).first()
        except Exception:
            existing = None

    created = False

    # ---------- UPDATE ----------
    if existing:
        try:
            updated = False
            for f in ("name", "email", "phone", "company", "job_title", "source", "details"):
                v = payload_fields.get(f)
                if v and getattr(existing, f, None) != v:
                    setattr(existing, f, v)
                    updated = True

            if hasattr(existing, "external_source"):
                existing.external_source = external_source

            if external_id and hasattr(existing, "external_id"):
                existing.external_id = str(external_id)

            if hasattr(existing, "sync_status"):
                existing.sync_status = "in_sync"

            if hasattr(existing, "last_sync_at"):
                existing.last_sync_at = now

            if updated:
                db.session.add(existing)
                db.session.commit()

            return existing, False, None
        except Exception as e:
            db.session.rollback()
            return None, False, "db_update_failed"

    # ---------- CREATE ----------
    try:
        create_kwargs = {
            "name": payload_fields.get("name") or payload_fields.get("email") or "Unknown",
            "email": email,
            "phone": phone,
            "company": payload_fields.get("company"),
            "job_title": payload_fields.get("job_title"),
            "source": payload_fields.get("source") or external_source,
            "details": payload_fields.get("details"),
            "status": "new",
            "workspace_id": workspace_id,
            "external_source": external_source,
            "external_id": external_id,
            "sync_status": "in_sync",
            "last_sync_at": now,
            "created_at": now,
            "updated_at": now,
        }

        lead_obj = Lead(**create_kwargs)
        db.session.add(lead_obj)
        db.session.commit()
        return lead_obj, True, None

    except Exception:
        db.session.rollback()
        return None, False, "db_create_failed"


# ------------------- ACTIVITY LOGGING -------------------
def _create_activity_for_lead(lead_obj, created, workspace_id, raw_payload):
    _, Activity = _get_lead_and_activity_models()
    if not Activity:
        return

    now = _now()
    try:
        a = Activity(
            entity_type="lead",
            entity_id=lead_obj.id,
            type="note_created" if created else "note_updated",
            title="Lead received" if created else "Lead updated",
            description=f"Payload={raw_payload}",
            timestamp=now,
            workspace_id=workspace_id,
        )
        db = current_app.db
        db.session.add(a)
        db.session.commit()
    except:
        db.session.rollback()


# ------------------- OUTBOUND SYNC: PUSH TO EXTERNAL CRM -------------------
def _get_outbound_config(workspace_id: str) -> Dict[str, Any]:
    """
    Get outbound sync configuration for a workspace.
    Stored in Settings with name 'outbound_sync_config'.
    """
    if not workspace_id:
        return {}
    
    try:
        Setting = current_app.crm_models.get("Setting")
        if not Setting:
            return {}
        
        db = current_app.db
        setting = db.session.query(Setting).filter(
            Setting.workspace_id == str(workspace_id),
            Setting.name == "outbound_sync_config"
        ).first()
        
        if setting and setting.value:
            import json
            try:
                return json.loads(setting.value)
            except:
                return {}
        return {}
    except:
        return {}


def _push_lead_to_webhook(lead_obj, config: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Push lead to user's custom webhook URL.
    """
    import requests as req
    
    webhook_url = config.get("webhook_url")
    if not webhook_url:
        return False, "No webhook URL configured"
    
    try:
        payload = {
            "event": "lead.created",
            "timestamp": _now().isoformat(),
            "lead": {
                "id": lead_obj.id,
                "name": lead_obj.name,
                "email": lead_obj.email,
                "phone": lead_obj.phone,
                "company": lead_obj.company,
                "job_title": lead_obj.job_title,
                "source": lead_obj.source,
                "status": lead_obj.status,
                "external_id": lead_obj.external_id,
                "created_at": lead_obj.created_at.isoformat() if lead_obj.created_at else None,
            }
        }
        
        # Add custom headers if configured
        headers = {"Content-Type": "application/json"}
        custom_headers = config.get("headers", {})
        headers.update(custom_headers)
        
        resp = req.post(
            webhook_url,
            json=payload,
            headers=headers,
            timeout=10
        )
        
        if resp.status_code in (200, 201, 202, 204):
            return True, f"Pushed to webhook: {resp.status_code}"
        else:
            return False, f"Webhook returned {resp.status_code}: {resp.text[:200]}"
    
    except Exception as e:
        return False, f"Webhook error: {str(e)}"


def _push_lead_to_hubspot(lead_obj, config: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Push lead to HubSpot CRM via their API.
    """
    import requests as req
    
    api_key = config.get("hubspot_api_key")
    if not api_key:
        return False, "No HubSpot API key configured"
    
    try:
        url = "https://api.hubapi.com/crm/v3/objects/contacts"
        
        payload = {
            "properties": {
                "firstname": (lead_obj.name or "").split()[0] if lead_obj.name else "",
                "lastname": " ".join((lead_obj.name or "").split()[1:]) if lead_obj.name else "",
                "email": lead_obj.email,
                "phone": lead_obj.phone,
                "company": lead_obj.company,
                "jobtitle": lead_obj.job_title,
                "hs_lead_status": "NEW",
            }
        }
        
        resp = req.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        
        if resp.status_code in (200, 201):
            result = resp.json()
            return True, f"HubSpot contact created: {result.get('id')}"
        else:
            return False, f"HubSpot error {resp.status_code}: {resp.text[:200]}"
    
    except Exception as e:
        return False, f"HubSpot error: {str(e)}"


def _push_lead_to_pipedrive(lead_obj, config: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Push lead to Pipedrive CRM via their API.
    """
    import requests as req
    
    api_key = config.get("pipedrive_api_key")
    if not api_key:
        return False, "No Pipedrive API key configured"
    
    try:
        url = f"https://api.pipedrive.com/v1/persons?api_token={api_key}"
        
        payload = {
            "name": lead_obj.name or lead_obj.email or "Unknown",
            "email": [{"value": lead_obj.email, "primary": True}] if lead_obj.email else [],
            "phone": [{"value": lead_obj.phone, "primary": True}] if lead_obj.phone else [],
        }
        
        resp = req.post(url, json=payload, timeout=15)
        
        if resp.status_code in (200, 201):
            result = resp.json()
            if result.get("success"):
                return True, f"Pipedrive person created: {result.get('data', {}).get('id')}"
            else:
                return False, f"Pipedrive error: {result.get('error')}"
        else:
            return False, f"Pipedrive error {resp.status_code}"
    
    except Exception as e:
        return False, f"Pipedrive error: {str(e)}"


def _sync_lead_outbound(lead_obj, workspace_id: str):
    """
    Main function to sync a lead to external CRM(s).
    Supports: Custom webhook, HubSpot, Pipedrive.
    """
    config = _get_outbound_config(workspace_id)
    if not config or not config.get("enabled"):
        return  # Outbound sync not configured
    
    db = current_app.db
    sync_results = []
    
    try:
        # Track sync attempts
        destinations = config.get("destinations", [])
        
        for dest in destinations:
            dest_type = dest.get("type")
            success = False
            message = ""
            
            if dest_type == "webhook":
                success, message = _push_lead_to_webhook(lead_obj, dest)
            elif dest_type == "hubspot":
                success, message = _push_lead_to_hubspot(lead_obj, dest)
            elif dest_type == "pipedrive":
                success, message = _push_lead_to_pipedrive(lead_obj, dest)
            else:
                message = f"Unknown destination type: {dest_type}"
            
            sync_results.append({
                "type": dest_type,
                "success": success,
                "message": message
            })
            
            current_app.logger.info(f"Outbound sync [{dest_type}]: {message}")
        
        # Update sync status
        all_success = all(r["success"] for r in sync_results) if sync_results else True
        
        if hasattr(lead_obj, "sync_status"):
            lead_obj.sync_status = "in_sync" if all_success else "sync_error"
        if hasattr(lead_obj, "last_sync_at"):
            lead_obj.last_sync_at = _now()
        if hasattr(lead_obj, "sync_error") and not all_success:
            errors = [r["message"] for r in sync_results if not r["success"]]
            lead_obj.sync_error = "; ".join(errors)[:500]
        
        db.session.commit()
        
    except Exception as e:
        current_app.logger.exception(f"Outbound sync failed for lead {lead_obj.id}: {e}")
        db.session.rollback()


# API: Configure outbound sync for a workspace
@bp.route("/config/outbound", methods=["POST"])
def configure_outbound_sync():
    """
    Configure outbound sync for a workspace.
    
    POST body:
    {
        "workspace_id": "41",
        "enabled": true,
        "destinations": [
            {
                "type": "webhook",
                "webhook_url": "https://your-crm.com/webhook",
                "headers": {"Authorization": "Bearer xxx"}
            },
            {
                "type": "hubspot",
                "hubspot_api_key": "xxx"
            },
            {
                "type": "pipedrive",
                "pipedrive_api_key": "xxx"
            }
        ]
    }
    """
    import json
    
    body = request.get_json() or {}
    workspace_id = body.get("workspace_id")
    
    if not workspace_id:
        return jsonify({"ok": False, "error": "workspace_id required"}), 400
    
    try:
        Setting = current_app.crm_models.get("Setting")
        if not Setting:
            return jsonify({"ok": False, "error": "Setting model not found"}), 500
        
        db = current_app.db
        
        # Find or create setting
        setting = db.session.query(Setting).filter(
            Setting.workspace_id == str(workspace_id),
            Setting.name == "outbound_sync_config"
        ).first()
        
        config = {
            "enabled": body.get("enabled", True),
            "destinations": body.get("destinations", [])
        }
        
        if setting:
            setting.value = json.dumps(config)
        else:
            setting = Setting(
                workspace_id=str(workspace_id),
                name="outbound_sync_config",
                value=json.dumps(config),
                masked=True
            )
            db.session.add(setting)
        
        db.session.commit()
        
        return jsonify({
            "ok": True,
            "message": "Outbound sync configured",
            "config": config
        }), 200
        
    except Exception as e:
        current_app.logger.exception("Failed to configure outbound sync")
        return jsonify({"ok": False, "error": str(e)}), 500


# ------------------- PROVIDER PARSERS -------------------
def _parse_meta_payload(payload):
    p = payload.get("lead") or {}
    return (
        _safe_str(
            p.get("id")
            or p.get("lead_id")
            or p.get("leadgen_id")
            or p.get("leadgen_id_string")
        ),
        {
            "name": p.get("name") or p.get("full_name"),
            "email": p.get("email"),
            "phone": p.get("phone"),
            "company": p.get("company"),
            "job_title": p.get("job_title"),
            "source": "Facebook Ad",
        },
    )


def _parse_zapier_payload(payload):
    d = payload.get("data") or payload.get("body") or payload
    return (
        _safe_str(d.get("id") or d.get("record_id")),
        {
            "name": d.get("name"),
            "email": d.get("email"),
            "phone": d.get("phone"),
            "company": d.get("company"),
            "job_title": d.get("job_title"),
            "source": "Zapier",
        },
    )


def _parse_sheets_payload(payload):
    row = payload.get("row") or payload
    return (
        _safe_str(row.get("id")),
        {
            "name": row.get("name"),
            "email": row.get("email"),
            "phone": row.get("phone"),
            "company": row.get("company"),
            "job_title": row.get("job_title"),
            "source": "Google Sheets",
        },
    )


def _parse_typeform_payload(payload):
    fr = payload.get("form_response") or {}
    return (
        _safe_str(fr.get("response_id") or fr.get("token")),
        {
            "name": payload.get("name"),
            "email": payload.get("email"),
            "phone": payload.get("phone"),
            "company": payload.get("company"),
            "job_title": payload.get("job_title"),
            "source": "Typeform",
        },
    )


def _parse_hubspot_payload(payload):
    props = payload.get("properties") or {}
    if "properties" in props:
        props = {k: v.get("value") for k, v in props["properties"].items()}

    return (
        _safe_str(payload.get("objectId")),
        {
            "name": props.get("firstname"),
            "email": props.get("email"),
            "phone": props.get("phone"),
            "company": props.get("company"),
            "job_title": props.get("jobtitle"),
            "source": "HubSpot",
        },
    )


def _parse_pipedrive_payload(payload):
    # Pipedrive sends data in "current" for updated events, or flat for create
    d = payload.get("current") or payload
    
    # Handle Pipedrive's nested email/phone format
    email = None
    phone = None
    if isinstance(d.get("email"), list):
        email = d["email"][0].get("value") if d["email"] else None
    else:
        email = d.get("email")
    
    if isinstance(d.get("phone"), list):
        phone = d["phone"][0].get("value") if d["phone"] else None
    else:
        phone = d.get("phone")
    
    return (
        _safe_str(d.get("id")),
        {
            "name": d.get("name") or d.get("person_name"),
            "email": email or d.get("person_email"),
            "phone": phone or d.get("person_phone"),
            "company": d.get("org_name"),
            "job_title": d.get("job_title"),
            "source": "Pipedrive",
        },
    )


# ------------------- META FIELD MAPPING HELPER -------------------
def _map_meta_fields_to_crm(raw_fields: Dict[str, Any]) -> Dict[str, Any]:
    """
    Robustly map varied Meta form fields to standard CRM fields.
    Handles snake_case, spaces, and common variations.
    """
    mapped = {}
    
    # helper to find first matching key in raw_fields
    def get_val(aliases):
        for alias in aliases:
            # try exact match
            if alias in raw_fields:
                return raw_fields[alias]
            # try case-insensitive / normalized match
            for k, v in raw_fields.items():
                if k.lower().replace(" ", "_") == alias:
                    return v
        return None

    # 1. NAME
    # Try fully constructed first
    name = get_val(["full_name", "fullname", "name"])
    if not name:
        fname = get_val(["first_name", "firstname", "fname"])
        lname = get_val(["last_name", "lastname", "surname", "lname"])
        if fname:
            name = f"{fname} {lname or ''}".strip()
    
    mapped["name"] = name or "Unknown"

    # 2. EMAIL
    mapped["email"] = get_val([
        "email", "work_email", "email_address", "contact_email", "user_email", "business_email"
    ])

    # 3. PHONE
    mapped["phone"] = get_val([
        "phone_number", "phone", "work_phone", "mobile", "full_phone_number", "contact_number", "whatsapp"
    ])

    # 4. COMPANY
    mapped["company"] = get_val([
        "company_name", "company", "business_name", "organization", "company_text"
    ])

    # 5. JOB TITLE
    mapped["job_title"] = get_val([
        "job_title", "jobtitle", "title", "role", "designation", "position", "current_role"
    ])
    
    mapped["source"] = "Facebook Lead Ad"

    # Build details: user-submitted form answers + ad attribution metadata
    details = {k: v for k, v in raw_fields.items() if not k.startswith("_meta_")}
    ad_attribution = {}
    for k, v in raw_fields.items():
        if k.startswith("_meta_") and v:
            ad_attribution[k[6:]] = v  # strip "_meta_" prefix
    if ad_attribution:
        details["ad_attribution"] = ad_attribution
    mapped["details"] = details
    return mapped


# ------------------- ROUTES -------------------
def _handle(provider, parser, payload):
    external_id, fields = parser(payload)
    workspace_id = _get_workspace_id_from_request(payload)

    # --- API KEY CHECK ---
    ok, err = _validate_api_key(workspace_id)
    if not ok:
        return jsonify({"ok": False, "reason": err}), 401

    # --- RATE LIMIT ---
    ok, reason = _rate_limit_check(workspace_id)
    if not ok:
        return jsonify({"ok": False, "reason": reason}), 429

    # --- UPSERT ---
    lead_obj, created, err = _upsert_lead_from_provider(
        external_source=provider,
        external_id=external_id,
        payload_fields=fields,
        workspace_id=workspace_id,
    )

    if err:
        return jsonify({"ok": False, "reason": err}), 500

    _create_activity_for_lead(lead_obj, created, workspace_id, payload)

    return jsonify(
        {"ok": True, "lead_id": lead_obj.id, "created": created}
    ), (201 if created else 200)


# Webhook Endpoints
@bp.route("/zapier", methods=["POST"])
def zapier():
    return _handle("zapier", _parse_zapier_payload, request.get_json() or {})


@bp.route("/meta-lead", methods=["POST"])
def meta_lead():
    return _handle("meta", _parse_meta_payload, request.get_json() or {})


@bp.route("/sheets", methods=["POST"])
def sheets():
    return _handle("sheets", _parse_sheets_payload, request.get_json() or {})


@bp.route("/typeform", methods=["POST"])
def typeform():
    return _handle("typeform", _parse_typeform_payload, request.get_json() or {})


@bp.route("/hubspot", methods=["POST"])
def hubspot():
    return _handle("hubspot", _parse_hubspot_payload, request.get_json() or {})


@bp.route("/pipedrive", methods=["POST"])
def pipedrive():
    return _handle("pipedrive", _parse_pipedrive_payload, request.get_json() or {})


# generic catch-all
@bp.route("/provider/<provider>", methods=["POST"])
def generic_provider(provider):
    return _handle(
        provider.lower(),
        lambda p: (None, {"name": p.get("name"), "email": p.get("email")}),
        request.get_json() or {},
    )


VERIFY_TOKEN = "sociovia_leadgen_webhook_2024"  # Configure this in Meta App settings

@bp.route("/meta/leadgen", methods=["GET", "POST", "OPTIONS"])
def meta_leadgen_realtime():
    current_app.logger.info(f"Meta leadgen endpoint hit: {request.method}")
    if request.method == "OPTIONS":
        return jsonify({"ok": True}), 200
    """
    Handle Meta's leadgen webhook:
    - GET: Webhook verification (hub.challenge response)
    - POST: Real-time lead notification processing
    """
    if request.method == "GET":
        # Webhook verification from Meta
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == "subscribe" and token == VERIFY_TOKEN:
            current_app.logger.info("Meta leadgen webhook verified successfully")
            return challenge, 200
        else:
            current_app.logger.warning(f"Meta leadgen webhook verification failed: mode={mode}, token={token}")
            return "Verification failed", 403
    
    # POST: Receive lead notification
    try:
        payload = request.get_json() or {}
        current_app.logger.info(f"Meta leadgen webhook received: {payload}")
        
        # Meta sends leadgen notifications in this format:
        # { "entry": [{ "changes": [{ "field": "leadgen", "value": { "leadgen_id": "...", "page_id": "...", "form_id": "...", "ad_id": "...", "adgroup_id": "...", "created_time": ... } }] }] }
        
        entries = payload.get("entry", [])
        leads_processed = 0
        
        for entry in entries:
            changes = entry.get("changes", [])
            for change in changes:
                if change.get("field") != "leadgen":
                    continue
                
                value = change.get("value", {})
                leadgen_id = value.get("leadgen_id")
                page_id = value.get("page_id")
                form_id = value.get("form_id")
                ad_id = value.get("ad_id")
                adgroup_id = value.get("adgroup_id")
                created_time = value.get("created_time")
                
                if not leadgen_id:
                    continue
                
                # Find workspace and campaign mapping for this form
                workspace_id, campaign = _find_workspace_for_lead_form(form_id)
                
                # Fallback: use test_workspace_id from payload for testing
                if not workspace_id:
                    workspace_id = payload.get("test_workspace_id")
                    if workspace_id:
                        current_app.logger.info(f"Using test_workspace_id fallback: {workspace_id}")
                
                # Wait a bit for Meta to propagate data (race condition fix)
                time.sleep(2)
                
                # Fetch full lead data from Meta (pass workspace_id and form_id to help find token)
                lead_data = _fetch_lead_from_meta(leadgen_id, page_id, workspace_id=workspace_id, form_id=form_id)
                current_app.logger.info(f"lead_data from Meta: {lead_data}")
                current_app.logger.info(f"test_lead_data in payload: {payload.get('test_lead_data')}")
                
                # Fallback: use test_lead_data from payload for testing
                if not lead_data and payload.get("test_lead_data"):
                    lead_data = payload.get("test_lead_data")
                    current_app.logger.info(f"Using test_lead_data fallback: {lead_data}")
                
                current_app.logger.info(f"Final lead_data: {lead_data}, workspace_id: {workspace_id}")
                
                if lead_data:
                    # Ensure webhook-level attribution is available for mapping
                    if form_id and "_meta_form_id" not in lead_data:
                        lead_data["_meta_form_id"] = str(form_id)
                    if ad_id and "_meta_ad_id" not in lead_data:
                        lead_data["_meta_ad_id"] = str(ad_id)
                    if adgroup_id and "_meta_adgroup_id" not in lead_data:
                        lead_data["_meta_adgroup_id"] = str(adgroup_id)
                    if campaign and "_meta_campaign_name" not in lead_data:
                        lead_data["_meta_campaign_name"] = campaign.name
                    if campaign and "_meta_campaign_id" not in lead_data:
                        lead_data["_meta_campaign_id"] = str(campaign.id)

                    # Upsert into CRM
                    lead_obj, created, err = _upsert_lead_from_provider(
                        external_source="meta_leadgen",
                        external_id=str(leadgen_id),
                        payload_fields=_map_meta_fields_to_crm(lead_data),
                        workspace_id=workspace_id,
                    )
                    
                    if lead_obj:
                        leads_processed += 1
                        # Include full webhook context in activity log
                        _create_activity_for_lead(lead_obj, created, workspace_id, {
                            "source": "meta_leadgen_webhook",
                            "fetched_data": lead_data,
                            "form_id": form_id,
                            "leadgen_id": leadgen_id,
                            "ad_id": ad_id,
                            "adgroup_id": adgroup_id,
                            "created_time": created_time,
                            "campaign_id": campaign.id if campaign else None,
                            "campaign_name": campaign.name if campaign else None,
                        })
                        
                        # Push to external CRM(s) if configured
                        if created:  # Only sync newly created leads
                            _sync_lead_outbound(lead_obj, workspace_id)
                        
                        current_app.logger.info(f"Lead processed: id={lead_obj.id}, created={created}, campaign={campaign.id if campaign else 'N/A'}")
        
        return jsonify({"ok": True, "leads_processed": leads_processed}), 200
        
    except Exception as e:
        current_app.logger.exception(f"Meta leadgen webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def _find_workspace_for_lead_form(form_id: str):
    """
    Find the workspace_id associated with a lead form using Campaign table.
    Campaigns store lead_form_id in their details JSON column.
    Returns (workspace_id, campaign) tuple.
    """
    if not form_id:
        return None, None
    
    try:
        Campaign = current_app.crm_models.get("Campaign")
        if not Campaign:
            current_app.logger.warning("Campaign model not found")
            return None, None
        
        db = current_app.db
        form_id_str = str(form_id)
        
        # Query campaigns where details JSON contains the lead_form_id
        # SQLAlchemy JSON column query
        campaigns = db.session.query(Campaign).filter(
            Campaign.details.isnot(None)
        ).all()
        
        for campaign in campaigns:
            details = campaign.details or {}
            if isinstance(details, str):
                import json
                try:
                    details = json.loads(details)
                except:
                    continue
            
            # Check if lead_form_id matches
            campaign_form_id = details.get("lead_form_id")
            if campaign_form_id and str(campaign_form_id) == form_id_str:
                current_app.logger.info(f"Found campaign {campaign.id} for form {form_id}")
                return campaign.workspace_id, campaign
        
        current_app.logger.warning(f"No campaign found for form_id {form_id}")
        return None, None
        
    except Exception as e:
        current_app.logger.exception(f"Error finding workspace for form {form_id}: {e}")
        return None, None


def _fetch_lead_from_meta(leadgen_id: str, page_id: str, workspace_id: str = None, form_id: str = None):
    """
    Fetch full lead data from Meta Graph API using the leadgen_id.
    Uses the workspace's system user token from SocialAccount.
    """
    import requests
    
    try:
        from models import SocialAccount
        
        db = current_app.db
        access_token = None
        
        # Strategy 1: Find token via workspace_id if provided
        if workspace_id:
            # Find Facebook SocialAccount for this workspace
            social = db.session.query(SocialAccount).filter(
                SocialAccount.provider == "facebook",
                SocialAccount.workspace_id == str(workspace_id)
            ).first()
            
            if social and social.access_token:
                access_token = social.access_token
                current_app.logger.info(f"Using token from workspace {workspace_id}")
        
        # Strategy 2: Find token via Campaign if form_id provided
        if not access_token and form_id:
            Campaign = current_app.crm_models.get("Campaign")
            if Campaign:
                campaigns = db.session.query(Campaign).filter(
                    Campaign.details.isnot(None)
                ).all()
                
                for campaign in campaigns:
                    details = campaign.details or {}
                    if isinstance(details, str):
                        import json
                        try:
                            details = json.loads(details)
                        except:
                            continue
                    
                    if str(details.get("lead_form_id")) == str(form_id):
                        # Found campaign, now get token for this workspace
                        social = db.session.query(SocialAccount).filter(
                            SocialAccount.provider == "facebook",
                            SocialAccount.workspace_id == str(campaign.workspace_id)
                        ).first()
                        
                        if social and social.access_token:
                            access_token = social.access_token
                            current_app.logger.info(f"Using token from campaign's workspace {campaign.workspace_id}")
                        break
        
        # Strategy 3: Fallback - find any Facebook token (last resort)
        if not access_token:
            social = db.session.query(SocialAccount).filter(
                SocialAccount.provider == "facebook",
                SocialAccount.access_token.isnot(None)
            ).first()
            
            if social and social.access_token:
                access_token = social.access_token
                current_app.logger.info(f"Using fallback token from SocialAccount {social.id}")
        
        if not access_token:
            current_app.logger.warning(f"No access token found for lead {leadgen_id}")
            return None
        
        # Fetch lead data from Meta
        url = f"https://graph.facebook.com/v21.0/{leadgen_id}"
        resp = requests.get(
            url,
            params={
                "access_token": access_token,
                "fields": "id,created_time,field_data,form_id,ad_id,campaign_id,adgroup_id"
            },
            timeout=30
        )

        data = resp.json()
        if data.get("error"):
            current_app.logger.warning(f"Meta API error fetching lead: {data}")
            return None

        # Parse field_data into a flat dict
        lead_fields = {}
        for field in data.get("field_data", []):
            name = field.get("name", "").lower().replace(" ", "_")
            values = field.get("values", [])
            if values:
                lead_fields[name] = values[0]

        # Carry ad attribution metadata so the caller can store it on the lead
        for meta_key in ("form_id", "ad_id", "campaign_id", "adgroup_id"):
            if data.get(meta_key):
                lead_fields[f"_meta_{meta_key}"] = str(data[meta_key])

        # Fetch the form name from Meta so we can label which form the lead came from
        _form_id = data.get("form_id")
        if _form_id and access_token:
            try:
                form_resp = requests.get(
                    f"https://graph.facebook.com/v21.0/{_form_id}",
                    params={"access_token": access_token, "fields": "name"},
                    timeout=10,
                )
                form_data = form_resp.json()
                if form_data.get("name"):
                    lead_fields["_meta_form_name"] = form_data["name"]
            except Exception:
                pass

        current_app.logger.info(f"Parsed Meta lead fields for {leadgen_id}: {lead_fields}")
        return lead_fields
        
    except Exception as e:
        current_app.logger.exception(f"Error fetching lead from Meta: {e}")
        return None


# ------------------- FETCH HISTORICAL LEADS FROM FORM -------------------
@bp.route("/meta/form/<form_id>/leads", methods=["POST"])
def fetch_form_leads(form_id):
    """
    Fetch existing leads from a Meta lead form and import them to CRM.
    This gets leads that came in BEFORE webhook subscription.
    
    POST body:
    {
        "workspace_id": "41",
        "page_access_token": "EAA...",  // Optional - will try to find from SocialAccount if not provided
        "limit": 50  // Optional, default 50, max 500
    }
    """
    try:
        import requests as req
        
        body = request.get_json() or {}
        workspace_id = body.get("workspace_id")
        access_token = body.get("page_access_token")
        limit = min(body.get("limit", 50), 500)
        
        if not workspace_id:
            return jsonify({"ok": False, "error": "workspace_id required"}), 400
        
        # Try to get access token from body or find from campaign
        if not access_token:
            # Try to find token from campaign that uses this form
            Campaign = current_app.crm_models.get("Campaign")
            if Campaign:
                db = current_app.db
                campaigns = db.session.query(Campaign).filter(
                    Campaign.workspace_id == str(workspace_id),
                    Campaign.details.isnot(None)
                ).all()
                
                for campaign in campaigns:
                    details = campaign.details or {}
                    if isinstance(details, str):
                        import json
                        try:
                            details = json.loads(details)
                        except:
                            continue
                    
                    if str(details.get("lead_form_id")) == str(form_id):
                        # Found a campaign with this form, now find token from payload
                        payload = campaign.payload or {}
                        if isinstance(payload, str):
                            try:
                                payload = json.loads(payload)
                            except:
                                pass
                        # Token might be in payload or we need to fetch from SocialAccount
                        break
        
        if not access_token:
            return jsonify({
                "ok": False, 
                "error": "page_access_token required - couldn't find one automatically"
            }), 400
        
        # Fetch leads from Meta Graph API
        api_version = "v21.0"
        url = f"https://graph.facebook.com/{api_version}/{form_id}/leads"
        
        all_leads = []
        next_url = url
        
        while next_url and len(all_leads) < limit:
            resp = req.get(
                next_url,
                params={
                    "access_token": access_token,
                    "fields": "id,created_time,field_data",
                    "limit": min(50, limit - len(all_leads))
                } if next_url == url else {},
                timeout=30
            )
            
            data = resp.json()
            
            if data.get("error"):
                return jsonify({
                    "ok": False,
                    "error": "meta_api_error",
                    "details": data.get("error")
                }), 400
            
            leads_batch = data.get("data", [])
            all_leads.extend(leads_batch)
            
            # Check for pagination
            paging = data.get("paging", {})
            next_url = paging.get("next")
        
        # Import leads to CRM
        leads_imported = 0
        leads_skipped = 0
        
        for lead in all_leads:
            leadgen_id = lead.get("id")
            
            # Parse field_data
            lead_fields = {}
            for field in lead.get("field_data", []):
                name = field.get("name", "").lower().replace(" ", "_")
                values = field.get("values", [])
                if values:
                    lead_fields[name] = values[0]
            
            # Upsert into CRM
            lead_obj, created, err = _upsert_lead_from_provider(
                external_source="meta_leadgen",
                external_id=str(leadgen_id),
                payload_fields=_map_meta_fields_to_crm(lead_fields),
                workspace_id=workspace_id,
            )
            
            if err:
                current_app.logger.warning(f"Failed to import lead {leadgen_id}: {err}")
                leads_skipped += 1
            elif created:
                leads_imported += 1
                _create_activity_for_lead(lead_obj, True, workspace_id, {
                    "source": "meta_form_import",
                    "form_id": form_id,
                    "leadgen_id": leadgen_id,
                    "created_time": lead.get("created_time"),
                })
            else:
                leads_skipped += 1  # Already existed
        
        current_app.logger.info(f"Imported {leads_imported} leads from form {form_id}")
        
        return jsonify({
            "ok": True,
            "form_id": form_id,
            "total_fetched": len(all_leads),
            "leads_imported": leads_imported,
            "leads_skipped": leads_skipped,
        }), 200
        
    except Exception as e:
        current_app.logger.exception(f"Error fetching leads from form {form_id}: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500
