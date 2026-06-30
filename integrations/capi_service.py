"""
Meta Conversions API helper for the WhatsApp service (standalone).

Uses ``CapiAccount`` / ``CapiEvent`` rows from shared PostgreSQL — no SocioviaCrm imports.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import traceback
import uuid
from datetime import datetime

import requests
from flask import current_app

logger = logging.getLogger(__name__)


def _hash_data(data) -> str | None:
    """SHA-256 (hex) of a PII value: trim + lowercase the STRING before encoding.
    Caller must pre-normalize phone to digits-only."""
    if data is None or str(data).strip() == "":
        return None
    return hashlib.sha256(str(data).strip().lower().encode("utf-8")).hexdigest()


def _normalize_phone(raw) -> str | None:
    """Phone → digits-only incl. country code (E.164 without '+') BEFORE hashing,
    per Meta. Reuse the region-aware normalizer when available, else strip to
    digits."""
    if raw is None or str(raw).strip() == "":
        return None
    try:
        from whatsapp.utils import normalize_phone_robust
        e164 = normalize_phone_robust(str(raw))
        if e164:
            digits = re.sub(r"\D", "", str(e164))
            if digits:
                return digits
    except Exception:
        pass
    digits = re.sub(r"\D", "", str(raw))
    return digits or None


def _post_with_retry(url, params, json_body, timeout=10, attempts=3):
    """POST with bounded retry on TRANSIENT failures (network, 5xx, Meta code 2)."""
    last_exc = None
    for i in range(attempts):
        try:
            resp = requests.post(url, params=params, json=json_body, timeout=timeout)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    try:
                        code = (resp.json().get("error") or {}).get("code")
                    except Exception:
                        code = None
                    if code == 2 and i < attempts - 1:
                        time.sleep(0.5 * (2 ** i))
                        continue
                return resp
            if i < attempts - 1:
                time.sleep(0.5 * (2 ** i))
                continue
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if i < attempts - 1:
                time.sleep(0.5 * (2 ** i))
                continue
            raise
    if last_exc:
        raise last_exc


def _log_to_db(
    *,
    workspace_id,
    event_name: str,
    action_source: str,
    event_source_url: str | None,
    user_data_json: dict,
    custom_data_json: dict | None,
    status: str,
    pixel_id: str | None = None,
    api_response: dict | None = None,
    error_message: str | None = None,
    ext_event_id: str | None = None,
) -> dict:
    if not current_app or not workspace_id:
        return {"success": status == "sent", "error": error_message}

    try:
        from shared_models import CapiEvent, db
    except ImportError:
        return {"success": status == "sent", "error": error_message}

    try:
        event_id = ext_event_id or str(uuid.uuid4())
        safe_user_data = {
            "em": user_data_json.get("email"),
            "ph": user_data_json.get("phone"),
            "client_ip_address": user_data_json.get("client_ip_address") or user_data_json.get("ip"),
            "client_user_agent": user_data_json.get("client_user_agent") or user_data_json.get("user_agent"),
        }
        ws_id = int(workspace_id) if str(workspace_id).isdigit() else workspace_id
        record = CapiEvent(
            workspace_id=ws_id,
            event_id=event_id,
            event_name=event_name,
            pixel_id=pixel_id,
            event_time=datetime.utcnow(),
            event_source_url=event_source_url,
            action_source=action_source or "business_messaging",
            user_data_json=safe_user_data,
            custom_data_json=custom_data_json,
            status=status,
            api_response=api_response,
            error_message=error_message,
            sent_at=datetime.utcnow() if status == "sent" else None,
        )
        db.session.add(record)
        db.session.commit()
        result = {"success": status == "sent", "event_id": event_id, "id": record.id}
        if error_message:
            result["error"] = error_message
        return result
    except Exception as exc:
        logger.error("Failed to log CapiEvent: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(exc)}


def send_capi_event(
    event_name: str,
    user_data_dict: dict,
    custom_data_dict: dict | None = None,
    workspace_id=None,
    action_source: str = "business_messaging",
    event_source_url: str | None = None,
    test_event_code: str | None = None,
) -> dict:
    pixel_id = None
    access_token = None

    if workspace_id and current_app:
        try:
            from shared_models import CapiAccount

            ws_key = int(workspace_id) if str(workspace_id).isdigit() else workspace_id
            account = CapiAccount.query.filter_by(workspace_id=ws_key).first()
            if account and account.pixel_id and account.system_user_token:
                pixel_id = account.pixel_id
                access_token = account.system_user_token
            # Fallback: a SocialAccount (Meta) token if the CapiAccount is missing
            # the token but has the pixel, so a connected ad account still works.
            if pixel_id and not access_token:
                try:
                    from shared_models import SocialAccount
                    sa = (SocialAccount.query
                          .filter_by(workspace_id=str(workspace_id))
                          .order_by(SocialAccount.created_at.desc()).first())
                    if sa and getattr(sa, "access_token", None):
                        access_token = sa.access_token
                        logger.info("CAPI: using SocialAccount token fallback workspace=%s", workspace_id)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("CAPI account lookup failed workspace=%s: %s", workspace_id, exc)

    if not pixel_id or not access_token:
        # NOT an error — the workspace simply hasn't connected CAPI. Record as
        # 'skipped' (distinct from a real send failure) so dashboards/alerts don't
        # treat unconfigured workspaces as broken.
        logger.info(
            "CAPI event '%s' skipped for workspace=%s (no pixel/token configured)",
            event_name,
            workspace_id,
        )
        return _log_to_db(
            workspace_id=workspace_id,
            event_name=event_name,
            action_source=action_source,
            event_source_url=event_source_url,
            user_data_json=user_data_dict,
            custom_data_json=custom_data_dict,
            status="skipped",
            error_message="CAPI not configured (no pixel/token)",
        )

    emails = [_hash_data(user_data_dict.get("email"))] if user_data_dict.get("email") else []
    _ph = _normalize_phone(user_data_dict.get("phone"))
    phones = [_hash_data(_ph)] if _ph else []

    first_name = user_data_dict.get("first_name")
    last_name = user_data_dict.get("last_name")
    full_name = user_data_dict.get("name")
    if full_name and not first_name and not last_name:
        parts = full_name.split(" ", 1)
        first_name = parts[0]
        if len(parts) > 1:
            last_name = parts[1]

    user_data: dict = {}
    if emails:
        user_data["em"] = emails
    if phones:
        user_data["ph"] = phones
    if first_name:
        user_data["fn"] = [_hash_data(first_name)]
    if last_name:
        user_data["ln"] = [_hash_data(last_name)]

    for src_key, dst_key in (
        ("client_ip_address", "client_ip_address"),
        ("ip", "client_ip_address"),
        ("client_user_agent", "client_user_agent"),
        ("user_agent", "client_user_agent"),
        ("fbc", "fbc"),
        ("fbp", "fbp"),
        ("lead_id", "lead_id"),
        ("ctwa_clid", "ctwa_clid"),
    ):
        value = user_data_dict.get(src_key)
        if value and dst_key not in user_data:
            user_data[dst_key] = str(value)

    if custom_data_dict is None:
        custom_data_dict = {}
    if action_source != "website":
        custom_data_dict.setdefault("event_source", "crm")
        custom_data_dict.setdefault("lead_event_source", "Sociovia WhatsApp")

    event_id = str(uuid.uuid4())
    payload = {
        "data": [
            {
                "event_name": event_name,
                "event_time": int(time.time()),
                "action_source": action_source,
                "event_id": event_id,
                "user_data": user_data,
                "custom_data": custom_data_dict,
            }
        ]
    }
    if event_source_url:
        payload["data"][0]["event_source_url"] = event_source_url

    tec = test_event_code or os.environ.get("CAPI_TEST_EVENT_CODE")
    if tec:
        payload["test_event_code"] = str(tec)

    api_version = os.environ.get("FB_API_VERSION", "v21.0")
    url = f"https://graph.facebook.com/{api_version}/{pixel_id}/events"

    try:
        response = _post_with_retry(url, {"access_token": access_token}, payload, timeout=10)
        body = response.json()
        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            # Log the structured Meta error + fbtrace_id so rejections are debuggable.
            logger.error(
                "CAPI %s rejected workspace=%s code=%s subcode=%s fbtrace=%s msg=%s",
                event_name, workspace_id, error.get("code"), error.get("error_subcode"),
                error.get("fbtrace_id"), error.get("message"),
            )
            raise RuntimeError(error.get("message", "Facebook Graph API error"))

        return _log_to_db(
            workspace_id=workspace_id,
            event_name=event_name,
            action_source=action_source,
            event_source_url=event_source_url,
            user_data_json=user_data_dict,
            custom_data_json=custom_data_dict,
            status="sent",
            pixel_id=pixel_id,
            api_response=body,
            ext_event_id=event_id,
        )
    except Exception as exc:
        logger.error("Failed to send CAPI event %s: %s", event_name, exc)
        return _log_to_db(
            workspace_id=workspace_id,
            event_name=event_name,
            action_source=action_source,
            event_source_url=event_source_url,
            user_data_json=user_data_dict,
            custom_data_json=custom_data_dict,
            status="failed",
            pixel_id=pixel_id,
            error_message=str(exc) if "re-authorization" in str(exc) else traceback.format_exc(),
        )
