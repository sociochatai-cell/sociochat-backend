"""
WhatsApp Account Service
========================

Handles account management operations:
  - _get_or_create_account
  - _get_workspace_account
  - ensure_all_waba_subscriptions
  - connect_account (OAuth)
  - get_oauth_url
"""

import os
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import requests

from .models import WhatsAppAccount, shadow_sync_account_quality_rating
from .utils import subscribe_waba_to_app

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com"


class WhatsAppAccountService:
    """
    Mixin for WhatsApp account management operations.

    All methods expect self.db_session, self.phone_number_id,
    self.access_token, and self.waba_id to exist
    (provided by the base WhatsAppService via MRO).
    """

    def _get_workspace_account(self, workspace_id: str) -> Optional["WhatsAppAccount"]:
        """Get active WhatsApp account for workspace."""
        return WhatsAppAccount.query.filter_by(
            workspace_id=workspace_id,
            is_active=True,
        ).order_by(WhatsAppAccount.created_at.desc()).first()

    def _get_or_create_account(self) -> WhatsAppAccount:
        """Get or create WhatsApp account record."""
        account = WhatsAppAccount.query.filter_by(
            phone_number_id=self.phone_number_id
        ).first()

        if not account:
            account = WhatsAppAccount(
                waba_id=self.waba_id or "unknown",
                phone_number_id=self.phone_number_id,
                display_phone_number=self.phone_number_id,  # Will be updated from API
                is_active=True,
            )
            self.db_session.add(account)
            self.db_session.flush()
            logger.info(f"Created new WhatsApp account: {self.phone_number_id}")

        return account

    @staticmethod
    def ensure_all_waba_subscriptions():
        """
        Startup task to ensure ALL active WhatsApp accounts are subscribed to the app.
        This fixes stale or missing subscriptions in production without manual intervention.
        """
        try:
            active_accounts = WhatsAppAccount.query.filter_by(is_active=True).all()
            logger.info(f"Startup: Ensuring webhook subscriptions for {len(active_accounts)} active WABAs.")

            for account in active_accounts:
                token = account.get_access_token()
                if not token:
                    continue

                subscribe_waba_to_app(account.waba_id, token)

            return True
        except Exception as e:
            logger.error(f"Failed to ensure all WABA subscriptions: {e}")
            return False

    # ============================================================
    # Phase-2: OAuth Methods
    # ============================================================

    def get_oauth_url(self, workspace_id: str) -> str:
        """Generate the Meta Embedded Signup URL."""
        try:
            from tenant.integration import get_tenant_meta_config
            _cfg = get_tenant_meta_config(workspace_id=workspace_id)
        except Exception:
            _cfg = None
        app_id = (getattr(_cfg, "app_id", None) if _cfg else None) or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        api_ver = (getattr(_cfg, "whatsapp_api_version", None) if _cfg else None) or os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION", "v22.0")
        base_url = os.getenv("APP_BASE_URL", "https://sociovia-backend-362038465411.europe-west1.run.app").rstrip("/")
        redirect_uri = f"{base_url}/api/whatsapp/connect/callback"

        if not app_id:
            raise ValueError("META_APP_ID or FB_APP_ID environment variable not set")

        scopes = "whatsapp_business_management,whatsapp_business_messaging,business_management"

        return (
            f"https://www.facebook.com/{api_ver}/dialog/oauth?"
            f"client_id={app_id}&"
            f"redirect_uri={redirect_uri}&"
            f"state={workspace_id}&"
            f"scope={scopes}&"
            f"response_type=code"
        )

    def connect_account(self, code: str, workspace_id: str):
        """Exchange code for token and store account details."""
        try:
            from tenant.integration import get_tenant_meta_config
            _cfg = get_tenant_meta_config(workspace_id=workspace_id)
        except Exception:
            _cfg = None
        app_id = (getattr(_cfg, "app_id", None) if _cfg else None) or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
        app_secret = (getattr(_cfg, "app_secret", None) if _cfg else None) or os.getenv("META_APP_SECRET") or os.getenv("FB_APP_SECRET")
        api_ver = (getattr(_cfg, "whatsapp_api_version", None) if _cfg else None) or os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION", "v22.0")
        base_url = os.getenv("APP_BASE_URL", "https://sociovia-backend-362038465411.europe-west1.run.app").rstrip("/")
        redirect_uri = f"{base_url}/api/whatsapp/connect/callback"

        # 1. Exchange code for User Access Token
        token_url = (
            f"https://graph.facebook.com/{api_ver}/oauth/access_token?"
            f"client_id={app_id}&"
            f"client_secret={app_secret}&"
            f"redirect_uri={redirect_uri}&"
            f"code={code}"
        )

        resp = requests.get(token_url)
        data = resp.json()

        if "error" in data:
            raise Exception(f"Token exchange failed: {data['error'].get('message')}")

        access_token = data["access_token"]

        from .meta_asset_discovery import DiscoveryAmbiguousError, resolve_binding_for_auto_connect

        try:
            binding, _disc = resolve_binding_for_auto_connect(
                access_token,
                api_version=api_ver,
                app_id=app_id,
                app_secret=app_secret,
                hints=None,
                allow_legacy_single_guess=False,
            )
        except DiscoveryAmbiguousError as e:
            raise Exception(
                "Multiple WhatsApp Business assets found for this login. "
                "Use OAuth connect with hints or Embedded Signup to select an asset."
            ) from e

        waba_id = binding["waba_id"]
        waba_name = binding.get("waba_name") or "Unknown WABA"
        phone_id = binding["phone_number_id"]
        display_number = binding.get("display_phone_number")
        quality_rating = binding.get("quality_rating")
        meta_business_id = binding.get("meta_business_id")

        # 3. Store in Database — with cross-workspace guard
        from .connection_guard import check_phone_available
        conflict = check_phone_available(phone_id, workspace_id)
        if conflict:
            raise Exception(conflict["error"])

        existing = WhatsAppAccount.query.filter_by(
            phone_number_id=phone_id
        ).first()

        if existing:
            account = existing
            account.workspace_id = workspace_id  # Safe: guard passed above
            account.is_active = True
        else:
            account = WhatsAppAccount(phone_number_id=phone_id)
            self.db_session.add(account)

        # Update fields
        account.waba_id = waba_id
        account.workspace_id = workspace_id
        account.display_phone_number = display_number
        account.verified_name = waba_name
        if meta_business_id:
            account.meta_business_id = str(meta_business_id)
        if quality_rating:
            shadow_sync_account_quality_rating(account, quality_rating, db_session=self.db_session)
        else:
            account.quality_score = None

        # Save Encrypted Token
        account.set_access_token(access_token, token_type="permanent")
        account.last_synced_at = datetime.now(timezone.utc)

        self.db_session.commit()
        logger.info(f"Connected WhatsApp account {display_number} for workspace {workspace_id}")

        # AUTO-REGISTER PHONE NUMBER WITH WHATSAPP BUSINESS API
        try:
            register_resp = requests.post(
                f"https://graph.facebook.com/{api_ver}/{phone_id}/register",
                json={
                    "messaging_product": "whatsapp",
                    "pin": "123456"  # Default 6-digit PIN for 2FA
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                timeout=15
            )
            register_result = register_resp.json()
            if register_result.get("success"):
                logger.info(f"Phone number {phone_id} auto-registered with WhatsApp Business API")
            else:
                logger.warning(f"Phone registration response: {register_result}")
        except Exception as reg_error:
            logger.warning(f"Phone registration warning (may already be registered): {reg_error}")

        return account
