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

from shared_models import db
from .models import WhatsAppAccount, shadow_sync_account_quality_rating
from .encryption import encrypt_token, decrypt_token

logger = logging.getLogger(__name__)

# Maps Sociovia.com plan names → SocioChat daily message limits.
# SocioChat's own plans are looked up from subscription constants directly.
_SOCIOVIA_PLAN_TO_DAILY_LIMIT = {
    "ai_free":      0,      # blocked — should never reach here but guard anyway
    "ai_starter":   1000,
    "ai_growth":    5000,
    "ai_custom":    None,   # unlimited
    # legacy sociovia plans
    "starter":      1000,
    "growth":       5000,
    "enterprise":   None,   # unlimited
    "beta":         5000,
}

def _resolve_daily_limit_for_user(workspace_id: str) -> Optional[int]:
    """
    Returns the daily_message_limit to seed for a workspace owner.
    Reads the owner's plan from the users table and maps it to a limit.
    Returns None = unlimited.
    """
    try:
        from shared_models import User
        from subscription.constants import PLAN_LIMITS
        result = db.session.execute(
            db.text("SELECT u.plan FROM users u JOIN workspaces2 w ON w.user_id = u.id WHERE w.id = :wid LIMIT 1"),
            {"wid": workspace_id}
        ).fetchone()
        if not result:
            return 1000  # safe default
        plan = result[0] or "starter"
        # Sociovia ai_* plans
        if plan in _SOCIOVIA_PLAN_TO_DAILY_LIMIT:
            return _SOCIOVIA_PLAN_TO_DAILY_LIMIT[plan]
        # SocioChat native plans
        plan_cfg = PLAN_LIMITS.get(plan, {})
        return plan_cfg.get("messages_per_day", 1000)
    except Exception as e:
        logger.warning("_resolve_daily_limit_for_user failed (default 1000): %s", e)
        return 1000


def _seed_capabilities(account_id: int, workspace_id: str) -> None:
    """
    Seed whatsapp_account_capabilities row on new/reconnected account.
    Uses ON CONFLICT DO NOTHING so existing rows (set by Sociovia monolith) are preserved.
    """
    try:
        daily_limit = _resolve_daily_limit_for_user(workspace_id)
        db.session.execute(db.text("""
            INSERT INTO whatsapp_account_capabilities
                (account_id, subscription_status, ai_enabled, automation_enabled,
                 broadcast_enabled, daily_message_limit, projection_version, updated_at)
            VALUES
                (:aid, 'ACTIVE', true, true, true, :lim, 1, NOW())
            ON CONFLICT (account_id) DO NOTHING
        """), {"aid": account_id, "lim": daily_limit})
        db.session.commit()
        logger.info("Seeded capabilities for account %s (daily_limit=%s)", account_id, daily_limit)
    except Exception as e:
        logger.warning("_seed_capabilities failed for account %s: %s", account_id, e)

META_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v24.0")
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


def _meta_app_credentials() -> tuple[Optional[str], Optional[str]]:
    """Resolve Meta app id/secret used for WhatsApp (prefer explicit WhatsApp app env)."""
    app_id = (
        os.getenv("WHATSAPP_APP_ID")
        or os.getenv("META_APP_ID")
        or os.getenv("FB_APP_ID")
    )
    app_secret = (
        os.getenv("WHATSAPP_APP_SECRET")
        or os.getenv("META_APP_SECRET")
        or os.getenv("FB_APP_SECRET")
    )
    return app_id, app_secret


def verify_messaging_send_access(
    access_token: str,
    phone_number_id: str,
    waba_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Verify the token can send messages for this phone/WABA (not just read metadata).

    Partner-assigned CLIENT_OWNED WABAs often fail with Meta error #200 when using a
    personal user token or when the app lacks Advanced Access — even if Business Manager
    shows "Full control".
    """
    if not access_token or not phone_number_id:
        return {
            "can_send": False,
            "error": "Missing access token or phone_number_id",
            "error_code": "MISSING_FIELDS",
        }

    details: Dict[str, Any] = {}
    app_id, app_secret = _meta_app_credentials()

    if app_id and app_secret:
        try:
            debug_resp = requests.get(
                f"{META_GRAPH_API}/debug_token",
                params={
                    "input_token": access_token,
                    "access_token": f"{app_id}|{app_secret}",
                },
                timeout=15,
            )
            debug_json = debug_resp.json()
            if debug_resp.status_code != 200:
                err = debug_json.get("error", {})
                details["debug_error"] = err.get("message")
                if err.get("code") == 100 and "did not match" in (err.get("message") or ""):
                    return {
                        "can_send": False,
                        "error": (
                            "This access token belongs to a different Meta app than Sociovia is "
                            f"configured to use. Reconnect using the Sociovia WhatsApp app or paste a "
                            f"System User token generated for app {app_id}."
                        ),
                        "error_code": "TOKEN_APP_MISMATCH",
                        "details": details,
                    }
            else:
                data = debug_json.get("data", {})
                details["token_type"] = data.get("type")
                details["token_app_id"] = data.get("app_id")
                details["scopes"] = data.get("scopes", [])
                token_type = data.get("type")
                if token_type == "USER":
                    details["hint"] = (
                        "Personal user tokens often cannot send on client-owned partner WABAs. "
                        "Use a System User access token from Sociovia Business Manager."
                    )
                if waba_id:
                    messaging_targets: list[str] = []
                    management_targets: list[str] = []
                    has_messaging_granular = False
                    for scope in data.get("granular_scopes") or []:
                        scope_name = scope.get("scope")
                        targets = [str(t) for t in (scope.get("target_ids") or [])]
                        if scope_name == "whatsapp_business_messaging":
                            has_messaging_granular = True
                            messaging_targets = targets
                            details["messaging_waba_targets"] = targets
                        elif scope_name == "whatsapp_business_management":
                            management_targets = targets
                            details["management_waba_targets"] = targets

                    # Meta often returns empty target_ids for System User tokens even when
                    # the WABA is assigned — only block when Meta lists explicit targets
                    # and this WABA is missing from the list.
                    if has_messaging_granular and len(messaging_targets) > 0:
                        if waba_id not in messaging_targets:
                            hints = [
                                f"In Sociovia Business Manager → System users: assign WABA {waba_id} "
                                "to this system user with WhatsApp permissions.",
                                "Regenerate the token and select this WhatsApp Business Account "
                                "when Meta asks which assets to grant.",
                                "Ensure both whatsapp_business_messaging and "
                                "whatsapp_business_management include this WABA.",
                            ]
                            if waba_id in management_targets and waba_id not in messaging_targets:
                                hints.insert(
                                    0,
                                    "This token has management scope for the WABA but not messaging — "
                                    "regenerate the token and enable messaging for this asset.",
                                )
                            return {
                                "can_send": False,
                                "error": (
                                    f"Token is not granted whatsapp_business_messaging for WABA {waba_id}."
                                ),
                                "error_code": "WABA_NOT_IN_MESSAGING_SCOPE",
                                "details": details,
                                "hints": hints,
                            }
                    elif has_messaging_granular and len(messaging_targets) == 0:
                        details["granular_targets_note"] = (
                            "debug_token returned no WABA target_ids for messaging; "
                            "will verify with live send probe."
                        )
        except requests.exceptions.RequestException as e:
            logger.warning("debug_token during send verification failed: %s", e)

    if waba_id:
        try:
            waba_resp = requests.get(
                f"{META_GRAPH_API}/{waba_id}",
                params={"fields": "ownership_type,name", "access_token": access_token},
                timeout=15,
            )
            if waba_resp.status_code == 200:
                waba_data = waba_resp.json()
                details["waba_ownership_type"] = waba_data.get("ownership_type")
                details["waba_name"] = waba_data.get("name")
        except requests.exceptions.RequestException:
            pass

    try:
        probe_resp = requests.post(
            f"{META_GRAPH_API}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": "10000000000",
                "type": "text",
                "text": {"body": "sociovia_send_probe"},
            },
            timeout=15,
        )
        if probe_resp.status_code == 200:
            return {"can_send": True, "details": details}

        err = probe_resp.json().get("error", {})
        code = err.get("code")
        msg = err.get("message", "Send permission check failed")
        details["probe_error_code"] = code
        details["probe_message"] = msg

        if code == 200:
            ownership = details.get("waba_ownership_type")
            hints = [
                "In Trusthomes Business Manager → Partners → Sociovia: grant full control on the "
                "WhatsApp account, including permission to send messages on behalf of the WABA.",
                "In Sociovia Meta App (Sociovia.ai): ensure Advanced Access is approved for "
                "whatsapp_business_messaging and whatsapp_business_management.",
                "Generate a permanent System User token in Sociovia Business Manager, assign the "
                "Trusthomes WABA to that system user, and reconnect via Manual Link in Sociovia.",
            ]
            if ownership == "CLIENT_OWNED":
                hints.insert(
                    0,
                    "This is a partner/client-owned WABA — personal Facebook login tokens usually "
                    "cannot send. Use a System User token instead.",
                )
            return {
                "can_send": False,
                "error": msg,
                "error_code": "MESSAGING_PERMISSION_DENIED",
                "details": details,
                "hints": hints,
            }

        # Other errors (invalid recipient, etc.) mean the send endpoint accepted our auth.
        return {"can_send": True, "details": details}
    except requests.exceptions.RequestException as e:
        return {
            "can_send": False,
            "error": f"Send permission probe failed: {e}",
            "error_code": "PROBE_FAILED",
            "details": details,
        }


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
            "token_type": account.token_type,
            "is_coexistence": bool(getattr(account, "is_coexistence", False)),
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
                "is_coexistence": bool(getattr(account, "is_coexistence", False)),
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
        "is_coexistence": bool(getattr(account, "is_coexistence", False)),
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

    # Step 2b: Verify this token can actually send (not just read phone metadata)
    send_check = verify_messaging_send_access(access_token, phone_number_id, waba_id)
    if not send_check.get("can_send"):
        return {
            "success": False,
            "error": send_check.get("error", "Token cannot send messages for this WhatsApp account"),
            "error_code": send_check.get("error_code", "MESSAGING_PERMISSION_DENIED"),
            "hints": send_check.get("hints", []),
            "details": send_check.get("details"),
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
            existing_send_check = verify_messaging_send_access(
                existing_token, phone_number_id, waba_id
            )
            if existing_token_check.get("valid") and existing_send_check.get("can_send"):
                # Don't overwrite a token that can actually send
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
            if existing_token_check.get("valid") and not existing_send_check.get("can_send"):
                logger.warning(
                    "Replacing token for account %s: valid for /me but cannot send (%s)",
                    existing_account.id,
                    existing_send_check.get("error_code"),
                )
        
        # Update existing account with new token
        existing_account.set_access_token(access_token, "permanent")
        existing_account.is_active = True
        existing_account.connected_by_user_id = user_id
        existing_account.last_synced_at = datetime.now(timezone.utc)
        existing_account.display_phone_number = phone_status.get("display_phone_number")
        existing_account.verified_name = phone_status.get("verified_name") or existing_account.verified_name
        qr = phone_status.get("quality_rating")
        if qr:
            shadow_sync_account_quality_rating(existing_account, qr, db_session=db.session)
        else:
            existing_account.quality_score = None
        
        db.session.commit()
        
        logger.info(f"Updated existing WhatsApp account: {existing_account.id}")

        # Seed capabilities if row missing (ON CONFLICT DO NOTHING preserves existing)
        _seed_capabilities(existing_account.id, workspace_id)

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
        connected_by_user_id=user_id,
        is_active=True,
    )
    new_account.set_access_token(access_token, "permanent")
    new_account.last_synced_at = datetime.now(timezone.utc)
    
    db.session.add(new_account)
    db.session.flush()
    qr = phone_status.get("quality_rating")
    if qr:
        shadow_sync_account_quality_rating(new_account, qr, db_session=db.session)
    db.session.commit()
    
    logger.info(f"Created new WhatsApp account: {new_account.id} for workspace {workspace_id}")

    # Seed capabilities row with plan-based daily limit
    _seed_capabilities(new_account.id, workspace_id)

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
    Run post-connection setup via the canonical provisioning pipeline.
    All setup (webhook subscription, warmup, templates, capability matrix)
    is handled by the provisioning engine.
    """
    setup_results = {
        "webhook_subscription": None,
        "health_check": None,
        "issues": []
    }
    
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            setup_results["issues"].append("Account not found")
            return setup_results

        from .provisioning_engine import provision_account

        result = provision_account(
            account=account,
            access_token=access_token,
            source="manual_connect",
            is_new_account=False,
        )
        
        setup_results["provisioning"] = result.to_dict()
        setup_results["webhook_subscription"] = {
            "success": result.capability_matrix.get("webhook_subscribed", 
                       __import__("types").SimpleNamespace(enabled=False)).enabled,
        }
        setup_results["health_check"] = {
            "overall_status": "healthy" if result.readiness_score >= 70 else "warning",
            "readiness_score": result.readiness_score,
        }
        if result.errors:
            setup_results["issues"].extend(result.errors)

    except Exception as e:
        logger.exception(f"Post-connection setup error: {e}")
        setup_results["issues"].append(f"Setup error: {str(e)}")
    
    return setup_results
