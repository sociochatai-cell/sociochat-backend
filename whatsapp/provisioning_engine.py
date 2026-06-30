"""
WhatsApp Provisioning Engine
============================
THE single canonical pipeline for post-connection account setup.
Every onboarding flow MUST call provision_account() after saving the account.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

import requests

from shared_models import db
from .provisioning_types import (
    CAP_BUSINESS_PROFILE, CAP_CAN_SEND, CAP_DISPLAY_NAME_APPROVED,
    CAP_MESSAGING_TIER, CAP_NOT_RESTRICTED, CAP_PHONE_REGISTERED,
    CAP_QUALITY_HEALTHY, CAP_TEMPLATES_SYNCED, CAP_TOKEN_APP_OWNERSHIP,
    CAP_TOKEN_SCOPES, CAP_TOKEN_VALID, CAP_WARMUP_ACKNOWLEDGED,
    CAP_WEBHOOK_FIELDS_COMPLETE, CAP_WEBHOOK_SUBSCRIBED,
    ADVISORY_SCOPES, REQUIRED_SCOPES, FULL_WEBHOOK_FIELDS,
    CapabilityStatus, ProvisioningCheck, ProvisioningResult,
    PROV_HEALTH_VERIFIED, PROV_OAUTH_RECEIVED, PROV_PHONE_REGISTERED,
    PROV_READY, PROV_TEMPLATES_SYNCED, PROV_TOKEN_EXCHANGED,
    PROV_WARMUP_STARTED, PROV_WEBHOOK_SUBSCRIBED,
    OP_CONNECTED, compute_readiness_score, determine_operational_lifecycle,
)

if TYPE_CHECKING:
    from .models import WhatsAppAccount

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v24.0")
GRAPH_ROOT = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
META_APP_ID = os.getenv("META_APP_ID") or os.getenv("FB_APP_ID", "")
META_APP_SECRET = os.getenv("META_APP_SECRET") or os.getenv("FB_APP_SECRET", "")

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 2


def _utcnow():
    return datetime.now(timezone.utc)


def _retry_request(method, url, **kwargs):
    """Execute HTTP request with exponential backoff retry."""
    for attempt in range(MAX_RETRIES):
        try:
            kwargs.setdefault("timeout", 15)
            resp = getattr(requests, method)(url, **kwargs)
            return resp
        except requests.exceptions.RequestException:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_BASE_SECONDS * (2 ** attempt))


# ============================================================
# Pipeline Steps
# ============================================================

def _step_validate_token(access_token: str) -> tuple:
    """Step 1-3: Deep token validation via /debug_token."""
    cap_valid = CapabilityStatus(enabled=False)
    cap_scopes = CapabilityStatus(enabled=False)
    cap_app = CapabilityStatus(enabled=False, auto_fixable=False)
    token_debug = {}
    checks = []

    if not access_token:
        cap_valid.reason = "No access token"
        checks.append(ProvisioningCheck("token_validation", "critical", "No access token provided"))
        return cap_valid, cap_scopes, cap_app, token_debug, checks

    # Validate via /me first
    try:
        me_resp = _retry_request("get", f"{GRAPH_ROOT}/me",
                                 params={"fields": "id,name", "access_token": access_token})
        if me_resp.status_code != 200:
            err = me_resp.json().get("error", {})
            cap_valid.reason = err.get("message", "Token invalid")
            checks.append(ProvisioningCheck("token_validation", "critical", cap_valid.reason))
            return cap_valid, cap_scopes, cap_app, token_debug, checks
    except Exception as e:
        cap_valid.reason = str(e)
        checks.append(ProvisioningCheck("token_validation", "critical", f"Token check failed: {e}"))
        return cap_valid, cap_scopes, cap_app, token_debug, checks

    cap_valid.enabled = True

    # Deep introspection via /debug_token with app access token
    if META_APP_ID and META_APP_SECRET:
        try:
            app_token = f"{META_APP_ID}|{META_APP_SECRET}"
            dbg_resp = _retry_request("get", f"{GRAPH_ROOT}/debug_token",
                                      params={"input_token": access_token, "access_token": app_token})
            if dbg_resp.status_code == 200:
                data = dbg_resp.json().get("data", {})
                token_debug = {
                    "is_valid": data.get("is_valid"),
                    "app_id": data.get("app_id"),
                    "user_id": data.get("user_id"),
                    "type": data.get("type"),
                    "expires_at": data.get("expires_at"),
                    "scopes": data.get("scopes", []),
                    "granular_scopes": data.get("granular_scopes", []),
                }

                # Check token validity
                if not data.get("is_valid"):
                    cap_valid.enabled = False
                    cap_valid.reason = "Token marked invalid by Meta"

                # Check expiry
                exp = data.get("expires_at", 0)
                if exp and exp > 0:
                    from datetime import datetime as dt
                    exp_dt = dt.fromtimestamp(exp, tz=timezone.utc)
                    if exp_dt < _utcnow():
                        cap_valid.enabled = False
                        cap_valid.reason = "Token expired"

                # Check scopes
                granted = set(data.get("scopes", []))
                missing = REQUIRED_SCOPES - granted
                if not missing:
                    cap_scopes.enabled = True
                else:
                    cap_scopes.reason = f"Missing scopes: {', '.join(missing)}"
                    checks.append(ProvisioningCheck(
                        "token_scopes", "critical",
                        f"Missing required scopes: {', '.join(missing)}"
                    ))

                # Advisory scope check
                missing_advisory = ADVISORY_SCOPES - granted
                if missing_advisory:
                    checks.append(ProvisioningCheck(
                        "advisory_scopes", "warning",
                        f"Optional scopes not granted: {', '.join(missing_advisory)}. "
                        "Some advanced business operations may fail.",
                    ))

                # App ownership
                debug_app_id = str(data.get("app_id") or "")
                if debug_app_id == META_APP_ID:
                    cap_app.enabled = True
                else:
                    cap_app.reason = f"Token belongs to app {debug_app_id}, expected {META_APP_ID}"
                    checks.append(ProvisioningCheck(
                        "app_ownership", "warning",
                        cap_app.reason
                    ))

                # Tech Provider: prefer business integration (SYSTEM) token from Embedded Signup
                token_type = data.get("type")
                token_debug["token_type"] = token_type
                if token_type == "SYSTEM":
                    checks.append(ProvisioningCheck(
                        "token_type", "healthy",
                        "Business integration token (SYSTEM)",
                    ))
                elif token_type == "USER":
                    checks.append(ProvisioningCheck(
                        "token_type", "warning",
                        "Token is USER type; Tech Provider flows expect a business integration "
                        "token from Embedded Signup. Webhook subscribe may fail with (#200).",
                    ))
        except Exception as e:
            logger.warning("debug_token failed: %s", e)
            # /debug_token failure is non-fatal; we already confirmed /me works
            cap_scopes.enabled = True  # Assume scopes OK if debug fails
            cap_scopes.reason = "Could not verify scopes (debug_token unavailable)"
            cap_app.reason = "Could not verify app ownership"
    else:
        cap_scopes.enabled = True
        cap_scopes.reason = "Scope verification skipped (no app credentials)"
        cap_app.reason = "App ownership check skipped"

    status = "healthy" if cap_valid.enabled else "critical"
    checks.append(ProvisioningCheck("token_validation", status,
                                    "Token is valid" if cap_valid.enabled else (cap_valid.reason or "Invalid")))
    return cap_valid, cap_scopes, cap_app, token_debug, checks


def _step_register_phone(phone_number_id: str, access_token: str) -> tuple:
    """Step 4: Register phone for Cloud API."""
    cap = CapabilityStatus(enabled=False, auto_fixable=True)
    checks = []

    if not phone_number_id:
        cap.reason = "No phone_number_id"
        return cap, checks

    try:
        resp = _retry_request("post", f"{GRAPH_ROOT}/{phone_number_id}/register",
                              json={"messaging_product": "whatsapp", "pin": "123456"},
                              headers={"Authorization": f"Bearer {access_token}",
                                       "Content-Type": "application/json"})
        result = resp.json()

        if resp.status_code == 200 and result.get("success"):
            cap.enabled = True
            cap.fixed = True
            checks.append(ProvisioningCheck("phone_registration", "healthy", "Phone registered for Cloud API"))
        elif result.get("error", {}).get("code") == 33:
            # Already registered — that's fine
            cap.enabled = True
            checks.append(ProvisioningCheck("phone_registration", "healthy", "Phone already registered"))
        else:
            err_msg = result.get("error", {}).get("message", "Registration failed")
            cap.reason = err_msg
            checks.append(ProvisioningCheck("phone_registration", "warning", f"Registration: {err_msg}"))
            # May already be registered even on error
            cap.enabled = True
    except Exception as e:
        cap.reason = str(e)
        cap.enabled = True  # Assume registered, don't block
        checks.append(ProvisioningCheck("phone_registration", "warning", f"Could not verify registration: {e}"))

    return cap, checks


def _step_subscribe_webhooks(waba_id: str, access_token: str) -> tuple:
    """Step 5: Subscribe WABA to webhooks with field integrity check."""
    cap_sub = CapabilityStatus(enabled=False, auto_fixable=True)
    cap_fields = CapabilityStatus(enabled=False)
    checks = []

    if not waba_id:
        cap_sub.reason = "No waba_id"
        return cap_sub, cap_fields, checks

    # First check current subscription
    try:
        check_resp = _retry_request("get", f"{GRAPH_ROOT}/{waba_id}/subscribed_apps",
                                    headers={"Authorization": f"Bearer {access_token}"})
        if check_resp.status_code == 200:
            apps = check_resp.json().get("data", [])
            our_app = None
            for app in apps:
                app_id = str(app.get("id") or "")
                nested = app.get("whatsapp_business_api_data") or {}
                nested_id = str(nested.get("id") or "")
                if META_APP_ID and (app_id == META_APP_ID or nested_id == META_APP_ID):
                    our_app = app
                    break
                if not META_APP_ID and nested_id:
                    our_app = app
                    break
            if our_app:
                cap_sub.enabled = True
            elif apps:
                # Some app subscribed but not ours
                cap_sub.enabled = False
                cap_sub.reason = "Our app is not subscribed"
    except Exception as e:
        logger.warning("Webhook check failed: %s", e)

    # Subscribe (always re-subscribe to ensure fields are correct)
    try:
        sub_resp = _retry_request(
            "post", f"{GRAPH_ROOT}/{waba_id}/subscribed_apps",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"subscribed_fields": FULL_WEBHOOK_FIELDS},
        )
        result = sub_resp.json()
        if sub_resp.status_code == 200 and result.get("success"):
            cap_sub.enabled = True
            cap_sub.fixed = not cap_sub.enabled  # Only mark fixed if it wasn't already
            cap_fields.enabled = True
            checks.append(ProvisioningCheck("webhook_subscription", "healthy",
                                            "WABA subscribed to webhooks with all required fields",
                                            fixed=True))
        else:
            err = result.get("error", {}).get("message", "Subscription failed")
            cap_sub.reason = err
            checks.append(ProvisioningCheck("webhook_subscription", "critical",
                                            f"Webhook subscription failed: {err}",
                                            auto_fix_available=True))
    except Exception as e:
        cap_sub.reason = str(e)
        checks.append(ProvisioningCheck("webhook_subscription", "critical",
                                        f"Webhook subscription error: {e}",
                                        auto_fix_available=True))

    return cap_sub, cap_fields, checks


def _step_check_phone_quality(phone_number_id: str, access_token: str) -> tuple:
    """Step 7-8: Check phone quality, display name, messaging tier."""
    cap_quality = CapabilityStatus(enabled=False)
    cap_name = CapabilityStatus(enabled=False)
    cap_tier = CapabilityStatus(enabled=False)
    checks = []

    if not phone_number_id:
        return cap_quality, cap_name, cap_tier, checks

    try:
        resp = _retry_request(
            "get", f"{GRAPH_ROOT}/{phone_number_id}",
            params={"fields": "display_phone_number,verified_name,quality_rating,"
                    "messaging_limit_tier,name_status,status",
                    "access_token": access_token},
        )
        if resp.status_code == 200:
            data = resp.json()
            quality = data.get("quality_rating", "UNKNOWN")
            name_status = data.get("name_status", "UNKNOWN")
            tier = data.get("messaging_limit_tier", "UNKNOWN")
            phone_status = str(data.get("status", "UNKNOWN")).upper()

            # Quality
            if quality in ("GREEN", "YELLOW"):
                cap_quality.enabled = True
            else:
                cap_quality.reason = f"Quality: {quality}"
            checks.append(ProvisioningCheck("phone_quality",
                                            "healthy" if quality == "GREEN" else "warning",
                                            f"Quality rating: {quality}"))

            # Display name
            if name_status == "APPROVED":
                cap_name.enabled = True
            else:
                cap_name.reason = f"Name status: {name_status}"
            checks.append(ProvisioningCheck("display_name",
                                            "healthy" if cap_name.enabled else "warning",
                                            f"Display name: {name_status}"))

            # Messaging tier
            if tier and tier not in ("UNKNOWN", "TIER_NOT_SET"):
                cap_tier.enabled = True
            else:
                cap_tier.reason = f"Tier: {tier}"
            checks.append(ProvisioningCheck("messaging_tier",
                                            "healthy" if cap_tier.enabled else "warning",
                                            f"Messaging tier: {tier}"))

            # Phone disconnected?
            if phone_status in ("DISCONNECTED", "UNREGISTERED", "OFFLINE"):
                cap_quality.enabled = False
                cap_quality.reason = f"Phone status: {phone_status}"
                checks.append(ProvisioningCheck("phone_status", "critical",
                                                f"Phone is {phone_status}"))
    except Exception as e:
        logger.warning("Phone quality check failed: %s", e)
        checks.append(ProvisioningCheck("phone_quality", "warning", f"Quality check failed: {e}"))

    return cap_quality, cap_name, cap_tier, checks


def _step_check_business_profile(phone_number_id: str, access_token: str) -> tuple:
    """Step 6: Validate business profile completeness."""
    cap = CapabilityStatus(enabled=False)
    checks = []

    try:
        from .meta_asset_discovery import check_business_readiness
        readiness = check_business_readiness(phone_number_id, access_token)
        if readiness.get("is_ready"):
            cap.enabled = True
            checks.append(ProvisioningCheck("business_profile", "healthy", "Business profile is complete"))
        else:
            missing = readiness.get("missing_fields", []) + readiness.get("issues", [])
            cap.reason = f"Missing: {', '.join(missing)}"
            checks.append(ProvisioningCheck("business_profile", "warning",
                                            f"Incomplete: {', '.join(missing)}"))
    except Exception as e:
        cap.reason = str(e)
        checks.append(ProvisioningCheck("business_profile", "warning", f"Profile check failed: {e}"))

    return cap, checks


def _step_verify_send_access(access_token: str, phone_number_id: str, waba_id: str) -> tuple:
    """Step: Verify messaging send permission."""
    cap = CapabilityStatus(enabled=False)
    checks = []

    try:
        from .connection_path import verify_messaging_send_access
        result = verify_messaging_send_access(access_token, phone_number_id, waba_id)
        if result.get("can_send"):
            cap.enabled = True
            checks.append(ProvisioningCheck("send_permission", "healthy", "Can send messages"))
        else:
            cap.reason = result.get("error", "Cannot send")
            checks.append(ProvisioningCheck("send_permission", "critical", cap.reason))
    except Exception as e:
        cap.reason = str(e)
        checks.append(ProvisioningCheck("send_permission", "warning", f"Send check failed: {e}"))
        cap.enabled = True  # Don't block on check failure

    return cap, checks


def _step_sync_templates(account) -> tuple:
    """Step 9: Sync templates."""
    cap = CapabilityStatus(enabled=False, auto_fixable=True)
    checks = []

    try:
        from .encryption import decrypt_token
        from .services import WhatsAppService
        token = account.get_access_token()
        if token:
            svc = WhatsAppService(access_token=token, phone_number_id=account.phone_number_id,
                                  account_id=account.id)
            svc.sync_templates()
            cap.enabled = True
            cap.fixed = True
            checks.append(ProvisioningCheck("template_sync", "healthy", "Templates synced"))
    except Exception as e:
        cap.reason = str(e)
        checks.append(ProvisioningCheck("template_sync", "warning", f"Template sync failed: {e}"))
        cap.enabled = True  # Non-critical

    return cap, checks


def _step_manage_warmup(account, is_new_account: bool, source: str) -> tuple:
    """Step 10: Manage warmup lifecycle."""
    cap = CapabilityStatus(enabled=True)
    warmup_state = {}
    checks = []

    try:
        from .warmup_account_ops import (
            start_warmup_for_new_account, ensure_mature_relink_skips_warmup,
            complete_warmup_if_due, build_public_state,
        )

        if is_new_account:
            start_warmup_for_new_account(account, source=source)
            checks.append(ProvisioningCheck("warmup", "warning",
                                            "72h warmup started — campaigns/broadcasts blocked"))
        else:
            ensure_mature_relink_skips_warmup(account)
            complete_warmup_if_due(account)

        warmup_state = build_public_state(account)
    except Exception as e:
        logger.warning("Warmup management failed: %s", e)
        cap.reason = str(e)
        checks.append(ProvisioningCheck("warmup", "warning", f"Warmup setup: {e}"))

    return cap, warmup_state, checks


# ============================================================
# Main Pipeline
# ============================================================

def provision_account(
    account: "WhatsAppAccount",
    access_token: str,
    *,
    source: str,
    is_new_account: bool,
    api_version: Optional[str] = None,
) -> ProvisioningResult:
    """
    THE canonical provisioning pipeline.
    Every onboarding flow MUST call this after saving the account to DB.
    """
    logger.info("▶ Provisioning account %s (source=%s, new=%s)", account.id, source, is_new_account)

    result = ProvisioningResult(success=False, source=source)
    caps = {}
    all_checks = []
    state = PROV_OAUTH_RECEIVED

    # Step 1-3: Token validation
    cap_tok, cap_scopes, cap_app, token_debug, tok_checks = _step_validate_token(access_token)
    caps[CAP_TOKEN_VALID] = cap_tok
    caps[CAP_TOKEN_SCOPES] = cap_scopes
    caps[CAP_TOKEN_APP_OWNERSHIP] = cap_app
    result.token_debug = token_debug
    all_checks.extend(tok_checks)
    state = PROV_TOKEN_EXCHANGED

    # Persist token health
    try:
        account.token_health = "valid" if cap_tok.enabled else "invalid"
        account.token_health_checked_at = _utcnow()
        account.token_health_detail = token_debug
        db.session.flush()
    except Exception:
        pass

    # If token is invalid, abort but still return partial result
    if not cap_tok.enabled:
        result.errors.append("Token validation failed — cannot proceed")
        result.action_required = "Re-authenticate with valid Meta credentials"
        result.checks = all_checks
        result.capability_matrix = caps
        result.readiness_score = compute_readiness_score(caps)
        return result

    # Meta Tech Provider order: subscribe webhooks (Step 2) before register phone (Step 3)
    cap_wh, cap_fields, wh_checks = _step_subscribe_webhooks(account.waba_id, access_token)
    caps[CAP_WEBHOOK_SUBSCRIBED] = cap_wh
    caps[CAP_WEBHOOK_FIELDS_COMPLETE] = cap_fields
    all_checks.extend(wh_checks)

    if getattr(account, "is_coexistence", False):
        cap_phone = CapabilityStatus(enabled=True)
        phone_checks = [
            ProvisioningCheck(
                "phone_registration",
                "healthy",
                "Skipped — coexistence number already registered on WhatsApp Business app",
            )
        ]
    else:
        cap_phone, phone_checks = _step_register_phone(account.phone_number_id, access_token)
    caps[CAP_PHONE_REGISTERED] = cap_phone
    all_checks.extend(phone_checks)
    state = PROV_PHONE_REGISTERED

    # Persist webhook status
    try:
        account.webhook_subscription_status = "subscribed" if cap_wh.enabled else "failed"
        account.webhook_health = "healthy" if cap_wh.enabled else "degraded"
        account.webhook_last_checked_at = _utcnow()
        if not cap_wh.enabled:
            account.webhook_last_error = cap_wh.reason
        db.session.flush()
    except Exception:
        pass

    state = PROV_WEBHOOK_SUBSCRIBED

    # Step 6: Business profile
    cap_biz, biz_checks = _step_check_business_profile(account.phone_number_id, access_token)
    caps[CAP_BUSINESS_PROFILE] = cap_biz
    all_checks.extend(biz_checks)

    # Step 7-8: Phone quality + display name + tier
    cap_q, cap_n, cap_t, q_checks = _step_check_phone_quality(account.phone_number_id, access_token)
    caps[CAP_QUALITY_HEALTHY] = cap_q
    caps[CAP_DISPLAY_NAME_APPROVED] = cap_n
    caps[CAP_MESSAGING_TIER] = cap_t
    all_checks.extend(q_checks)

    # Step: Verify send access
    cap_send, send_checks = _step_verify_send_access(access_token, account.phone_number_id, account.waba_id)
    caps[CAP_CAN_SEND] = cap_send
    all_checks.extend(send_checks)

    # Step: Restriction check
    restriction = getattr(account, "restriction_state", "none") or "none"
    caps[CAP_NOT_RESTRICTED] = CapabilityStatus(
        enabled=(restriction in ("none", "")),
        reason=f"Restricted: {getattr(account, 'restriction_reason', '')}" if restriction not in ("none", "") else None,
    )

    # Step 9: Template sync
    cap_tpl, tpl_checks = _step_sync_templates(account)
    caps[CAP_TEMPLATES_SYNCED] = cap_tpl
    all_checks.extend(tpl_checks)
    state = PROV_TEMPLATES_SYNCED

    # Step 10: Warmup lifecycle
    cap_warmup, warmup_state, warmup_checks = _step_manage_warmup(account, is_new_account, source)
    caps[CAP_WARMUP_ACKNOWLEDGED] = cap_warmup
    result.warmup_state = warmup_state
    all_checks.extend(warmup_checks)
    state = PROV_WARMUP_STARTED

    # Safe mode for incomplete profiles
    if not cap_biz.enabled and cap_tok.enabled:
        try:
            account.operational_mode = "advisory_safe_mode"
            account.safe_mode_reason = f"Incomplete Business Profile: {cap_biz.reason}"
            db.session.flush()
        except Exception:
            pass

    # Compute final scores
    score = compute_readiness_score(caps)
    in_warmup = warmup_state.get("in_warmup_window", False)
    lifecycle = determine_operational_lifecycle(caps, score, in_warmup, restriction)

    state = PROV_HEALTH_VERIFIED
    if score >= 70:
        state = PROV_READY

    # Build action required
    action = None
    critical_fails = [k for k, v in caps.items() if not v.enabled and k in {
        CAP_TOKEN_VALID, CAP_TOKEN_SCOPES, CAP_WEBHOOK_SUBSCRIBED, CAP_CAN_SEND}]
    if critical_fails:
        action = f"Critical issues: {', '.join(c.replace('_', ' ') for c in critical_fails)}"
    elif score < 80:
        warning_caps = [k for k, v in caps.items() if not v.enabled]
        if warning_caps:
            action = f"Improve: {', '.join(c.replace('_', ' ') for c in warning_caps[:3])}"

    # Commit all account changes
    try:
        db.session.commit()
    except Exception as e:
        logger.warning("Provisioning commit failed: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass

    result.success = len(critical_fails) == 0
    result.readiness_score = score
    result.provisioning_state = state
    result.operational_lifecycle = lifecycle
    result.capability_matrix = caps
    result.checks = all_checks
    result.action_required = action

    logger.info("✅ Provisioning complete: account=%s score=%d state=%s lifecycle=%s",
                account.id, score, state, lifecycle)

    return result
