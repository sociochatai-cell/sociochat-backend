"""
Meta OAuth Integration for WhatsApp Business Account
====================================================
Phase-2 Part-1: Embedded Signup Flow

Handles Meta OAuth flow to connect WhatsApp Business Accounts.
"""

import os
import logging
import secrets
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from flask import session, request

from shared_models import db
from .models import WhatsAppAccount
from .encryption import encrypt_token
from .utils import subscribe_waba_to_app

logger = logging.getLogger(__name__)

# Meta OAuth Configuration (read all common env names used across deploys / Docker)
META_APP_ID = os.getenv("META_APP_ID") or os.getenv("FB_APP_ID") or os.getenv("WHATSAPP_APP_ID")
META_APP_SECRET = (
    os.getenv("META_APP_SECRET")
    or os.getenv("FB_APP_SECRET")
    or os.getenv("WHATSAPP_APP_SECRET")
)
META_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v22.0")
# OAuth dialog uses a fixed version, but token exchange uses configurable version
META_OAUTH_BASE = f"https://www.facebook.com/{META_API_VERSION}/dialog/oauth"
META_TOKEN_EXCHANGE = f"https://graph.facebook.com/{META_API_VERSION}/oauth/access_token"
META_GRAPH_API = f"https://graph.facebook.com/{META_API_VERSION}"

# Required scopes for WhatsApp Business
REQUIRED_SCOPES = [
    "whatsapp_business_messaging",
    "whatsapp_business_management",
    "business_management",
]


def get_redirect_uri() -> str:
    """Get OAuth callback URL."""
    app_base = os.getenv("APP_BASE_URL", "https://sociovia-backend-362038465411.europe-west1.run.app")
    return f"{app_base}/api/whatsapp/connect/callback"


def generate_state() -> str:
    """Generate a random state token for OAuth security."""
    return secrets.token_urlsafe(32)


def get_oauth_url(workspace_id: str, user_id: str) -> Dict[str, Any]:
    """
    Generate Meta OAuth URL for Embedded Signup.
    
    Args:
        workspace_id: Workspace ID to associate account with
        user_id: User ID who is connecting the account
        
    Returns:
        Dict with auth_url and state
    """
    if not META_APP_ID:
        raise ValueError("META_APP_ID environment variable not set")
    
    state = generate_state()
    
    # Store state in session for verification
    session[f"wa_oauth_state_{workspace_id}"] = state
    session[f"wa_oauth_workspace_{state}"] = workspace_id
    session[f"wa_oauth_user_{state}"] = user_id
    
    params = {
        "client_id": META_APP_ID,
        "redirect_uri": get_redirect_uri(),
        "state": state,
        "scope": ",".join(REQUIRED_SCOPES),
        "response_type": "code",
    }
    
    auth_url = f"{META_OAUTH_BASE}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
    
    return {
        "auth_url": auth_url,
        "state": state,
    }


def exchange_code_for_token(code: str, state: str) -> Dict[str, Any]:
    """
    Exchange authorization code for access token.
    
    Args:
        code: Authorization code from Meta
        state: State token for verification
        
    Returns:
        Dict with access_token, token_type, expires_in
    """
    if not META_APP_SECRET:
        raise ValueError("META_APP_SECRET environment variable not set")
    
    # Verify state
    workspace_id = session.get(f"wa_oauth_workspace_{state}")
    user_id = session.get(f"wa_oauth_user_{state}")
    
    if not workspace_id or not user_id:
        raise ValueError("Invalid or expired OAuth state")
    
    # Exchange code for token
    params = {
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "redirect_uri": get_redirect_uri(),
        "code": code,
    }
    
    response = requests.get(META_TOKEN_EXCHANGE, params=params, timeout=30)
    response.raise_for_status()
    
    token_data = response.json()
    
    if "error" in token_data:
        raise ValueError(f"Token exchange failed: {token_data['error']}")
    
    access_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in")  # Seconds
    
    if not access_token:
        raise ValueError("No access token in response")
    
    # Calculate expiration
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    
    return {
        "access_token": access_token,
        "token_type": token_data.get("token_type", "bearer"),
        "expires_in": expires_in,
        "expires_at": expires_at,
        "workspace_id": workspace_id,
        "user_id": user_id,
    }


def fetch_waba_info(access_token: str) -> Dict[str, Any]:
    """
    Fetch WhatsApp Business Account information from Meta (canonical discovery).

    Uses ``meta_asset_discovery`` (Business → owned WABAs → phone_numbers).
    On ambiguous portfolios, falls back to the first candidate only when
    ``allow_legacy_single_guess`` is enabled inside resolver (True here for
    backward compatibility with older single-account OAuth flows).
    """
    from .meta_asset_discovery import candidate_to_legacy_oauth_dict, resolve_binding_for_auto_connect

    binding, _disc = resolve_binding_for_auto_connect(
        access_token,
        api_version=META_API_VERSION,
        app_id=META_APP_ID,
        app_secret=META_APP_SECRET,
        hints=None,
        allow_legacy_single_guess=True,
    )
    return candidate_to_legacy_oauth_dict(binding)


def save_whatsapp_account(
    workspace_id: str,
    user_id: str,
    access_token: str,
    token_expires_at: Optional[datetime],
    waba_info: Dict[str, Any],
) -> WhatsAppAccount:
    """
    Save or update WhatsApp account in database.
    
    ALSO automatically sets up flow encryption:
    1. Generates RSA key pair
    2. Saves private key encrypted
    3. Uploads public key to Meta
    
    Args:
        workspace_id: Workspace ID
        user_id: User ID who connected
        access_token: Access token (will be encrypted)
        token_expires_at: Token expiration
        waba_info: WABA information from Meta
        
    Returns:
        WhatsAppAccount instance
    """
    # GUARD: Block cross-workspace conflict before saving
    from .connection_guard import check_phone_available
    conflict = check_phone_available(waba_info["phone_number_id"], workspace_id)
    if conflict:
        raise ValueError(conflict["error"])
    
    # Check if account already exists (by phone_number_id globally, not per workspace)
    account = WhatsAppAccount.query.filter_by(
        phone_number_id=waba_info["phone_number_id"],
    ).first()
    
    is_new_account = account is None
    
    if account:
        # Update existing account (safe: guard passed above)
        account.workspace_id = workspace_id
        account.waba_id = waba_info["waba_id"]
        account.set_access_token(access_token, "permanent", token_expires_at)
        account.connected_by_user_id = user_id
        account.display_phone_number = waba_info.get("display_phone_number")
        account.verified_name = waba_info.get("waba_name")
        account.last_synced_at = datetime.now(timezone.utc)
        account.is_active = True
        if waba_info.get("meta_business_id"):
            account.meta_business_id = str(waba_info["meta_business_id"])
    else:
        # Create new account
        account = WhatsAppAccount(
            workspace_id=workspace_id,
            waba_id=waba_info["waba_id"],
            phone_number_id=waba_info["phone_number_id"],
            display_phone_number=waba_info.get("display_phone_number"),
            verified_name=waba_info.get("waba_name"),
            connected_by_user_id=user_id,
            is_active=True,
            meta_business_id=str(waba_info["meta_business_id"]) if waba_info.get("meta_business_id") else None,
        )
        account.set_access_token(access_token, "permanent", token_expires_at)
        account.last_synced_at = datetime.now(timezone.utc)
        db.session.add(account)
    
    db.session.commit()
    
    logger.info(f"Saved WhatsApp account: WABA {waba_info['waba_id']} for workspace {workspace_id}")

    try:
        uid = int(str(user_id).strip())
        from monolith_integration.trigger import schedule_capabilities_resync_for_accounts

        schedule_capabilities_resync_for_accounts([account.id], uid, reason="whatsapp_oauth_save")
    except Exception as exc:
        logger.warning("[whatsapp_integration] capability projection schedule failed: %s", exc)
    
    # Subscribe WABA to app for webhooks (messages, template updates, etc.)
    try:
        subscribe_waba_to_app(waba_info["waba_id"], access_token)
    except Exception as e:
        logger.warning(f"Initial webhook subscription failed for WABA {waba_info['waba_id']}: {e}")
    
    # ============================================================
    # AUTOMATIC FLOW ENCRYPTION SETUP
    # ============================================================
    # Only setup flow keys if:
    # 1. New account OR account doesn't have keys yet
    # 2. Access token is available
    
    if is_new_account or not account.has_flow_keys():
        try:
            from .flow_endpoint import setup_flow_encryption_for_account
            
            logger.info(f"Setting up flow encryption for WABA {waba_info['waba_id']}")
            result = setup_flow_encryption_for_account(account)
            
            if result.get("success"):
                logger.info(f"Flow encryption configured for WABA {waba_info['waba_id']}")
            else:
                logger.warning(f"Flow encryption setup partial for WABA {waba_info['waba_id']}: {result.get('error')}")
                # Keys saved but not uploaded - can retry later via /keys/upload
                
        except Exception as e:
            logger.exception(f"Flow encryption setup failed for WABA {waba_info['waba_id']}: {e}")
            # Don't fail account creation - flow setup can be retried
    
    return account


def exchange_short_for_long_token(short_token: str) -> Dict[str, Any]:
    """
    Exchange a short-lived access token from Facebook SDK for a long-lived token.
    
    This is used for simple Facebook OAuth login (not Embedded Signup).
    The short-lived token comes from the FB.login() callback on frontend.
    
    Args:
        short_token: Short-lived access token from Facebook SDK
        
    Returns:
        Dict with long_token, expires_in, token_type
    """
    if not META_APP_ID:
        raise ValueError("META_APP_ID environment variable not set")
    if not META_APP_SECRET:
        raise ValueError("META_APP_SECRET environment variable not set")
    
    # Exchange short token for long-lived token
    exchange_url = f"{META_GRAPH_API}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "fb_exchange_token": short_token,
    }
    
    response = requests.get(exchange_url, params=params, timeout=30)
    
    if response.status_code != 200:
        error_data = response.json() if response.content else {}
        error_msg = error_data.get("error", {}).get("message", "Token exchange failed")
        logger.error(f"Facebook token exchange failed: {error_msg}")
        raise ValueError(error_msg)
    
    token_data = response.json()
    
    if "error" in token_data:
        raise ValueError(f"Token exchange failed: {token_data['error']}")
    
    long_token = token_data.get("access_token")
    expires_in = token_data.get("expires_in")  # Usually 60 days
    
    if not long_token:
        raise ValueError("No access token in exchange response")
    
    # Calculate expiration
    expires_at = None
    if expires_in:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    
    return {
        "access_token": long_token,
        "token_type": token_data.get("token_type", "bearer"),
        "expires_in": expires_in,
        "expires_at": expires_at,
    }


def validate_facebook_token(access_token: str) -> Dict[str, Any]:
    """
    Validate a Facebook access token and return user/app info.
    
    Args:
        access_token: Facebook access token to validate
        
    Returns:
        Dict with user_id, app_id, is_valid, scopes
    """
    debug_url = f"{META_GRAPH_API}/debug_token"
    params = {
        "input_token": access_token,
        "access_token": f"{META_APP_ID}|{META_APP_SECRET}",
    }
    
    response = requests.get(debug_url, params=params, timeout=30)
    
    if response.status_code != 200:
        return {"is_valid": False, "error": "Failed to validate token"}
    
    data = response.json().get("data", {})
    
    return {
        "is_valid": data.get("is_valid", False),
        "user_id": data.get("user_id"),
        "app_id": data.get("app_id"),
        "scopes": data.get("scopes", []),
        "expires_at": data.get("expires_at"),
    }

