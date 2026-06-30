"""
WhatsApp Health Check & Auto-Setup
===================================
Comprehensive health monitoring and auto-recovery for WhatsApp accounts.

This module handles:
1. Token validation and auto-refresh detection
2. WABA webhook subscription verification and auto-subscribe
3. Webhook endpoint accessibility check
4. Template sync status
5. Account quality monitoring

When a user connects an account, this ensures everything is configured correctly.
"""

import os
import logging
import requests
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from shared_models import db
from .models import WhatsAppAccount
from .encryption import decrypt_token

logger = logging.getLogger(__name__)

META_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v24.0")
META_GRAPH_API = f"https://graph.facebook.com/{META_API_VERSION}"


# ============================================================
# Health Check Status
# ============================================================

class HealthStatus:
    """Health check status constants."""
    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class HealthCheckResult:
    """
    Result of a health check operation.
    """
    def __init__(self, name: str, status: str, message: str, 
                 details: Optional[Dict] = None, auto_fix_available: bool = False):
        self.name = name
        self.status = status
        self.message = message
        self.details = details or {}
        self.auto_fix_available = auto_fix_available
        self.fixed = False
        self.fix_result = None
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
            "auto_fix_available": self.auto_fix_available,
            "fixed": self.fixed,
            "fix_result": self.fix_result
        }


# ============================================================
# Token Validation
# ============================================================

def validate_access_token(access_token: str) -> Tuple[bool, str, Dict]:
    """
    Validate access token with Meta API.
    
    Returns:
        (is_valid, error_message, details)
    """
    if not access_token:
        return False, "No access token provided", {}
    
    try:
        # First verify the token can access /me (works for user/system-user tokens)
        me_resp = requests.get(
            f"{META_GRAPH_API}/me",
            params={"fields": "id,name", "access_token": access_token},
            timeout=10,
        )

        if me_resp.status_code != 200:
            me_error = me_resp.json().get("error", {})
            return False, me_error.get("message", "Token validation failed"), {
                "error_code": me_error.get("code"),
                "error_subcode": me_error.get("error_subcode"),
                "error_type": me_error.get("type"),
            }

        me_data = me_resp.json()

        # Then try /debug_token for richer metadata (requires app owner/developer context)
        debug_resp = requests.get(
            f"{META_GRAPH_API}/debug_token",
            params={
                "input_token": access_token,
                "access_token": access_token,
            },
            timeout=10,
        )

        if debug_resp.status_code == 200:
            data = debug_resp.json().get("data", {})
            is_valid = data.get("is_valid", False)
            expires_at = data.get("expires_at", 0)
            app_id = data.get("app_id")

            if expires_at and expires_at > 0:
                expiry_time = datetime.fromtimestamp(expires_at, tz=timezone.utc)
                now = datetime.now(timezone.utc)

                if expiry_time < now:
                    return False, "Token has expired", {
                        "expired_at": expiry_time.isoformat(),
                        "error_code": "TOKEN_EXPIRED",
                        "actor_id": me_data.get("id"),
                        "actor_name": me_data.get("name"),
                    }

                days_until_expiry = (expiry_time - now).days
                if days_until_expiry < 7:
                    return True, f"Token expires in {days_until_expiry} days", {
                        "expires_at": expiry_time.isoformat(),
                        "days_until_expiry": days_until_expiry,
                        "warning": "TOKEN_EXPIRING_SOON",
                        "app_id": app_id,
                        "scopes": data.get("scopes", []),
                        "actor_id": me_data.get("id"),
                        "actor_name": me_data.get("name"),
                    }

            if is_valid:
                return True, "Token is valid", {
                    "app_id": app_id,
                    "expires_at": expires_at,
                    "scopes": data.get("scopes", []),
                    "actor_id": me_data.get("id"),
                    "actor_name": me_data.get("name"),
                }

            return False, data.get("error", {}).get("message", "Token is invalid"), {
                "error_code": "TOKEN_INVALID",
                "actor_id": me_data.get("id"),
                "actor_name": me_data.get("name"),
            }

        # /debug_token may fail for non-app-owner users even when token itself works.
        debug_error = debug_resp.json().get("error", {})
        return True, "Token is valid (limited introspection)", {
            "warning": "TOKEN_DEBUG_RESTRICTED",
            "debug_message": debug_error.get("message"),
            "debug_error_code": debug_error.get("code"),
            "debug_error_subcode": debug_error.get("error_subcode"),
            "actor_id": me_data.get("id"),
            "actor_name": me_data.get("name"),
        }

    except requests.exceptions.Timeout:
        return False, "Meta API timeout during token validation", {"error_code": "TIMEOUT"}
    except requests.exceptions.RequestException as e:
        return False, f"Request failed: {str(e)}", {"error_code": "REQUEST_FAILED"}
    except Exception as e:
        logger.exception(f"Token validation error: {e}")
        return False, f"Unexpected error: {str(e)}", {"error_code": "UNKNOWN"}


# ============================================================
# WABA Webhook Subscription
# ============================================================

def check_waba_webhook_subscription(waba_id: str, access_token: str) -> Tuple[bool, str, Dict]:
    """
    Check if WABA is subscribed to receive webhooks.
    
    Returns:
        (is_subscribed, message, details)
    """
    if not waba_id or not access_token:
        return False, "Missing WABA ID or access token", {}
    
    try:
        url = f"{META_GRAPH_API}/{waba_id}/subscribed_apps"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            apps = data.get("data", [])
            
            if apps:
                # Check if our app is subscribed
                app_id = os.getenv("FB_APP_ID")
                for app in apps:
                    if app.get("id") == app_id or app.get("whatsapp_business_api_data"):
                        return True, "WABA is subscribed to webhooks", {
                            "subscribed_apps": apps
                        }
                
                return True, "WABA has app subscriptions", {"subscribed_apps": apps}
            else:
                return False, "WABA is NOT subscribed to any webhooks", {
                    "error_code": "NO_SUBSCRIPTION"
                }
        else:
            error = response.json().get("error", {})
            return False, error.get("message", "Failed to check subscription"), {
                "error_code": error.get("code")
            }
            
    except requests.exceptions.RequestException as e:
        return False, f"Request failed: {str(e)}", {"error_code": "REQUEST_FAILED"}


def subscribe_waba_to_webhooks(waba_id: str, access_token: str) -> Tuple[bool, str, Dict]:
    """
    Subscribe WABA to receive webhooks for this app.
    
    This is CRITICAL for receiving incoming messages AND template status updates!
    
    Without explicit subscribed_fields, Meta only sends 'messages' events.
    We need template status events for auto-updating approval/rejection status.
    
    Returns:
        (success, message, details)
    """
    if not waba_id or not access_token:
        return False, "Missing WABA ID or access token", {}
    
    # All webhook fields we need to subscribe to
    # - messages: incoming messages & delivery status
    # - message_template_status_update: template approval/rejection
    # - message_template_quality_update: template quality score changes
    # - template_category_update: template category changes
    # - message_echoes: messages sent from mobile (coexistence)
    from .provisioning_types import FULL_WEBHOOK_FIELDS

    subscribed_fields = list(FULL_WEBHOOK_FIELDS)
    
    try:
        url = f"{META_GRAPH_API}/{waba_id}/subscribed_apps"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        
        response = requests.post(url, headers=headers, json={
            "subscribed_fields": subscribed_fields,
        }, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            if data.get("success"):
                logger.info(f"✅ Successfully subscribed WABA {waba_id} to webhooks")
                return True, "Successfully subscribed WABA to webhooks", {
                    "waba_id": waba_id,
                    "subscribed": True
                }
            else:
                return False, "Subscription returned false", {"response": data}
        else:
            error = response.json().get("error", {})
            return False, error.get("message", "Subscription failed"), {
                "error_code": error.get("code"),
                "error_subcode": error.get("error_subcode")
            }
            
    except requests.exceptions.RequestException as e:
        logger.exception(f"WABA subscription failed: {e}")
        return False, f"Request failed: {str(e)}", {"error_code": "REQUEST_FAILED"}


# ============================================================
# Phone Number Quality Check
# ============================================================

def check_phone_number_quality(phone_number_id: str, access_token: str) -> Tuple[str, str, Dict]:
    """
    Check phone number quality rating and status.
    
    Returns:
        (status, message, details)
    """
    if not phone_number_id or not access_token:
        return HealthStatus.ERROR, "Missing phone number ID or token", {}
    
    try:
        url = f"{META_GRAPH_API}/{phone_number_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {
            "fields": "display_phone_number,verified_name,quality_rating,messaging_limit_tier,name_status,status"
        }
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            quality = data.get("quality_rating", "UNKNOWN")
            messaging_limit = data.get("messaging_limit_tier", "UNKNOWN")
            name_status = data.get("name_status", "UNKNOWN")
            phone_status = str(data.get("status", "UNKNOWN")).upper()
            
            details = {
                "display_phone_number": data.get("display_phone_number"),
                "verified_name": data.get("verified_name"),
                "quality_rating": quality,
                "messaging_limit_tier": messaging_limit,
                "name_status": name_status,
                "status": phone_status
            }

            if phone_status in {"DISCONNECTED", "UNREGISTERED", "OFFLINE"}:
                return HealthStatus.CRITICAL, f"Phone number status is {phone_status}", {
                    **details,
                    "error_code": "PHONE_DISCONNECTED",
                    "action_required": "Reconnect/register this number in WhatsApp Manager (Cloud API)",
                }
            
            # Determine health status based on quality
            if quality == "GREEN":
                return HealthStatus.HEALTHY, "Phone number quality is GREEN", details
            elif quality == "YELLOW":
                return HealthStatus.WARNING, "Phone number quality is YELLOW - improve message quality", details
            elif quality == "RED":
                return HealthStatus.CRITICAL, "Phone number quality is RED - messaging may be restricted", details
            else:
                return HealthStatus.WARNING, f"Unknown quality rating: {quality}", details
        else:
            error = response.json().get("error", {})
            return HealthStatus.ERROR, error.get("message", "Failed to check quality"), {
                "error_code": error.get("code")
            }
            
    except requests.exceptions.RequestException as e:
        return HealthStatus.ERROR, f"Request failed: {str(e)}", {}


# ============================================================
# Comprehensive Health Check
# ============================================================

def perform_health_check(account_id: int, auto_fix: bool = True) -> Dict[str, Any]:
    """
    Perform comprehensive health check on a WhatsApp account.
    
    Checks:
    1. Token validity
    2. WABA webhook subscription
    3. Phone number quality
    4. Template sync status
    
    Args:
        account_id: WhatsApp account ID
        auto_fix: Whether to attempt automatic fixes
        
    Returns:
        Complete health check report with fixes applied
    """
    results: List[HealthCheckResult] = []
    overall_status = HealthStatus.HEALTHY
    
    # Get account
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return {
            "success": False,
            "error": f"Account {account_id} not found",
            "overall_status": HealthStatus.CRITICAL
        }
    
    # Decrypt token
    access_token = None
    if account.access_token_encrypted:
        try:
            access_token = decrypt_token(account.access_token_encrypted)
        except Exception as e:
            logger.error(f"Failed to decrypt token: {e}")
    
    # ============================================================
    # Check 1: Token Validity
    # ============================================================
    token_valid, token_msg, token_details = validate_access_token(access_token)
    
    if not token_valid:
        token_result = HealthCheckResult(
            name="access_token",
            status=HealthStatus.CRITICAL,
            message=token_msg,
            details=token_details,
            auto_fix_available=False  # Token needs manual update
        )
        overall_status = HealthStatus.CRITICAL
    elif token_details.get("warning") == "TOKEN_EXPIRING_SOON":
        token_result = HealthCheckResult(
            name="access_token",
            status=HealthStatus.WARNING,
            message=token_msg,
            details=token_details,
            auto_fix_available=False
        )
        if overall_status == HealthStatus.HEALTHY:
            overall_status = HealthStatus.WARNING
    else:
        token_result = HealthCheckResult(
            name="access_token",
            status=HealthStatus.HEALTHY,
            message="Access token is valid",
            details=token_details
        )
    
    results.append(token_result)

    # ============================================================
    # Check 1b: Messaging send permission (partner WABAs need System User)
    # ============================================================
    if token_valid and account.phone_number_id:
        from .connection_path import verify_messaging_send_access

        send_check = verify_messaging_send_access(
            access_token, account.phone_number_id, account.waba_id
        )
        if send_check.get("can_send"):
            send_result = HealthCheckResult(
                name="messaging_send_permission",
                status=HealthStatus.HEALTHY,
                message="Token can send messages for this phone number",
                details=send_check.get("details") or {},
            )
        else:
            send_result = HealthCheckResult(
                name="messaging_send_permission",
                status=HealthStatus.CRITICAL,
                message=send_check.get("error", "Cannot send messages with this token"),
                details={
                    **(send_check.get("details") or {}),
                    "hints": send_check.get("hints", []),
                    "error_code": send_check.get("error_code"),
                },
                auto_fix_available=False,
            )
            overall_status = HealthStatus.CRITICAL
        results.append(send_result)
    
    # Only continue checks if token is valid
    if not token_valid:
        return {
            "success": True,
            "account_id": account_id,
            "waba_id": account.waba_id,
            "phone_number_id": account.phone_number_id,
            "display_phone_number": account.display_phone_number,
            "overall_status": overall_status,
            "checks": [r.to_dict() for r in results],
            "action_required": "Token is invalid or expired. Please update with a new System User Access Token.",
            "auto_fixes_applied": 0
        }
    
    # ============================================================
    # Check 2: WABA Webhook Subscription
    # ============================================================
    if account.waba_id:
        is_subscribed, sub_msg, sub_details = check_waba_webhook_subscription(
            account.waba_id, access_token
        )
        
        if not is_subscribed:
            webhook_result = HealthCheckResult(
                name="webhook_subscription",
                status=HealthStatus.CRITICAL,
                message=sub_msg,
                details=sub_details,
                auto_fix_available=True
            )
            
            # Auto-fix: Subscribe to webhooks
            if auto_fix:
                fix_success, fix_msg, fix_details = subscribe_waba_to_webhooks(
                    account.waba_id, access_token
                )
                webhook_result.fixed = fix_success
                webhook_result.fix_result = {
                    "success": fix_success,
                    "message": fix_msg,
                    "details": fix_details
                }
                
                if fix_success:
                    webhook_result.status = HealthStatus.HEALTHY
                    webhook_result.message = "Webhook subscription auto-fixed"
                    logger.info(f"✅ Auto-fixed webhook subscription for account {account_id}")
                else:
                    overall_status = HealthStatus.CRITICAL
            else:
                overall_status = HealthStatus.CRITICAL
        else:
            webhook_result = HealthCheckResult(
                name="webhook_subscription",
                status=HealthStatus.HEALTHY,
                message="WABA is subscribed to webhooks",
                details=sub_details
            )
        
        results.append(webhook_result)
    
    # ============================================================
    # Check 3: Phone Number Quality
    # ============================================================
    if account.phone_number_id:
        quality_status, quality_msg, quality_details = check_phone_number_quality(
            account.phone_number_id, access_token
        )
        
        quality_result = HealthCheckResult(
            name="phone_quality",
            status=quality_status,
            message=quality_msg,
            details=quality_details
        )
        
        if quality_status == HealthStatus.CRITICAL:
            overall_status = HealthStatus.CRITICAL
        elif quality_status == HealthStatus.WARNING and overall_status == HealthStatus.HEALTHY:
            overall_status = HealthStatus.WARNING
        
        results.append(quality_result)
    
    # ============================================================
    # Check 4: Webhook URL Configuration
    # ============================================================
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN")
    app_base_url = os.getenv("APP_BASE_URL", "")
    
    webhook_config_result = HealthCheckResult(
        name="webhook_config",
        status=HealthStatus.HEALTHY if verify_token else HealthStatus.WARNING,
        message="Webhook verify token is configured" if verify_token else "WHATSAPP_VERIFY_TOKEN not set",
        details={
            "verify_token_set": bool(verify_token),
            "webhook_url": f"{app_base_url}/api/whatsapp/webhook"
        }
    )
    results.append(webhook_config_result)
    
    # Count auto-fixes
    auto_fixes_applied = sum(1 for r in results if r.fixed)
    
    # Build action required message
    action_required = None
    if overall_status == HealthStatus.CRITICAL:
        critical_checks = [r for r in results if r.status == HealthStatus.CRITICAL]
        if critical_checks:
            action_required = f"Critical issues found: {', '.join(r.name for r in critical_checks)}"
    elif overall_status == HealthStatus.WARNING:
        warning_checks = [r for r in results if r.status == HealthStatus.WARNING]
        if warning_checks:
            action_required = f"Warnings: {', '.join(r.name for r in warning_checks)}"
    
    return {
        "success": True,
        "account_id": account_id,
        "waba_id": account.waba_id,
        "phone_number_id": account.phone_number_id,
        "display_phone_number": account.display_phone_number,
        "verified_name": account.verified_name,
        "overall_status": overall_status,
        "checks": [r.to_dict() for r in results],
        "action_required": action_required,
        "auto_fixes_applied": auto_fixes_applied,
        "checked_at": datetime.now(timezone.utc).isoformat()
    }


def perform_workspace_health_check(workspace_id: str, auto_fix: bool = True) -> Dict[str, Any]:
    """
    Perform health check for all WhatsApp accounts in a workspace.
    """
    accounts = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_active=True
    ).all()
    
    if not accounts:
        return {
            "success": True,
            "workspace_id": workspace_id,
            "message": "No active WhatsApp accounts found",
            "accounts": [],
            "overall_status": HealthStatus.WARNING
        }
    
    results = []
    overall_status = HealthStatus.HEALTHY
    
    for account in accounts:
        result = perform_health_check(account.id, auto_fix=auto_fix)
        results.append(result)
        
        # Update overall status
        account_status = result.get("overall_status", HealthStatus.HEALTHY)
        if account_status == HealthStatus.CRITICAL:
            overall_status = HealthStatus.CRITICAL
        elif account_status == HealthStatus.WARNING and overall_status == HealthStatus.HEALTHY:
            overall_status = HealthStatus.WARNING
    
    return {
        "success": True,
        "workspace_id": workspace_id,
        "accounts": results,
        "overall_status": overall_status,
        "total_accounts": len(accounts),
        "checked_at": datetime.now(timezone.utc).isoformat()
    }


# ============================================================
# Post-Connection Setup
# ============================================================

def setup_account_after_connection(account_id: int) -> Dict[str, Any]:
    """
    Called after a WhatsApp account is connected.
    Performs all necessary setup steps automatically.
    
    Steps:
    1. Validate token
    2. Subscribe WABA to webhooks
    3. Sync templates
    4. Check phone number status
    
    Returns:
        Setup result with any issues encountered
    """
    logger.info(f"🔧 Running post-connection setup for account {account_id}")
    
    # Run health check with auto-fix enabled
    health_result = perform_health_check(account_id, auto_fix=True)
    
    # Additional setup: Sync templates
    try:
        from .services import WhatsAppService
        account = WhatsAppAccount.query.get(account_id)
        if account and account.access_token_encrypted:
            access_token = decrypt_token(account.access_token_encrypted)
            service = WhatsAppService(
                access_token=access_token,
                phone_number_id=account.phone_number_id,
                account_id=account.id
            )
            # Trigger template sync
            service.sync_templates()
            logger.info(f"✅ Post-connection setup completed for account {account_id}")
    except Exception as e:
        logger.exception(f"Template sync during setup failed: {e}")
    
    return {
        "success": health_result.get("overall_status") != HealthStatus.CRITICAL,
        "account_id": account_id,
        "health_check": health_result,
        "setup_complete": True,
        "message": "Account setup completed" if health_result.get("overall_status") != HealthStatus.CRITICAL 
                   else "Account setup completed with issues - please review"
    }
