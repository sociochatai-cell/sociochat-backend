"""
WhatsApp Connection Path Detection
===================================
BSP-level implementation for dual-path WhatsApp connection.

Detects whether a workspace should use:
- Path A: Embedded Signup (NEW numbers only)
- Path B: Manual Linking (existing WABA + token)

CRITICAL META RULES:
1. Embedded Signup = ONLY for brand-new WhatsApp numbers
2. Existing WhatsApp Cloud API numbers CANNOT be reused via Embedded Signup
3. A number can be connected to ONLY one integration at a time
4. Test numbers (like 15558016716) behave differently
"""

import os
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any

from models import db
from .models import WhatsAppAccount
from .encryption import encrypt_token, decrypt_token

logger = logging.getLogger(__name__)

META_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
META_GRAPH_API = f"https://graph.facebook.com/{META_API_VERSION}"


# ============================================================
# Connection Status Constants
# ============================================================

class ConnectionStatus:
    """
    WhatsApp connection status states.
    
    NO_ACCOUNT: No WhatsApp account exists for this workspace
    CONNECTED: Fully connected with valid token + phone_number_id
    PARTIAL: Has WABA but missing phone_number_id or incomplete setup
    RELINK_REQUIRED: Token expired or invalid, needs re-authentication
    """
    NO_ACCOUNT = "NO_ACCOUNT"
    CONNECTED = "CONNECTED"
    PARTIAL = "PARTIAL"
    RELINK_REQUIRED = "RELINK_REQUIRED"


class RecommendedPath:
    """Recommended connection path for the user."""
    EMBEDDED = "EMBEDDED"  # Use Embedded Signup (new number)
    MANUAL = "MANUAL"      # Use manual linking (existing account)


# ============================================================
# Token Validation
# ============================================================

def validate_token_with_meta(access_token: str) -> Dict[str, Any]:
    """
    Validate access token by calling Meta Graph API.
    
    Args:
        access_token: The token to validate
        
    Returns:
        Dict with:
        - valid: bool
        - user_id: str | None
        - error: str | None
        - permissions: list | None
    """
    if not access_token:
        return {"valid": False, "error": "No token provided"}
    
    try:
        # Call /me to validate token
        url = f"{META_GRAPH_API}/me"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"fields": "id,name"}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            return {
                "valid": True,
                "user_id": data.get("id"),
                "name": data.get("name"),
                "error": None
            }
        elif response.status_code == 401:
            return {"valid": False, "error": "Token expired or revoked"}
        else:
            error_data = response.json().get("error", {})
            return {
                "valid": False,
                "error": error_data.get("message", f"HTTP {response.status_code}")
            }
    except requests.exceptions.Timeout:
        return {"valid": False, "error": "Meta API timeout"}
    except requests.exceptions.RequestException as e:
        logger.exception(f"Token validation request failed: {e}")
        return {"valid": False, "error": str(e)}


def check_phone_number_status(access_token: str, phone_number_id: str) -> Dict[str, Any]:
    """
    Check phone number status from Meta API.
    
    Returns:
        Dict with display_name status, quality rating, etc.
    """
    if not access_token or not phone_number_id:
        return {"valid": False, "error": "Missing token or phone_number_id"}
    
    try:
        url = f"{META_GRAPH_API}/{phone_number_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        params = {"fields": "display_phone_number,verified_name,code_verification_status,quality_rating,name_status"}
        
        response = requests.get(url, headers=headers, params=params, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            
            # Detect if this is a test number (Meta formats like +1 555-XXX-XXXX)
            display_number = data.get("display_phone_number", "")
            is_test_number = display_number.startswith("+1 555") or "15558" in display_number.replace(" ", "").replace("-", "")
            
            return {
                "valid": True,
                "display_phone_number": display_number,
                "verified_name": data.get("verified_name"),
                "name_status": data.get("name_status"),  # APPROVED, PENDING, DECLINED
                "quality_rating": data.get("quality_rating"),  # GREEN, YELLOW, RED
                "is_test_number": is_test_number
            }
        else:
            error_data = response.json().get("error", {})
            return {
                "valid": False,
                "error": error_data.get("message", f"HTTP {response.status_code}")
            }
    except requests.exceptions.RequestException as e:
        logger.exception(f"Phone number status check failed: {e}")
        return {"valid": False, "error": str(e)}


# ============================================================
# Main Detection Function
# ============================================================

def detect_whatsapp_connection_path(workspace_id: str) -> Dict[str, Any]:
    """
    Determine which connection path a workspace should use.
    
    This is the core detection function that tells the frontend
    whether to show Embedded Signup or Manual Linking.
    
    Args:
        workspace_id: The workspace to check
        
    Returns:
        {
            "status": "NO_ACCOUNT" | "CONNECTED" | "PARTIAL" | "RELINK_REQUIRED",
            "recommended_path": "EMBEDDED" | "MANUAL" | None,
            "reason": str,
            "account_summary": {
                "id": int,
                "waba_id": str,
                "phone_number": str | None,
                "phone_number_id": str | None,
                "display_name_status": "APPROVED" | "IN_REVIEW" | None,
                "quality_rating": str | None,
                "is_test_number": bool,
                "is_active": bool
            } | None,
            "can_use_embedded_signup": bool,
            "can_use_manual_link": bool
        }
    """
    if not workspace_id:
        return {
            "status": ConnectionStatus.NO_ACCOUNT,
            "recommended_path": RecommendedPath.EMBEDDED,
            "reason": "No workspace ID provided",
            "account_summary": None,
            "can_use_embedded_signup": True,
            "can_use_manual_link": True,
        }
    
    # Query for existing WhatsApp account (active first, then any)
    account = WhatsAppAccount.query.filter_by(
        workspace_id=workspace_id,
        is_active=True
    ).first()
    
    # ============================================================
    # FAST PATH: Active account with token + phone → return immediately
    # No Meta API calls needed — just use what's in the DB.
    # This is the common case and should be near-instant.
    # ============================================================
    if account and account.phone_number_id and account.get_access_token():
        account_summary = {
            "id": account.id,
            "waba_id": account.waba_id,
            "phone_number": account.display_phone_number,
            "phone_number_id": account.phone_number_id,
            "verified_name": account.custom_name or account.verified_name,
            "display_name_status": None,
            "quality_rating": account.quality_score,
            "is_test_number": False,
            "is_active": account.is_active,
            "is_coexistence": account.is_coexistence,
            "token_type": account.token_type,
        }
        return {
            "status": ConnectionStatus.CONNECTED,
            "recommended_path": None,
            "reason": "WhatsApp Business account is fully connected",
            "account_summary": account_summary,
            "can_use_embedded_signup": False,
            "can_use_manual_link": True,
        }
    
    # If no active account, check for inactive ones (user explicitly unlinked)
    if not account:
        account = WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id
        ).order_by(WhatsAppAccount.id.desc()).first()
        
        if account:
            # Found an inactive account — DON'T auto-reactivate.
            # The user explicitly unlinked, so let them choose to reconnect.
            account_summary = {
                "id": account.id,
                "waba_id": account.waba_id,
                "phone_number": account.display_phone_number,
                "phone_number_id": account.phone_number_id,
                "verified_name": account.custom_name or account.verified_name,
                "display_name_status": None,
                "quality_rating": account.quality_score,
                "is_test_number": False,
                "is_active": False,
                "token_type": account.token_type,
            }
            return {
                "status": ConnectionStatus.RELINK_REQUIRED,
                "recommended_path": RecommendedPath.MANUAL,
                "reason": "Account was unlinked — reconnect or switch to a different account",
                "account_summary": account_summary,
                "can_use_embedded_signup": True,   # Allow switching to a new number
                "can_use_manual_link": True,
            }
    
    # ============================================================
    # Case 1: No account exists → Path A (Embedded Signup)
    # ============================================================
    if not account:
        return {
            "status": ConnectionStatus.NO_ACCOUNT,
            "recommended_path": RecommendedPath.EMBEDDED,
            "reason": "No WhatsApp account connected to this workspace",
            "account_summary": None,
            "can_use_embedded_signup": True,
            "can_use_manual_link": True,  # User can still manually link if they have credentials
        }
    
    # ============================================================
    # Case 2: Account exists but incomplete — check completeness
    # ============================================================
    
    # Build account summary
    account_summary = {
        "id": account.id,
        "waba_id": account.waba_id,
        "phone_number": account.display_phone_number,
        "phone_number_id": account.phone_number_id,
        "verified_name": account.custom_name or account.verified_name,
        "display_name_status": None,
        "quality_rating": account.quality_score,
        "is_test_number": False,
        "is_active": account.is_active,
        "token_type": account.token_type,
    }
    
    # Check if phone_number_id is missing → PARTIAL
    if not account.phone_number_id:
        return {
            "status": ConnectionStatus.PARTIAL,
            "recommended_path": RecommendedPath.MANUAL,
            "reason": "WhatsApp account exists but phone number setup is incomplete",
            "account_summary": account_summary,
            "can_use_embedded_signup": False,  # CRITICAL: Hide Embedded Signup if ANY account exists
            "can_use_manual_link": True,
        }
    
    # Check if token exists
    access_token = account.get_access_token()
    
    if not access_token:
        return {
            "status": ConnectionStatus.RELINK_REQUIRED,
            "recommended_path": RecommendedPath.MANUAL,
            "reason": "Access token is missing - please reconnect your account",
            "account_summary": account_summary,
            "can_use_embedded_signup": False,  # CRITICAL: Hide Embedded Signup
            "can_use_manual_link": True,
        }
    
    # For accounts that reached here (e.g. just re-activated), do a quick token check
    token_check = validate_token_with_meta(access_token)
    
    if not token_check.get("valid"):
        # Token expired or invalid
        return {
            "status": ConnectionStatus.RELINK_REQUIRED,
            "recommended_path": RecommendedPath.MANUAL,
            "reason": f"Access token is invalid: {token_check.get('error', 'Unknown error')}",
            "account_summary": account_summary,
            "can_use_embedded_signup": False,  # CRITICAL: Hide Embedded Signup
            "can_use_manual_link": True,
        }
    
    # ============================================================
    # Case 3: Fully connected (re-activated or was missing from fast path)
    # ============================================================
    return {
        "status": ConnectionStatus.CONNECTED,
        "recommended_path": None,  # Already connected, no path needed
        "reason": "WhatsApp Business account is fully connected",
        "account_summary": account_summary,
        "can_use_embedded_signup": False,  # CRITICAL: Never show Embedded Signup for connected accounts
        "can_use_manual_link": True,  # Can still re-link if needed
    }


# ============================================================
# Manual Connection (Path B)
# ============================================================

def connect_manual(
    workspace_id: str,
    user_id: str,
    waba_id: str,
    phone_number_id: str,
    access_token: str,
) -> Dict[str, Any]:
    """
    Connect an existing WhatsApp account via manual credentials.
    
    SAFETY RULES:
    1. Validate token before saving
    2. Never overwrite a working token
    3. Check workspace isolation
    4. Encrypt token at rest
    
    Args:
        workspace_id: Target workspace
        user_id: User performing the connection
        waba_id: WhatsApp Business Account ID
        phone_number_id: Phone Number ID
        access_token: Access token from Meta
        
    Returns:
        Dict with success status and account info
    """
    logger.info(f"Manual WhatsApp connection attempt: workspace={workspace_id}, waba={waba_id}")
    
    # Validate inputs
    if not all([workspace_id, user_id, waba_id, phone_number_id, access_token]):
        return {
            "success": False,
            "error": "Missing required fields",
            "error_code": "MISSING_FIELDS"
        }
    
    # Step 1: Validate token with Meta
    token_check = validate_token_with_meta(access_token)
    if not token_check.get("valid"):
        return {
            "success": False,
            "error": f"Invalid access token: {token_check.get('error', 'Token validation failed')}",
            "error_code": "INVALID_TOKEN"
        }
    
    # Step 2: Validate phone_number_id
    phone_status = check_phone_number_status(access_token, phone_number_id)
    if not phone_status.get("valid"):
        return {
            "success": False,
            "error": f"Invalid phone number ID: {phone_status.get('error', 'Phone number validation failed')}",
            "error_code": "INVALID_PHONE"
        }
    
    # Step 3: Check if this WABA+phone already exists ANYWHERE in the database
    existing_account = WhatsAppAccount.query.filter_by(
        waba_id=waba_id,
        phone_number_id=phone_number_id,
    ).first()
    
    # Step 4: Handle existing account scenarios
    if existing_account:
        # Normalise workspace_id comparison (both as strings)
        existing_ws = str(existing_account.workspace_id) if existing_account.workspace_id else None
        target_ws = str(workspace_id) if workspace_id else None
        
        # Case A: Account belongs to a DIFFERENT workspace and is active
        if existing_ws != target_ws and existing_account.is_active:
            logger.warning(f"WABA {waba_id} + phone {phone_number_id} already connected to workspace {existing_account.workspace_id}")
            return {
                "success": False,
                "error": "This WhatsApp number is already connected to another workspace. Disconnect it there first.",
                "error_code": "ALREADY_CONNECTED_OTHER"
            }
        
        # Case B: Account belongs to a DIFFERENT workspace but is inactive - transfer it
        if existing_ws != target_ws and not existing_account.is_active:
            logger.info(f"Transferring inactive account from workspace {existing_account.workspace_id} to {workspace_id}")
            existing_account.workspace_id = workspace_id
        
        # Case C & D: Account belongs to THIS workspace (active or inactive) - update it
    
    if existing_account:
        # Check if existing token is still valid
        existing_token = existing_account.get_access_token()
        if existing_token:
            existing_token_check = validate_token_with_meta(existing_token)
            if existing_token_check.get("valid"):
                # Don't overwrite a working token, but ensure account is active
                was_reactivated = False
                if not existing_account.is_active:
                    existing_account.is_active = True
                    db.session.commit()
                    was_reactivated = True
                    logger.info(f"Re-activated account {existing_account.id} with valid token")
                else:
                    logger.info(f"Account already connected with valid token, no update needed")
                return {
                    "success": True,
                    "message": "Account already connected with valid token",
                    "account": existing_account.to_dict(),
                    "was_updated": was_reactivated
                }
        
        # Update existing account with new token
        existing_account.set_access_token(access_token, "permanent")
        existing_account.is_active = True
        existing_account.connected_by_user_id = user_id
        existing_account.last_synced_at = datetime.now(timezone.utc)
        existing_account.display_phone_number = phone_status.get("display_phone_number")
        existing_account.verified_name = phone_status.get("verified_name") or existing_account.verified_name
        existing_account.quality_score = phone_status.get("quality_rating")
        
        db.session.commit()
        
        logger.info(f"Updated existing WhatsApp account: {existing_account.id}")
        
        # Run post-connection setup (webhook subscription, etc.)
        setup_result = _run_post_connection_setup(existing_account.id, waba_id, access_token)
        
        return {
            "success": True,
            "message": "Account reconnected successfully",
            "account": existing_account.to_dict(),
            "was_updated": True,
            "setup": setup_result
        }
    
    # Step 5: Create new account
    new_account = WhatsAppAccount(
        workspace_id=workspace_id,
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        display_phone_number=phone_status.get("display_phone_number"),
        verified_name=phone_status.get("verified_name"),
        quality_score=phone_status.get("quality_rating"),
        connected_by_user_id=user_id,
        is_active=True,
    )
    new_account.set_access_token(access_token, "permanent")
    new_account.last_synced_at = datetime.now(timezone.utc)
    
    db.session.add(new_account)
    db.session.commit()
    
    logger.info(f"Created new WhatsApp account: {new_account.id} for workspace {workspace_id}")
    
    # Step 6: Auto-setup - Subscribe WABA to webhooks
    setup_result = _run_post_connection_setup(new_account.id, waba_id, access_token)
    
    return {
        "success": True,
        "message": "Account connected successfully",
        "account": new_account.to_dict(),
        "was_updated": False,
        "is_new": True,
        "setup": setup_result
    }


def _run_post_connection_setup(account_id: int, waba_id: str, access_token: str) -> Dict[str, Any]:
    """
    Run post-connection setup tasks:
    1. Subscribe WABA to webhooks
    2. Validate webhook configuration
    3. Log any issues for debugging
    
    This ensures new users don't have to manually configure webhooks.
    """
    setup_results = {
        "webhook_subscription": None,
        "health_check": None,
        "issues": []
    }
    
    try:
        from .health_check import subscribe_waba_to_webhooks, perform_health_check
        
        # Auto-subscribe WABA to webhooks
        success, message, details = subscribe_waba_to_webhooks(waba_id, access_token)
        setup_results["webhook_subscription"] = {
            "success": success,
            "message": message,
            "details": details
        }
        
        if not success:
            setup_results["issues"].append(f"Webhook subscription failed: {message}")
            logger.warning(f"⚠️ Webhook subscription failed for account {account_id}: {message}")
        else:
            logger.info(f"✅ Webhook subscription successful for account {account_id}")
        
        # Run health check
        health_result = perform_health_check(account_id, auto_fix=True)
        setup_results["health_check"] = health_result
        
        if health_result.get("overall_status") == "critical":
            setup_results["issues"].append("Health check found critical issues")
        
    except Exception as e:
        logger.exception(f"Post-connection setup error: {e}")
        setup_results["issues"].append(f"Setup error: {str(e)}")
    
    return setup_results
