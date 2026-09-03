"""
WhatsApp Template Service
=========================

Handles template management operations:
  - sync_templates (full sync from Meta)
  - sync_single_template
  - create_draft_template / update_draft_template
  - delete_template / archive_template / duplicate_template
  - submit_template_to_meta / edit_meta_template
  - resumable_media_upload
  - _get_voice_call_capability
"""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple

import requests

from .models import WhatsAppAccount, WhatsAppTemplate

def _coerce_quality_score(qs):
    """Meta may return quality_score as a dict {'score': 'GREEN'|'YELLOW'|'RED'|'UNKNOWN', 'date': ...},
    a plain string, or None. The whatsapp_templates.quality_score column is String(32),
    so persist just the string score (psycopg2 cannot adapt a raw dict)."""
    if isinstance(qs, dict):
        return qs.get('score')
    if isinstance(qs, str):
        return qs
    return None

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com"


class WhatsAppTemplateService:
    """
    Mixin for WhatsApp template management operations.

    All methods expect self.db_session, self.phone_number_id,
    self.access_token, self.waba_id, and self.api_version to exist
    (provided by the base WhatsAppService via MRO).
    """

    # ============================================================
    # Template Sync
    # ============================================================

    def sync_templates(self) -> Dict[str, Any]:
        """
        Sync all templates from Meta for the current account.

        Handles:
        1. Pagination (fetches ALL templates)
        2. Updates existing templates
        3. Creates new templates
        4. Deletes local templates that no longer exist on Meta

        Returns:
            Sync stats dict
        """
        account = self._get_or_create_account()

        if not self.access_token:
            return {"success": False, "error": "No access token available"}

        try:
            logger.info(f"Starting template sync for account {account.id} (WABA: {account.waba_id})")

            # 1. Fetch ALL templates from Meta with pagination
            all_meta_templates = []
            base_url = f"{WHATSAPP_API_BASE}/{self.api_version}/{account.waba_id}/message_templates"
            next_url = base_url
            params = {"limit": 100}

            while next_url:
                resp = requests.get(
                    next_url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )

                if resp.status_code != 200:
                    logger.error(f"Meta template fetch failed: {resp.text}")
                    return {"success": False, "error": f"Meta API error: {resp.text}"}

                data = resp.json()
                templates_page = data.get("data", [])
                all_meta_templates.extend(templates_page)

                paging = data.get("paging", {})
                next_url = paging.get("next")
                params = {}

            logger.info(f"Fetched {len(all_meta_templates)} templates from Meta")

            # 2. Process Templates
            meta_template_ids = set()
            synced_count = 0
            created_count = 0
            updated_count = 0

            # Pre-fetch all local templates in one query to prevent N+1 query overhead
            local_templates = WhatsAppTemplate.query.filter_by(account_id=account.id).all()
            local_map = {(t.name, t.language): t for t in local_templates}

            for tpl in all_meta_templates:
                meta_id = tpl.get("id")
                name = tpl.get("name")
                language = tpl.get("language")
                status = tpl.get("status")
                category = tpl.get("category")

                meta_template_ids.add((name, language))

                existing = local_map.get((name, language))

                if existing:
                    existing.meta_template_id = meta_id
                    existing.status = status
                    existing.category = category
                    existing.components = tpl.get("components", [])
                    existing.rejection_reason = tpl.get("rejected_reason")
                    existing.quality_score = _coerce_quality_score(tpl.get("quality_score"))

                    for comp in tpl.get("components", []):
                        comp_type = comp.get("type", "").upper()
                        if comp_type == "BODY":
                            existing.body_text = comp.get("text", "")
                            all_vars = re.findall(r'\{\{([^}]+)\}\}', existing.body_text)
                            existing.variable_count = len(set(all_vars))
                        elif comp_type == "HEADER":
                            existing.header_text = comp.get("text", "")
                        elif comp_type == "FOOTER":
                            existing.footer_text = comp.get("text", "")

                    existing.last_synced_at = datetime.now(timezone.utc)
                    updated_count += 1
                else:
                    try:
                        new_tpl = WhatsAppTemplate.from_meta_template(account.id, tpl)
                    except AttributeError:
                        new_tpl = WhatsAppTemplate(
                            account_id=account.id,
                            meta_template_id=meta_id,
                            name=name,
                            language=language,
                            status=status,
                            category=category,
                            components=tpl.get("components", []),
                            rejection_reason=tpl.get("rejected_reason"),
                            quality_score=tpl.get("quality_score"),
                        )
                        for comp in tpl.get("components", []):
                            comp_type = comp.get("type", "").upper()
                            if comp_type == "BODY":
                                new_tpl.body_text = comp.get("text", "")
                            elif comp_type == "HEADER":
                                new_tpl.header_text = comp.get("text", "")
                            elif comp_type == "FOOTER":
                                new_tpl.footer_text = comp.get("text", "")

                    new_tpl.last_synced_at = datetime.now(timezone.utc)
                    self.db_session.add(new_tpl)
                    created_count += 1

                synced_count += 1

            # 3. Handle Deletions
            all_local_templates = WhatsAppTemplate.query.filter_by(account_id=account.id).all()
            deleted_count = 0

            for local_tpl in all_local_templates:
                if (local_tpl.name, local_tpl.language) not in meta_template_ids:
                    logger.info(f"Deleting local template {local_tpl.name} ({local_tpl.language}) - absent from Meta")
                    self.db_session.delete(local_tpl)
                    deleted_count += 1

            self.db_session.commit()

            return {
                "success": True,
                "total_fetched": len(all_meta_templates),
                "synced": synced_count,
                "created": created_count,
                "updated": updated_count,
                "deleted": deleted_count
            }

        except Exception as e:
            logger.exception(f"Template sync failed: {e}")
            self.db_session.rollback()
            return {"success": False, "error": str(e)}

    def sync_single_template(self, template_id: int) -> Dict[str, Any]:
        """
        Sync a single template from Meta by its local database ID.
        """
        template = WhatsAppTemplate.query.get(template_id)
        if not template:
            return {"success": False, "error": "Template not found in database"}

        account = WhatsAppAccount.query.get(template.account_id)
        if not account:
            return {"success": False, "error": "Account not found"}

        if not self.access_token:
            return {"success": False, "error": "No access token available"}

        try:
            logger.info(f"Syncing single template {template_id} (name: {template.name}, meta_id: {template.meta_template_id})")

            meta_template = None
            api_version = self.api_version

            # Strategy 1: Direct fetch by meta_template_id
            if template.meta_template_id:
                url = f"{WHATSAPP_API_BASE}/{api_version}/{template.meta_template_id}"
                params = {"fields": "id,name,status,category,language,components,rejected_reason,quality_score"}

                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )

                if resp.status_code == 200:
                    meta_template = resp.json()
                elif resp.status_code == 404:
                    logger.warning(f"Template {template.meta_template_id} not found on Meta, trying name search")
                else:
                    logger.warning(f"Meta API error for direct fetch: {resp.status_code} - {resp.text}")

            # Strategy 2: Search by name+language
            if not meta_template:
                url = f"{WHATSAPP_API_BASE}/{api_version}/{account.waba_id}/message_templates"
                params = {
                    "name": template.name,
                    "language": template.language,
                    "fields": "id,name,status,category,language,components,rejected_reason,quality_score"
                }

                resp = requests.get(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    params=params,
                    timeout=30
                )

                if resp.status_code == 200:
                    data = resp.json()
                    templates = data.get("data", [])
                    for tpl in templates:
                        if tpl.get("name") == template.name and tpl.get("language") == template.language:
                            meta_template = tpl
                            break
                else:
                    logger.error(f"Meta name search failed: {resp.status_code} - {resp.text}")
                    return {"success": False, "error": f"Meta API error: {resp.text}"}

            if not meta_template:
                logger.warning(f"Template {template.name} not found on Meta - may have been deleted")
                return {
                    "success": False,
                    "error": "Template not found on Meta. It may have been deleted.",
                    "deleted_on_meta": True
                }

            # Update local record with Meta data
            old_status = template.status
            template.meta_template_id = meta_template.get("id")
            template.status = meta_template.get("status")
            template.category = meta_template.get("category")
            template.rejection_reason = meta_template.get("rejected_reason")
            template.quality_score = _coerce_quality_score(meta_template.get("quality_score"))
            template.components = meta_template.get("components", [])
            template.last_synced_at = datetime.now(timezone.utc)

            for comp in meta_template.get("components", []):
                comp_type = comp.get("type", "").upper()
                if comp_type == "BODY":
                    template.body_text = comp.get("text", "")
                    all_vars = re.findall(r'\{\{([^}]+)\}\}', template.body_text)
                    template.variable_count = len(set(all_vars))
                elif comp_type == "HEADER":
                    template.header_text = comp.get("text", "")
                elif comp_type == "FOOTER":
                    template.footer_text = comp.get("text", "")

            if old_status == "PENDING" and template.status == "APPROVED" and template.submitted_at:
                template.approved_at = datetime.now(timezone.utc)
                template.approval_duration_seconds = int(
                    (template.approved_at - template.submitted_at).total_seconds()
                )

            self.db_session.commit()

            status_changed = old_status != template.status

            return {
                "success": True,
                "template": template.to_dict(),
                "status_changed": status_changed,
                "old_status": old_status,
                "new_status": template.status,
                "quality_score": meta_template.get("quality_score"),
                "last_synced_at": template.last_synced_at.isoformat() + "Z"
            }

        except Exception as e:
            logger.exception(f"Single template sync failed: {e}")
            self.db_session.rollback()
            return {"success": False, "error": str(e)}

    # ============================================================
    # Template Management (Native - Phase 2)
    # ============================================================

    def create_draft_template(self, account_id: int, data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Create a new template draft (Local Status: DRAFT, Meta Status: None)."""
        try:
            account = WhatsAppAccount.query.get(account_id)
            if not account:
                return {"error": "Account not found"}, False

            name = data.get("name", "").strip().lower().replace(" ", "_")
            language = data.get("language", "en_US")

            existing = WhatsAppTemplate.query.filter_by(
                account_id=account_id,
                name=name,
                language=language
            ).first()

            if existing:
                if existing.is_archived:
                    existing.is_archived = False
                    existing.local_status = "DRAFT"
                    existing.meta_status = None
                    existing.components = data.get("components")
                    existing.body_text = self._extract_text(data, "BODY")
                    existing.header_text = self._extract_text(data, "HEADER")
                    existing.footer_text = self._extract_text(data, "FOOTER")
                    self.db_session.commit()
                    return existing.to_dict(), True
                else:
                    return {"error": f"Template '{name}' ({language}) already exists."}, False

            template = WhatsAppTemplate(
                account_id=account_id,
                name=name,
                category=data.get("category", "UTILITY"),
                language=language,
                components=data.get("components", []),
                body_text=self._extract_text(data, "BODY"),
                header_text=self._extract_text(data, "HEADER"),
                footer_text=self._extract_text(data, "FOOTER"),
                variable_count=self._count_variables(self._extract_text(data, "BODY")),
                local_status="DRAFT",
                meta_status=None,
                status="PENDING",
                confidence_initial=data.get("confidence_initial", 0),
                validation_flags=data.get("validation_flags", []),
                detected_intent=data.get("detected_intent"),
                approval_path=data.get("approval_path"),
            )

            self.db_session.add(template)
            self.db_session.commit()

            return template.to_dict(), True

        except Exception as e:
            logger.exception(f"Error creating draft template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def update_draft_template(self, template_id: int, data: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
        """Update an existing template draft."""
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False

            if template.is_archived:
                return {"error": "Cannot edit archived template"}, False

            if "components" in data:
                template.components = data["components"]
                template.body_text = self._extract_text(data, "BODY")
                template.header_text = self._extract_text(data, "HEADER")
                template.footer_text = self._extract_text(data, "FOOTER")
                template.variable_count = self._count_variables(template.body_text)

            if "category" in data:
                template.category = data["category"]

            if template.local_status == "SUBMITTED":
                template.local_status = "DRAFT"

            template.updated_at = datetime.now(timezone.utc)
            self.db_session.commit()

            return template.to_dict(), True

        except Exception as e:
            logger.exception(f"Error updating draft: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def delete_template(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """Permanently delete a template from local DB and Meta (if linked)."""
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False

            meta_success = True
            meta_error = None

            if template.meta_template_id or (template.meta_status and template.meta_status != 'DRAFT'):
                try:
                    account = WhatsAppAccount.query.get(template.account_id)
                    access_token = account.get_access_token() if account else os.getenv("WHATSAPP_ACCESS_TOKEN")

                    if account and account.waba_id:
                        url = f"{WHATSAPP_API_BASE}/{self.api_version}/{account.waba_id}/message_templates"
                        params = {"name": template.name}
                        if template.meta_template_id:
                            params["hsm_id"] = template.meta_template_id

                        logger.info(f"Deleting template from Meta: {url} params={params}")
                        resp = requests.delete(url, params=params, headers={"Authorization": f"Bearer {access_token}"})
                    elif template.meta_template_id:
                        url = f"{WHATSAPP_API_BASE}/{self.api_version}/{template.meta_template_id}"
                        logger.info(f"Deleting template from Meta by ID: {url}")
                        resp = requests.delete(url, headers={"Authorization": f"Bearer {access_token}"})
                    else:
                        resp = None

                    if resp:
                        if resp.status_code == 200 or resp.status_code == 204:
                            logger.info(f"Deleted template {template.name} from Meta")
                        else:
                            logger.warning(f"Failed to delete from Meta: SC={resp.status_code} Body={resp.text}")
                            meta_success = False
                            try:
                                meta_error = resp.json().get("error", {}).get("message", "Unknown Meta Error")
                            except:
                                meta_error = resp.text
                except Exception as e:
                    logger.error(f"Error deleting from Meta: {e}")
                    meta_success = False
                    meta_error = str(e)

            self.db_session.delete(template)
            self.db_session.commit()

            result = {"success": True}
            if not meta_success:
                result["meta_warning"] = f"Deleted locally but failed on Meta: {meta_error}"

            return result, True

        except Exception as e:
            logger.exception(f"Error deleting template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    def archive_template(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """Soft delete (archive) a template."""
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False

            template.is_archived = True
            template.archived_at = datetime.now(timezone.utc)
            template.local_status = "ARCHIVED"

            self.db_session.commit()
            return template.to_dict(), True

        except Exception as e:
            logger.exception(f"Error archiving template: {e}")
            return {"error": str(e)}, False

    def duplicate_template(self, template_id: int, new_name: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
        """Duplicate a template as a new DRAFT."""
        try:
            original = WhatsAppTemplate.query.get(template_id)
            if not original:
                return {"error": "Original template not found"}, False

            base_name = new_name or f"{original.name}_copy"
            count = 1
            final_name = base_name
            while WhatsAppTemplate.query.filter_by(account_id=original.account_id, name=final_name, language=original.language).first():
                final_name = f"{base_name}_{count}"
                count += 1

            new_template = WhatsAppTemplate(
                account_id=original.account_id,
                name=final_name,
                category=original.category,
                language=original.language,
                components=original.components,
                body_text=original.body_text,
                header_text=original.header_text,
                footer_text=original.footer_text,
                variable_count=original.variable_count,
                local_status="DRAFT",
                meta_status=None,
                status="DRAFT",
            )

            self.db_session.add(new_template)
            self.db_session.commit()

            return new_template.to_dict(), True

        except Exception as e:
            logger.exception(f"Error duplicating template: {e}")
            self.db_session.rollback()
            return {"error": str(e)}, False

    # ============================================================
    # Meta API Integration
    # ============================================================

    def resumable_media_upload(self, file_path: str, mime_type: str, app_id: Optional[str] = None, access_token: Optional[str] = None) -> Optional[str]:
        """Perform a Resumable Upload to get a media handle (h)."""
        if not os.path.exists(file_path):
            logger.error(f"File not found for upload: {file_path}")
            return None

        target_app_id = app_id or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        if not access_token:
            access_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        file_size = os.path.getsize(file_path)

        if not target_app_id or not access_token:
            logger.error("Missing App ID or Access Token for upload. META_APP_ID/FB_APP_ID=%s, token=%s", target_app_id, bool(access_token))
            return None

        # Step 1: Create Session
        session_url = f"{WHATSAPP_API_BASE}/v22.0/{target_app_id}/uploads"
        params = {
            "file_length": file_size,
            "file_type": mime_type,
            "access_token": access_token
        }

        upload_id = None
        try:
            resp = requests.post(session_url, params=params, timeout=10)
            data = resp.json()
            if "id" not in data:
                logger.error(f"Failed to create upload session: {data}")
                return None
            upload_id = data["id"]
        except Exception as e:
            logger.exception(f"Exception creating upload session: {e}")
            return None

        # Step 2: Upload Binary (with retries)
        upload_url = f"{WHATSAPP_API_BASE}/v22.0/{upload_id}"

        for attempt in range(3):
            try:
                with open(file_path, 'rb') as f:
                    headers = {
                        "Authorization": f"OAuth {access_token}",
                        "file_offset": "0"
                    }
                    resp = requests.post(upload_url, data=f, headers=headers, timeout=60)
                    result = resp.json()

                    if "h" in result:
                        return result["h"]
                    else:
                        logger.warning(f"Upload attempt {attempt+1} failed: {result}")

            except Exception as e:
                logger.warning(f"Upload attempt {attempt+1} exception: {e}")

        logger.error("All media upload attempts failed.")
        return None

    def submit_template_to_meta(self, template_id: int) -> Tuple[Dict[str, Any], bool]:
        """Submit a local draft to Meta API."""
        try:
            from .flow_access import validate_template_flow_attachment

            template = WhatsAppTemplate.query.get(template_id)
            if not template:
                return {"error": "Template not found"}, False

            payload = {
                "name": template.name,
                "category": template.category,
                "components": template.components,
                "language": template.language,
                "allow_category_change": True
            }

            # CRITICAL: Authentication templates have a very strict schema.
            if template.category == 'AUTHENTICATION':
                logger.info(f"Transforming payload for AUTHENTICATION template: {template.name}")
                transformed_components = []
                for comp in template.components:
                    new_comp = comp.copy()
                    if new_comp.get('type') == 'BODY':
                        if 'text' in new_comp:
                            del new_comp['text']
                        new_comp['add_security_recommendation'] = True
                    elif new_comp.get('type') == 'FOOTER':
                        if 'text' in new_comp:
                            logger.info(f"Skipping invalid FOOTER 'text' for AUTHENTICATION template: {template.name}")
                            continue
                    transformed_components.append(new_comp)
                payload['components'] = transformed_components

            print(f"\n{'='*60}")
            print(f"=== SUBMITTING TEMPLATE TO META ===")
            print(f"Category: {template.category}")
            print(f"Payload: {json.dumps(payload, indent=2)}")
            print(f"{'='*60}\n")

            account = WhatsAppAccount.query.get(template.account_id)
            if not account or not account.waba_id:
                return {"error": "Invalid account WABA configuration"}, False

            access_token = account.get_access_token() or os.getenv("WHATSAPP_ACCESS_TOKEN")

            # Validate advanced buttons server-side
            has_catalog_button = False
            has_voice_call_button = False
            for comp in (template.components or []):
                if str(comp.get("type", "")).upper() != "BUTTONS":
                    continue

                for btn in (comp.get("buttons", []) or []):
                    btn_type = str(btn.get("type", "")).upper()

                    if btn_type == "FLOW":
                        flow_id = str(btn.get("flow_id") or "").strip()
                        if not flow_id:
                            return {"error": "Flow button requires flow_id"}, False
                        is_valid_flow, flow_error = validate_template_flow_attachment(template.account_id, flow_id)
                        if not is_valid_flow:
                            return {"error": flow_error}, False

                    if btn_type == "COPY_CODE":
                        code_example = str(btn.get("example") or btn.get("copy_code") or "").strip()
                        if code_example and len(code_example) > 15:
                            return {"error": "COPY_CODE example must be 15 characters or less"}, False

                    if btn_type == "CATALOG":
                        has_catalog_button = True

                    if btn_type == "VOICE_CALL":
                        has_voice_call_button = True

            if has_catalog_button:
                if str(template.category).upper() != "MARKETING":
                    return {"error": "Catalog button requires MARKETING category"}, False

                catalogs_resp = requests.get(
                    f"{WHATSAPP_API_BASE}/v22.0/{account.waba_id}/product_catalogs",
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15,
                )
                catalogs_data = catalogs_resp.json() if catalogs_resp.content else {}
                if catalogs_resp.ok:
                    connected_catalogs = catalogs_data.get("data", []) or []
                    if not connected_catalogs:
                        return {
                            "error": "No product catalog connected to this WhatsApp Business Account. Connect a catalog in WhatsApp Manager before using CATALOG button."
                        }, False
                else:
                    logger.warning("Catalog precheck failed during draft submission: %s", catalogs_data)

            if has_voice_call_button:
                capability = self._get_voice_call_capability(account, access_token)
                if capability.get("block_template_submission"):
                    return {
                        "error": "VOICE_CALL button is blocked until account call readiness checks pass.",
                        "capability": capability,
                    }, False

            # Post to Meta
            url = f"{WHATSAPP_API_BASE}/v22.0/{account.waba_id}/message_templates"
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            data = resp.json()

            if "id" in data:
                template.meta_template_id = data["id"]
                template.meta_status = data.get("status", "PENDING")
                template.local_status = "SUBMITTED"
                template.submitted_at = datetime.now(timezone.utc)
                template.status = template.meta_status

                self.db_session.commit()
                return template.to_dict(), True
            else:
                err_msg = data.get("error", {}).get("message", "Unknown Meta Error")
                return {"error": err_msg, "details": data}, False

        except Exception as e:
            logger.exception(f"Error submitting template: {e}")
            return {"error": str(e)}, False

    def edit_meta_template(self, template_id: int, remove_media: bool = False) -> Tuple[Dict[str, Any], bool]:
        """Update an EXISTING Meta template (Re-submission)."""
        try:
            template = WhatsAppTemplate.query.get(template_id)
            if not template or not template.meta_template_id:
                return {"error": "Template not found or not linked to Meta"}, False

            payload = {
                "components": template.components,
                "category": template.category
            }

            account = WhatsAppAccount.query.get(template.account_id)
            access_token = account.get_access_token() or os.getenv("WHATSAPP_ACCESS_TOKEN")

            url = f"{WHATSAPP_API_BASE}/v22.0/{template.meta_template_id}"
            resp = requests.post(url, json=payload, headers={"Authorization": f"Bearer {access_token}"}, timeout=20)
            data = resp.json()

            if "success" in data and data["success"]:
                template.meta_status = "PENDING"
                template.local_status = "SUBMITTED"
                template.submitted_at = datetime.now(timezone.utc)
                template.status = "PENDING"

                self.db_session.commit()
                return template.to_dict(), True
            else:
                err_msg = data.get("error", {}).get("message", "Unknown Meta Update Error")
                return {"error": err_msg, "details": data}, False

        except Exception as e:
            logger.exception(f"Error editing Meta template: {e}")
            return {"error": str(e)}, False

    # ============================================================
    # Helpers
    # ============================================================

    def _get_voice_call_capability(self, account: WhatsAppAccount, access_token: str) -> Dict[str, Any]:
        """Best-effort voice-call readiness probe for VOICE_CALL template buttons."""
        strict_mode = str(os.getenv("WHATSAPP_STRICT_VOICE_CALL_READINESS", "")).strip().lower() in {
            "1", "true", "yes", "on"
        }

        payload: Dict[str, Any] = {
            "strict_mode": strict_mode,
            "checks": {
                "has_phone_number_id": bool(account.phone_number_id),
                "has_access_token": bool(access_token),
                "meta_probe_ok": False,
                "phone_verified": False,
                "display_name_approved": False,
            },
            "warnings": [],
            "receive_in_sociovia_dashboard": False,
        }

        if not payload["checks"]["has_phone_number_id"]:
            payload["warnings"].append("Missing phone_number_id on this account")
        if not payload["checks"]["has_access_token"]:
            payload["warnings"].append("Missing access token on this account")

        code_verification_status = ""
        name_status = ""

        if payload["checks"]["has_phone_number_id"] and payload["checks"]["has_access_token"]:
            try:
                api_version = os.getenv("WHATSAPP_API_VERSION", "v22.0")
                fields = "code_verification_status,name_status,quality_rating"
                resp = requests.get(
                    f"{WHATSAPP_API_BASE}/{api_version}/{account.phone_number_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"fields": fields},
                    timeout=15,
                )
                probe = resp.json() if resp.content else {}
                if resp.ok:
                    payload["checks"]["meta_probe_ok"] = True
                    code_verification_status = str(probe.get("code_verification_status") or "").upper()
                    name_status = str(probe.get("name_status") or "").upper()
                else:
                    payload["warnings"].append(
                        probe.get("error", {}).get("message") or "Could not verify call readiness from Meta API"
                    )
            except Exception as e:
                payload["warnings"].append(str(e))

        payload["checks"]["phone_verified"] = code_verification_status in {"VERIFIED", "CONNECTED"}
        payload["checks"]["display_name_approved"] = (not name_status) or name_status in {"APPROVED", "AVAILABLE"}

        voice_calling_ready = bool(
            payload["checks"]["has_phone_number_id"]
            and payload["checks"]["has_access_token"]
            and payload["checks"]["phone_verified"]
            and payload["checks"]["display_name_approved"]
        )
        payload["voice_calling_ready"] = voice_calling_ready
        payload["block_template_submission"] = bool(strict_mode and not voice_calling_ready)
        return payload

    def _extract_text(self, data: Dict, type_str: str) -> str:
        """Helper to extract text from components list."""
        components = data.get("components", [])
        for comp in components:
            if comp.get("type") == type_str:
                return comp.get("text", "")
        return ""

    def _count_variables(self, text: str) -> int:
        """Helper to count {{x}} variables."""
        if not text:
            return 0
        matches = re.findall(r'\{\{([^}]+)\}\}', text)
        return len(set(matches))
