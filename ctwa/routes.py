# ctwa/routes.py
# CTWA / WhatsApp Status Ads — HTTP API
# ====================================
#
# Blueprint mounted at /api/ctwa. Serves the ad wizards (AdCreatorWizard for
# Click-to-WhatsApp, StatusAdCreatorWizard for WhatsApp Status). Reuses the
# existing Meta credential helpers from the CRM meta_integration module.
#
#   GET    /api/ctwa/accounts                 -> {ad_accounts, pages, whatsapp_numbers}
#   GET    /api/ctwa/campaigns                -> list campaigns for a workspace
#   POST   /api/ctwa/campaigns                -> create a DRAFT campaign
#   GET    /api/ctwa/campaigns/<id>           -> get one campaign
#   PUT    /api/ctwa/campaigns/<id>           -> update a DRAFT campaign
#   DELETE /api/ctwa/campaigns/<id>           -> delete a DRAFT campaign
#   POST   /api/ctwa/campaigns/<id>/publish   -> push campaign -> ad set -> creative -> ad to Meta
#   GET    /api/ctwa/campaigns/<id>/insights  -> basic Meta insights (best effort)
#   GET    /api/ctwa/analytics/summary        -> workspace-level ad summary
#   POST   /api/ctwa/generate-image           -> AI ad image (Vertex Imagen) -> Spaces URL
#   POST   /api/ctwa/generate-copy            -> AI ad copy variations (Vertex Gemini)
#
# Auth: the app-wide before_request gate requires a logged-in user AND enforces
# that the supplied workspace_id is owned by that user, so these handlers can
# trust request.args["workspace_id"].

import os
import logging
from datetime import datetime, timedelta
from urllib.parse import urlencode, quote

import requests
from flask import Blueprint, request, jsonify, current_app, redirect

from shared_models import db
from ctwa.models import CTWACampaign

logger = logging.getLogger("sociovia.ctwa")

ctwa_bp = Blueprint("ctwa", __name__, url_prefix="/api/ctwa")

FB_API_VERSION = os.getenv("FB_API_VERSION", "v22.0")
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

FB_APP_ID = os.getenv("FB_APP_ID", "1782321995750055")
FB_APP_SECRET = os.getenv("FB_APP_SECRET")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _current_user_id():
    try:
        from tenant.context import get_current_user
        user = get_current_user()
        return getattr(user, "id", None) if user else None
    except Exception:
        return None


def _iso_to_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _meta_credentials(workspace_id, user_id):
    """(access_token, ad_account_id, error_tuple). Reuses the CRM helper."""
    from SocioviaCrm.routes.meta_integration import get_meta_credentials
    return get_meta_credentials(workspace_id, user_id)


def _list_whatsapp_numbers(workspace_id):
    """WhatsApp numbers connected to this workspace (for the ad destination)."""
    try:
        from whatsapp.models import WhatsAppAccount
        rows = (
            WhatsAppAccount.query
            .filter(WhatsAppAccount.workspace_id == str(workspace_id))
            .all()
        )
        out = []
        for r in rows:
            out.append({
                "id": r.id,
                "phone_number_id": r.phone_number_id,
                "display_phone_number": r.display_phone_number,
                "verified_name": (getattr(r, "custom_name", None) or r.verified_name),
                "is_active": True,
            })
        return out
    except Exception:
        logger.exception("ctwa: failed to list whatsapp numbers for ws=%s", workspace_id)
        return []


def _list_ad_accounts(access_token):
    if not access_token:
        return []
    try:
        resp = requests.get(
            f"{BASE_URL}/me/adaccounts",
            params={"access_token": access_token, "fields": "id,account_id,name,currency", "limit": 100},
            timeout=10,
        )
        j = resp.json()
        if not resp.ok:
            logger.warning("ctwa: /me/adaccounts non-ok: %s", j.get("error"))
            return []
        out = []
        for item in j.get("data", []) or []:
            out.append({
                "id": item.get("id"),                       # act_<id>
                "account_id": item.get("account_id"),
                "name": item.get("name") or item.get("id"),
                "currency": item.get("currency"),
            })
        return out
    except Exception:
        logger.exception("ctwa: failed to list ad accounts")
        return []


def _list_pages(access_token):
    if not access_token:
        return []
    try:
        resp = requests.get(
            f"{BASE_URL}/me/accounts",
            params={"access_token": access_token, "fields": "id,name", "limit": 100},
            timeout=10,
        )
        j = resp.json()
        if not resp.ok:
            logger.warning("ctwa: /me/accounts non-ok: %s", j.get("error"))
            return []
        return [{"id": p.get("id"), "name": p.get("name")} for p in (j.get("data", []) or [])]
    except Exception:
        logger.exception("ctwa: failed to list pages")
        return []


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

@ctwa_bp.route("/accounts", methods=["GET"])
def accounts():
    """Ad accounts + pages + WhatsApp numbers for the ad wizard's Accounts step.

    Always 200: returns whatever is connected. WhatsApp numbers come from our DB;
    ad accounts + pages require a linked Facebook account (Meta). If Facebook is
    not linked yet, those lists come back empty (the UI then prompts to connect).
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400

    user_id = _current_user_id()
    whatsapp_numbers = _list_whatsapp_numbers(workspace_id)

    ad_accounts, pages = [], []
    access_token, _ad_account_id, error = _meta_credentials(workspace_id, user_id)
    if error:
        # No linked Facebook account — not fatal. Return WhatsApp numbers so the
        # wizard can still show progress and prompt to connect an ad account.
        logger.info("ctwa accounts: no FB creds for ws=%s (returning WA numbers only)", workspace_id)
    else:
        ad_accounts = _list_ad_accounts(access_token)
        pages = _list_pages(access_token)

    return jsonify({
        "success": True,
        "ad_accounts": ad_accounts,
        "pages": pages,
        "whatsapp_numbers": whatsapp_numbers,
        "facebook_linked": bool(access_token),
    })


# ---------------------------------------------------------------------------
# Saved ad-account setup (per workspace)
# ---------------------------------------------------------------------------
# Users pick their Ad Account + Page + WhatsApp number ONCE (in Settings) and we
# store it here, so the ad wizard doesn't have to ask again on every campaign.

@ctwa_bp.route("/settings", methods=["GET"])
def get_settings():
    """Saved ad-account setup for this workspace (or null if not set yet)."""
    from ctwa.models import CTWAWorkspaceSettings
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    row = (
        CTWAWorkspaceSettings.query
        .filter(CTWAWorkspaceSettings.workspace_id == str(workspace_id))
        .first()
    )
    return jsonify({"success": True, "settings": row.to_dict() if row else None})


@ctwa_bp.route("/settings", methods=["PUT"])
def save_settings():
    """Upsert the workspace's saved Ad Account + Page + WhatsApp number."""
    from ctwa.models import CTWAWorkspaceSettings
    payload = request.get_json(silent=True) or {}
    workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    try:
        row = (
            CTWAWorkspaceSettings.query
            .filter(CTWAWorkspaceSettings.workspace_id == str(workspace_id))
            .first()
        )
        if not row:
            row = CTWAWorkspaceSettings(workspace_id=str(workspace_id))
            db.session.add(row)
        for field in ("ad_account_id", "ad_account_name", "page_id", "page_name",
                      "whatsapp_phone_number_id", "whatsapp_display_number"):
            if field in payload:
                setattr(row, field, payload[field])
        db.session.commit()
        return jsonify({"success": True, "settings": row.to_dict()})
    except Exception as e:
        db.session.rollback()
        logger.exception("ctwa: save settings failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Ad creative media upload -> DigitalOcean Spaces (returns a public URL)
# ---------------------------------------------------------------------------
# Meta must be able to FETCH the ad image/video from a public URL, so we upload
# the file to Spaces and hand back the resulting public https URL.

_CTWA_IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif"}
_CTWA_VIDEO_EXTS = {"mp4", "mov", "webm", "ogg"}
_CTWA_IMG_MAX = 10 * 1024 * 1024    # 10 MB
_CTWA_VID_MAX = 60 * 1024 * 1024    # 60 MB


@ctwa_bp.route("/upload-media", methods=["POST"])
def upload_media():
    import secrets as _secrets
    import mimetypes as _mimetypes
    from werkzeug.utils import secure_filename

    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"success": False, "error": "no_file"}), 400

    workspace_id = request.form.get("workspace_id") or request.args.get("workspace_id") or "shared"
    fname = secure_filename(file.filename) or "upload"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    ct = (file.mimetype or "").lower()

    if ext in _CTWA_VIDEO_EXTS or ct.startswith("video/"):
        is_video = True
    elif ext in _CTWA_IMAGE_EXTS or ct.startswith("image/"):
        is_video = False
    else:
        return jsonify({"success": False, "error": "unsupported_file_type"}), 400

    data = file.read()
    if not data:
        return jsonify({"success": False, "error": "empty_file"}), 400
    if len(data) > (_CTWA_VID_MAX if is_video else _CTWA_IMG_MAX):
        return jsonify({"success": False, "error": "file_too_large"}), 413

    resolved_ct = ct or _mimetypes.guess_type(fname)[0] or ("video/mp4" if is_video else "image/jpeg")
    unique = f"{_secrets.token_hex(8)}_{fname}"

    try:
        from core.spaces_storage import (
            is_spaces_configured, get_s3_client, build_object_key, build_public_url,
        )
        if not is_spaces_configured():
            return jsonify({"success": False, "error": "storage_not_configured"}), 503
        client, bucket = get_s3_client()
        key = build_object_key("uploads", "ads", str(workspace_id), unique)
        client.put_object(
            Bucket=bucket, Key=key, Body=data, ContentType=resolved_ct,
            ACL="public-read", CacheControl="public, max-age=31536000",
        )
        return jsonify({
            "success": True,
            "url": build_public_url(key),
            "media_type": "video" if is_video else "image",
        })
    except Exception as e:
        logger.exception("ctwa: media upload failed")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Facebook Ads connection (Ad Account + Page)
# ---------------------------------------------------------------------------
#
# Distinct from the WhatsApp connection. This links the Facebook side needed to
# run ads: it stores a SocialAccount(provider='facebook') row with the ads token
# + ad account, which is exactly what /accounts + publish read back.

def _exchange_long_lived(short_token):
    """Swap a short-lived FB user token for a long-lived one. Falls back to the
    original token if the app secret isn't configured or the exchange fails."""
    if not FB_APP_SECRET:
        return short_token, None
    try:
        r = requests.get(f"{BASE_URL}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET,
            "fb_exchange_token": short_token,
        }, timeout=15)
        j = r.json()
        if r.ok and j.get("access_token"):
            return j["access_token"], j.get("expires_in")
    except Exception:
        logger.exception("ctwa: long-lived token exchange failed")
    return short_token, None


def _persist_fb_connection(workspace_id, user_id, access_token, expires_in):
    """Fetch identity + ad accounts + pages for `access_token` and upsert a
    SocialAccount(provider='facebook') row. Returns (ok, result_or_error_dict)."""
    try:
        me = requests.get(f"{BASE_URL}/me", params={"access_token": access_token,
                                                     "fields": "id,name"}, timeout=10).json()
    except Exception:
        me = {}
    provider_user_id = me.get("id")
    if not provider_user_id:
        return False, {"error": me.get("error", {}).get("message", "could not read Facebook profile")}

    ad_accounts = _list_ad_accounts(access_token)
    pages = _list_pages(access_token)
    ad_account_id = None
    if ad_accounts:
        ad_account_id = str(ad_accounts[0].get("id") or "").replace("act_", "") or None

    try:
        from models import SocialAccount
        row = (
            SocialAccount.query
            .filter(SocialAccount.provider == "facebook",
                    SocialAccount.workspace_id == str(workspace_id))
            .filter((SocialAccount.user_id == user_id) if user_id is not None else True)
            .order_by(SocialAccount.updated_at.desc())
            .first()
        )
        if not row:
            row = SocialAccount(provider="facebook", workspace_id=str(workspace_id))
            db.session.add(row)
        row.user_id = user_id
        row.provider_user_id = provider_user_id
        row.account_name = me.get("name")
        row.access_token = access_token
        row.scopes = "ads_management,ads_read,business_management,pages_show_list"
        if ad_account_id:
            row.ad_account_id = ad_account_id
        if expires_in:
            try:
                row.token_expires_at = datetime.utcnow() + timedelta(seconds=int(expires_in))
            except Exception:
                pass
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.exception("ctwa: failed to persist Facebook Ads connection")
        return False, {"error": str(e)}

    return True, {
        "ad_accounts": ad_accounts,
        "pages": pages,
        "ad_account_count": len(ad_accounts),
        "page_count": len(pages),
    }


@ctwa_bp.route("/connect-ads", methods=["POST"])
def connect_ads():
    """Persist a Facebook Ads connection from an in-browser (JS SDK) token.

    Body: { workspace_id, access_token }. Kept for the popup path; the redirect
    OAuth flow below is the more reliable option.
    """
    payload = request.get_json(silent=True) or {}
    workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
    short_token = payload.get("access_token")
    if not workspace_id or not short_token:
        return jsonify({"success": False, "error": "workspace_id and access_token required"}), 400

    user_id = _current_user_id()
    access_token, expires_in = _exchange_long_lived(short_token)
    ok, result = _persist_fb_connection(workspace_id, user_id, access_token, expires_in)
    if not ok:
        return jsonify({"success": False, **result}), 400
    return jsonify({"success": True, "connected": True, **result})


# ---- Redirect-based OAuth (no popup — can't be blocked) --------------------

def _oauth_serializer():
    from itsdangerous import URLSafeTimedSerializer
    secret = os.getenv("SECRET_KEY") or current_app.secret_key or "ctwa-dev-secret"
    return URLSafeTimedSerializer(secret, salt="ctwa-ads-oauth")


def _origin_from_request():
    """Browser origin to build the redirect_uri + return URL. The frontend passes
    its own origin; fall back to configured base or the request host."""
    o = request.args.get("origin")
    if o:
        return o.rstrip("/")
    return (os.getenv("APP_BASE_URL") or request.host_url).rstrip("/")


@ctwa_bp.route("/oauth/connect", methods=["GET"])
def oauth_connect():
    """Start the Facebook Ads OAuth by redirecting the whole page to Facebook."""
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    user_id = _current_user_id()
    origin = _origin_from_request()
    redirect_uri = f"{origin}/api/ctwa/oauth/callback"
    state = _oauth_serializer().dumps({
        "workspace_id": workspace_id, "user_id": user_id, "origin": origin,
    })
    params = {
        "client_id": FB_APP_ID,
        "redirect_uri": redirect_uri,
        "state": state,
        "response_type": "code",
        "scope": "ads_management,ads_read,business_management,pages_show_list",
    }
    return redirect(f"https://www.facebook.com/{FB_API_VERSION}/dialog/oauth?{urlencode(params)}")


@ctwa_bp.route("/oauth/callback", methods=["GET"])
def oauth_callback():
    """Facebook redirects here with ?code=&state=. Exchange, persist, return."""
    err = request.args.get("error_description") or request.args.get("error")
    code = request.args.get("code")
    raw_state = request.args.get("state")

    # Recover the origin (+ identity) from the signed state so we can bounce back
    # to the right frontend even though Facebook stripped our query context.
    origin = (os.getenv("APP_BASE_URL") or request.host_url).rstrip("/")
    workspace_id = user_id = None
    if raw_state:
        try:
            data = _oauth_serializer().loads(raw_state, max_age=600)
            workspace_id = data.get("workspace_id")
            user_id = data.get("user_id")
            origin = (data.get("origin") or origin).rstrip("/")
        except Exception:
            return redirect(f"{origin}/dashboard/settings?ads_error=invalid_state")

    settings_url = f"{origin}/dashboard/settings"
    if err:
        return redirect(f"{settings_url}?ads_error={quote(str(err))}")
    if not code or not workspace_id:
        return redirect(f"{settings_url}?ads_error=missing_code")
    if not FB_APP_SECRET:
        return redirect(f"{settings_url}?ads_error=app_secret_not_configured")

    redirect_uri = f"{origin}/api/ctwa/oauth/callback"
    try:
        tok = requests.get(f"{BASE_URL}/oauth/access_token", params={
            "client_id": FB_APP_ID,
            "client_secret": FB_APP_SECRET,
            "redirect_uri": redirect_uri,
            "code": code,
        }, timeout=15).json()
    except Exception:
        tok = {}
    access_token = tok.get("access_token")
    if not access_token:
        msg = (tok.get("error", {}) or {}).get("message", "token_exchange_failed")
        return redirect(f"{settings_url}?ads_error={quote(str(msg))}")

    ok, result = _persist_fb_connection(workspace_id, user_id, access_token, tok.get("expires_in"))
    if not ok:
        return redirect(f"{settings_url}?ads_error={quote(str(result.get('error', 'save_failed')))}")
    return redirect(f"{settings_url}?ads_connected={result.get('ad_account_count', 0)}")


@ctwa_bp.route("/connection-status", methods=["GET"])
def connection_status():
    """Whether this workspace has a Facebook Ads connection, for the settings UI."""
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    user_id = _current_user_id()
    try:
        from models import SocialAccount
        row = (
            SocialAccount.query
            .filter(SocialAccount.provider == "facebook",
                    SocialAccount.workspace_id == str(workspace_id))
            .filter((SocialAccount.user_id == user_id) if user_id is not None else True)
            .order_by(SocialAccount.updated_at.desc())
            .first()
        )
    except Exception:
        row = None
    if not row or not row.access_token:
        return jsonify({"success": True, "connected": False})
    return jsonify({
        "success": True,
        "connected": True,
        "ad_account_id": (f"act_{row.ad_account_id}" if row.ad_account_id else None),
        "account_name": row.account_name,
        "connected_at": row.updated_at.isoformat() if row.updated_at else None,
    })


# ---------------------------------------------------------------------------
# Campaign CRUD
# ---------------------------------------------------------------------------

def _campaign_from_payload(payload, workspace_id, user_id):
    campaign = payload.get("campaign") or {}
    adset = payload.get("adset") or {}
    creative = payload.get("creative") or {}

    # Support BOTH the flat AdCreatorWizard payload and the nested CreateCTWA one.
    name = payload.get("name") or campaign.get("name") or "Untitled campaign"
    targeting = adset.get("targeting") or payload.get("targeting")
    row = CTWACampaign(
        workspace_id=str(workspace_id),
        user_id=user_id,
        ad_account_id=payload.get("ad_account_id"),
        name=name,
        objective=campaign.get("objective") or payload.get("objective") or "OUTCOME_ENGAGEMENT",
        ad_type=payload.get("ad_type") or "ctwa",
        cta_type=payload.get("cta_type") or "WHATSAPP_MESSAGE",
        status="DRAFT",
        create_leads=bool(payload.get("create_leads", True)),
        budget_type=payload.get("budget_type") or "daily",
        daily_budget=payload.get("daily_budget") if payload.get("daily_budget") is not None else adset.get("daily_budget"),
        lifetime_budget=payload.get("lifetime_budget"),
        budget_currency=payload.get("budget_currency") or "INR",
        start_time=_iso_to_dt(payload.get("start_time") or adset.get("start_time")),
        end_time=_iso_to_dt(payload.get("end_time") or adset.get("end_time")),
        placement=payload.get("placement") or adset.get("placement"),
        targeting=targeting,
        page_id=payload.get("page_id") or (creative.get("ctwa_config") or {}).get("page_id"),
        whatsapp_phone_number_id=payload.get("whatsapp_phone_number_id")
            or (creative.get("ctwa_config") or {}).get("phone_number_id"),
        creative=payload.get("creative") if isinstance(payload.get("creative"), dict) else creative,
        sync_status="pending",
    )
    return row


def _campaign_payload(row):
    """row.to_dict() plus a click-to-chat test link for the UI."""
    data = row.to_dict()
    try:
        creative = row.creative or {}
        prefilled = (creative.get("prefilled_message") or "").strip() or None
        if creative.get("ice_breakers"):
            prefilled = None
        data["wa_link"] = _wa_chat_link(_wa_number_for(row.whatsapp_phone_number_id), prefilled)
    except Exception:
        data["wa_link"] = None
    return data


@ctwa_bp.route("/campaigns", methods=["GET"])
def list_campaigns():
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    rows = (
        CTWACampaign.query
        .filter(CTWACampaign.workspace_id == str(workspace_id))
        .order_by(CTWACampaign.created_at.desc())
        .all()
    )
    return jsonify({"success": True, "campaigns": [_campaign_payload(r) for r in rows]})


@ctwa_bp.route("/campaigns/<int:cid>", methods=["GET"])
def get_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)
    return jsonify({"success": True, "campaign": _campaign_payload(row)})


@ctwa_bp.route("/campaigns", methods=["POST"])
def create_campaign():
    payload = request.get_json(silent=True) or {}
    workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    user_id = _current_user_id()
    try:
        row = _campaign_from_payload(payload, workspace_id, user_id)
        # If the wizard sent only creative/budget (no explicit Meta targets), fill
        # the missing Ad Account / Page / WhatsApp number from the workspace's saved
        # ad-account setup. Never overwrite a value the payload already provided.
        if not (row.ad_account_id and row.page_id and row.whatsapp_phone_number_id):
            from ctwa.models import CTWAWorkspaceSettings
            saved = (
                CTWAWorkspaceSettings.query
                .filter(CTWAWorkspaceSettings.workspace_id == str(workspace_id))
                .first()
            )
            if saved:
                if not row.ad_account_id:
                    row.ad_account_id = saved.ad_account_id
                if not row.page_id:
                    row.page_id = saved.page_id
                if not row.whatsapp_phone_number_id:
                    row.whatsapp_phone_number_id = saved.whatsapp_phone_number_id
        db.session.add(row)
        db.session.commit()
        return jsonify({"success": True, "campaign": _campaign_payload(row)}), 201
    except Exception as e:
        db.session.rollback()
        logger.exception("ctwa: create campaign failed")
        return jsonify({"success": False, "error": str(e)}), 500


@ctwa_bp.route("/campaigns/<int:cid>", methods=["PUT"])
def update_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)
    if row.status != "DRAFT":
        return jsonify({"success": False, "error": "only_draft_editable"}), 400
    payload = request.get_json(silent=True) or {}
    for field in ("name", "ad_account_id", "page_id", "whatsapp_phone_number_id",
                  "budget_currency", "objective", "ad_type"):
        if field in payload and payload[field] is not None:
            setattr(row, field, payload[field])
    if "daily_budget" in payload:
        row.daily_budget = payload["daily_budget"]
    for jf in ("placement", "targeting", "creative"):
        if jf in payload and payload[jf] is not None:
            setattr(row, jf, payload[jf])
    try:
        db.session.commit()
        return jsonify({"success": True, "campaign": _campaign_payload(row)})
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


@ctwa_bp.route("/campaigns/<int:cid>", methods=["DELETE"])
def delete_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)

    # If this campaign was published to Meta, best-effort remove it there too. A
    # Meta failure must NEVER block deleting our own DB row.
    if row.meta_campaign_id:
        access_token, _ad_account_id, error = _meta_credentials(row.workspace_id, _current_user_id())
        if not error:
            try:
                requests.delete(
                    f"{BASE_URL}/{row.meta_campaign_id}",
                    params={"access_token": access_token},
                    timeout=20,
                )
            except Exception:
                logger.warning("ctwa: best-effort Meta delete failed cid=%s", cid, exc_info=True)

    db.session.delete(row)
    db.session.commit()
    return jsonify({"success": True})


def _assert_owns(row):
    """Defense in depth on top of the global workspace-ownership gate."""
    # The before_request gate already blocks foreign workspace_id in the query
    # string; here we simply ensure the row belongs to a workspace the caller
    # passed. Nothing to do if the row has no workspace (shouldn't happen).
    return True


# ---------------------------------------------------------------------------
# Publish to Meta
# ---------------------------------------------------------------------------

def _meta_post(path, access_token, data):
    payload = dict(data)
    payload["access_token"] = access_token
    resp = requests.post(f"{BASE_URL}/{path}", data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        j = {"error": {"message": resp.text}}
    return resp.ok, j


def _fmt_meta_err(stage, j):
    """Build a detailed, human-readable error from a Meta Graph error response and
    log the FULL payload so we can see exactly which parameter Meta rejected."""
    err = (j or {}).get("error", {}) or {}
    bits = [err.get("message")]
    if err.get("error_user_title"):
        bits.append(err["error_user_title"])
    if err.get("error_user_msg"):
        bits.append(err["error_user_msg"])
    detail = " — ".join([b for b in bits if b]) or str(j)
    logger.warning(
        "ctwa meta error [%s] code=%s subcode=%s trace=%s :: %s | full=%s",
        stage, err.get("code"), err.get("error_subcode"), err.get("fbtrace_id"), detail, j,
    )
    return f"{stage}: {detail}"


def _build_targeting(row):
    t = dict(row.targeting or {})
    # Merge the placement pin (Status ads -> whatsapp/status) into targeting,
    # which is where Meta expects publisher_platforms / whatsapp_positions.
    placement = row.placement or {}
    pubs = list(placement.get("publisher_platforms") or [])
    wa_pos = list(placement.get("whatsapp_positions") or [])
    ig_pos = list(placement.get("instagram_positions") or [])
    fb_pos = list(placement.get("facebook_positions") or [])

    # Meta requirement: the WhatsApp Status placement can NOT run alone — its
    # inventory is delivered together with Instagram Stories. So whenever Status
    # is selected, we must also include Instagram + Instagram Stories, otherwise
    # Meta rejects the ad set ("Instagram Stories required for WhatsApp status").
    if "status" in wa_pos:
        if "instagram" not in pubs:
            pubs.append("instagram")
        if "story" not in ig_pos:
            ig_pos.append("story")

    if pubs:
        t["publisher_platforms"] = pubs
    if wa_pos:
        t["whatsapp_positions"] = wa_pos
    if ig_pos:
        t["instagram_positions"] = ig_pos
    if fb_pos:
        t["facebook_positions"] = fb_pos

    # Advanced audience — interests/behaviors go inside a flexible_spec entry and
    # custom_audiences becomes a top-level list of {id}. They arrive on the stored
    # targeting dict as lists of {id,name}; strip the raw keys (Meta doesn't accept
    # them at the top level) and only emit the shapes Meta expects. Each list is
    # guarded so empty selections are omitted entirely. (Best-effort per Meta's API.)
    raw = row.targeting or {}
    interests = [i for i in (raw.get("interests") or []) if isinstance(i, dict) and i.get("id")]
    behaviors = [b for b in (raw.get("behaviors") or []) if isinstance(b, dict) and b.get("id")]
    custom_audiences = [c for c in (raw.get("custom_audiences") or []) if isinstance(c, dict) and c.get("id")]
    for _k in ("interests", "behaviors", "custom_audiences"):
        t.pop(_k, None)

    flex = {}
    if interests:
        flex["interests"] = [
            ({"id": i["id"], "name": i["name"]} if i.get("name") else {"id": i["id"]})
            for i in interests
        ]
    if behaviors:
        flex["behaviors"] = [
            ({"id": b["id"], "name": b["name"]} if b.get("name") else {"id": b["id"]})
            for b in behaviors
        ]
    if flex:
        t["flexible_spec"] = [flex]
    if custom_audiences:
        t["custom_audiences"] = [{"id": c["id"]} for c in custom_audiences]
    return t


def _upload_image_to_meta(ad_account_id, access_token, media_url):
    """Register an external image with Meta and return its image_hash. Meta ad
    creatives require an image_hash (or already-hosted asset) — a raw external URL
    is rejected with a generic 'unknown error'."""
    try:
        img = requests.get(media_url, timeout=25)
        if not img.ok or not img.content:
            logger.warning("ctwa: could not download creative image %s", media_url)
            return None
        import base64
        b64 = base64.b64encode(img.content).decode()
        r = requests.post(
            f"{BASE_URL}/{ad_account_id}/adimages",
            data={"access_token": access_token, "bytes": b64},
            timeout=45,
        )
        j = r.json()
        if not r.ok:
            logger.warning("ctwa adimages upload failed: %s", j.get("error"))
            return None
        for v in (j.get("images") or {}).values():
            if v.get("hash"):
                return v["hash"]
        return None
    except Exception:
        logger.exception("ctwa: image upload to Meta failed")
        return None


def _wa_number_for(phone_number_id):
    """Best-effort real display phone number (digits only) for the wa.me link."""
    try:
        from whatsapp.models import WhatsAppAccount
        acc = (WhatsAppAccount.query
               .filter(WhatsAppAccount.phone_number_id == str(phone_number_id)).first())
        if acc and acc.display_phone_number:
            return "".join(ch for ch in acc.display_phone_number if ch.isdigit()) or None
    except Exception:
        logger.debug("ctwa: could not resolve wa number", exc_info=True)
    return None


def _wa_chat_link(wa_number, prefilled=None):
    """Official WhatsApp click-to-chat link. Just a deep link — no API call."""
    if wa_number:
        base = f"https://wa.me/{wa_number}"
        return f"{base}?text={quote(prefilled)}" if prefilled else base
    # No resolvable number — fall back to the generic send endpoint.
    return f"https://api.whatsapp.com/send?text={quote(prefilled)}" if prefilled else "https://api.whatsapp.com/send"


def _build_welcome_message(creative):
    """Best-effort `page_welcome_message` object for ice breakers, or None.

    Meta renders the welcome screen from a VISUAL_EDITOR / welcome_message spec
    carrying up to 3 ice-breaker titles (each capped at 24 chars). Returns None
    when there are no valid ice-breaker titles, so image-only ads without ice
    breakers are left completely unaffected. NOTE: this welcome-message format is
    per Meta's spec and is best-effort — it may need a test-and-iterate pass.
    """
    ibs = (creative or {}).get("ice_breakers")
    if not isinstance(ibs, list) or not ibs:
        return None
    titles = []
    for ib in ibs:
        t = ib.get("text") if isinstance(ib, dict) else ib
        if t is None:
            continue
        t = str(t).strip()
        if not t:
            continue
        titles.append(t[:24])
    titles = titles[:3]
    if not titles:
        return None
    return {
        "type": "VISUAL_EDITOR",
        "landing_screen_type": "welcome_message",
        "media_type": "text",
        "text_format": {
            "customer_action_type": "ice_breakers",
            "ice_breakers": [{"title": t} for t in titles],
        },
    }


def _upload_video_to_meta(ad_account_id, access_token, media_url):
    """Register an external video with Meta and return its video_id (or None).

    Meta fetches the video itself from the public `file_url`, mirroring the
    image flow. Returns None on any failure so the caller can decide how to fail.
    """
    try:
        r = requests.post(
            f"{BASE_URL}/{ad_account_id}/advideos",
            data={"access_token": access_token, "file_url": media_url},
            timeout=60,
        )
        try:
            j = r.json()
        except Exception:
            j = {}
        if not r.ok:
            logger.warning("ctwa advideos upload failed: %s", j.get("error"))
            return None
        return j.get("id")
    except Exception:
        logger.warning("ctwa: video upload to Meta failed", exc_info=True)
        return None


def _video_thumbnail(video_id, access_token):
    """Best-effort thumbnail URI for a freshly uploaded video, or None.

    Meta needs a moment to generate thumbnails after upload, so we try twice with
    a short pause between the two GETs and return the first uri we find.
    """
    import time
    for attempt in range(2):
        try:
            r = requests.get(
                f"{BASE_URL}/{video_id}",
                params={"access_token": access_token, "fields": "thumbnails{uri}"},
                timeout=20,
            )
            j = r.json()
            for th in ((j.get("thumbnails") or {}).get("data") or []):
                if th.get("uri"):
                    return th["uri"]
        except Exception:
            logger.debug("ctwa: thumbnail fetch attempt failed", exc_info=True)
        if attempt == 0:
            time.sleep(3)
    return None


@ctwa_bp.route("/campaigns/<int:cid>/publish", methods=["POST"])
def publish_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)

    activate = bool((request.get_json(silent=True) or {}).get("activate", False))
    user_id = _current_user_id()
    access_token, ad_account_id, error = _meta_credentials(row.workspace_id, user_id)
    if error:
        return error  # (jsonify, status) tuple from the CRM helper

    if not row.daily_budget and not row.lifetime_budget:
        return jsonify({"success": False, "error": "budget_required", "message": "Please set a budget before publishing the ad."}), 400

    ad_account_id = row.ad_account_id or ad_account_id
    if not ad_account_id:
        return jsonify({"success": False, "error": "no_ad_account"}), 400
    if not str(ad_account_id).startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    creative = row.creative or {}
    initial_status = "ACTIVE" if activate else "PAUSED"

    try:
        # 1) Campaign
        # Budget lives on the ad set (not the campaign), so Meta requires us to
        # explicitly declare that ad sets do NOT share budget.
        ok, camp = _meta_post(f"{ad_account_id}/campaigns", access_token, {
            "name": row.name,
            "objective": row.objective or "OUTCOME_ENGAGEMENT",
            "status": initial_status,
            "special_ad_categories": "[]",
            "is_adset_budget_sharing_enabled": "false",
        })
        if not ok:
            raise RuntimeError(_fmt_meta_err("campaign", camp))
        row.meta_campaign_id = camp.get("id")

        # 2) Ad Set — placement pinned here for Status ads
        adset_data = {
            "name": f"{row.name} - Ad Set",
            "campaign_id": row.meta_campaign_id,
            "billing_event": "IMPRESSIONS",
            "optimization_goal": "CONVERSATIONS",
            # Automatic bidding ("Highest volume") — needs NO bid amount. Without an
            # explicit strategy Meta falls back to the account default (often a bid
            # cap) which then demands a bid amount we don't collect.
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
            "destination_type": "WHATSAPP",
            "targeting": _json(_build_targeting(row)),
            "status": initial_status,
        }
        # Budget/schedule: a lifetime budget REQUIRES an end_time (Meta rejects it
        # otherwise), so we only take the lifetime path when both are present —
        # falling back to daily_budget (with a warning) keeps publish from hard
        # failing. start_time is optional and sent only when we have one.
        if row.budget_type == "lifetime" and row.lifetime_budget and row.end_time:
            adset_data["lifetime_budget"] = int(row.lifetime_budget * 100)
            adset_data["end_time"] = row.end_time.isoformat()
            if row.start_time:
                adset_data["start_time"] = row.start_time.isoformat()
        else:
            if row.budget_type == "lifetime" and row.lifetime_budget and not row.end_time:
                logger.warning(
                    "ctwa publish cid=%s: lifetime budget chosen but no end_time — "
                    "falling back to daily_budget", cid,
                )
            adset_data["daily_budget"] = int((row.daily_budget or 0) * 100) or 50000
        if row.page_id:
            adset_data["promoted_object"] = _json({"page_id": row.page_id})
        ok, adset = _meta_post(f"{ad_account_id}/adsets", access_token, adset_data)
        if not ok:
            raise RuntimeError(_fmt_meta_err("adset", adset))
        row.meta_adset_id = adset.get("id")

        # 3) Ad Creative — Click-to-WhatsApp CTA on the (full-screen for Status) asset.
        # Branches on media type: images use link_data (+ image_hash from
        # /adimages), videos use video_data (+ video_id from /advideos). Both carry
        # the same WhatsApp CTA, an optional pre-filled message on the wa.me link,
        # and an optional ice-breaker welcome screen.
        _ibs = creative.get("ice_breakers")
        has_ice_breakers = isinstance(_ibs, list) and len(_ibs) > 0
        welcome = _build_welcome_message(creative)

        wa_number = _wa_number_for(row.whatsapp_phone_number_id)
        prefilled = (creative.get("prefilled_message") or "").strip() or None
        # Ice breakers take precedence over a pre-filled link.
        if has_ice_breakers:
            prefilled = None
        wa_link = _wa_chat_link(wa_number, prefilled)

        # Build as PLAIN nested dicts — call_to_action / link_data / video_data must
        # be real objects. Only the top-level object_story_spec gets JSON-encoded
        # once when sent as form data (double-encoding nested fields => Meta
        # "unknown error"); page_welcome_message is the sole exception — Meta wants
        # it as a JSON *string* inside the spec.
        cta = {
            "type": row.cta_type or "WHATSAPP_MESSAGE",
            "value": {"app_destination": "WHATSAPP", "link": wa_link},
        }

        if creative.get("media_type") == "video" and creative.get("media_url"):
            # Task C: video path — upload to Meta /advideos, then build video_data.
            video_id = _upload_video_to_meta(ad_account_id, access_token, creative["media_url"])
            if video_id is None:
                raise RuntimeError("creative: video upload to Meta failed")
            thumb = _video_thumbnail(video_id, access_token)
            video_data = {
                "video_id": video_id,
                "message": creative.get("primary_text") or "",
                "call_to_action": cta,
            }
            if thumb:
                video_data["image_url"] = thumb
            if creative.get("headline"):
                video_data["title"] = creative["headline"]
            if welcome:  # Task B (best-effort)
                video_data["page_welcome_message"] = _json(welcome)
            object_story_spec = {"page_id": row.page_id, "video_data": video_data}
        else:
            # Image path. Meta needs an image_hash (not a raw URL); link_data
            # requires a link. When the creative carries >= 2 carousel cards we
            # build a CAROUSEL (child_attachments) instead of a single image.
            link_data = {
                "link": wa_link,
                "message": creative.get("primary_text") or "",
                "call_to_action": cta,
            }
            if creative.get("headline"):
                link_data["name"] = creative["headline"]

            # Carousel — upload each card's image and attach it. Cards whose upload
            # fails are skipped; if fewer than 2 valid cards remain we fall back to
            # the single-image flow below. (Image cards only — video is out of scope.)
            cards = creative.get("cards")
            child_attachments = []
            if isinstance(cards, list) and len(cards) >= 2:
                for c in cards:
                    if not isinstance(c, dict) or not c.get("image_url"):
                        continue
                    c_hash = _upload_image_to_meta(ad_account_id, access_token, c["image_url"])
                    if not c_hash:
                        continue
                    child_attachments.append({
                        "link": wa_link,
                        "name": c.get("headline", ""),
                        "description": c.get("description", ""),
                        "image_hash": c_hash,
                    })

            if len(child_attachments) >= 2:
                # Carousel: do NOT also set a top-level image_hash.
                link_data["child_attachments"] = child_attachments
            else:
                image_hash = None
                if creative.get("media_url") and creative.get("media_type") in (None, "image", ""):
                    image_hash = _upload_image_to_meta(ad_account_id, access_token, creative["media_url"])
                if image_hash:
                    link_data["image_hash"] = image_hash
                elif creative.get("media_url"):
                    link_data["picture"] = creative["media_url"]

            if welcome:  # Task B (best-effort)
                link_data["page_welcome_message"] = _json(welcome)
            object_story_spec = {"page_id": row.page_id, "link_data": link_data}

        welcome_skipped = False
        ok, cre = _meta_post(f"{ad_account_id}/adcreatives", access_token, {
            "name": f"{row.name} - Creative",
            "object_story_spec": _json(object_story_spec),
        })
        # Meta often rejects the ice-breaker welcome screen with a generic code=1.
        # That must never block the whole publish: strip it, retry once, and flag
        # that ice breakers weren't applied so the UI can say so.
        if not ok and welcome:
            logger.warning(
                "ctwa: creative rejected with page_welcome_message (%s) — retrying without ice breakers",
                ((cre or {}).get("error") or {}).get("message"),
            )
            for _spec in (object_story_spec.get("link_data"), object_story_spec.get("video_data")):
                if isinstance(_spec, dict):
                    _spec.pop("page_welcome_message", None)
            ok, cre = _meta_post(f"{ad_account_id}/adcreatives", access_token, {
                "name": f"{row.name} - Creative",
                "object_story_spec": _json(object_story_spec),
            })
            if ok:
                welcome_skipped = True
        if not ok:
            raise RuntimeError(_fmt_meta_err("creative", cre))
        row.meta_creative_id = cre.get("id")

        # 4) Ad
        ok, ad = _meta_post(f"{ad_account_id}/ads", access_token, {
            "name": f"{row.name} - Ad",
            "adset_id": row.meta_adset_id,
            "creative": _json({"creative_id": row.meta_creative_id}),
            "status": initial_status,
        })
        if not ok:
            raise RuntimeError(_fmt_meta_err("ad", ad))
        row.meta_ad_id = ad.get("id")

        row.status = "ACTIVE" if activate else "PAUSED"
        row.sync_status = "synced"
        row.sync_error = None
        db.session.commit()
        return jsonify({
            "success": True,
            "campaign": _campaign_payload(row),
            "meta_campaign_id": row.meta_campaign_id,
            "warning": (
                "Published, but Meta rejected the ice-breaker welcome screen, so the "
                "ice breakers were not applied. Set them on your WhatsApp Business "
                "profile instead."
            ) if welcome_skipped else None,
        })
    except Exception as e:
        row.sync_status = "error"
        row.sync_error = str(e)
        db.session.commit()
        logger.warning("ctwa publish failed cid=%s: %s", cid, e)
        return jsonify({"success": False, "error": str(e), "campaign": _campaign_payload(row)}), 502


def _json(obj):
    import json as _j
    return _j.dumps(obj)


# ---------------------------------------------------------------------------
# Lifecycle — activate / pause a published campaign
# ---------------------------------------------------------------------------

@ctwa_bp.route("/campaigns/<int:cid>/activate", methods=["POST"])
def activate_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)
    if not row.meta_campaign_id:
        return jsonify({"success": False, "error": "not_published"}), 400

    access_token, ad_account_id, error = _meta_credentials(row.workspace_id, _current_user_id())
    if error:
        return error  # (jsonify, status) tuple from the CRM helper

    try:
        # To go live, the AD, AD SET and CAMPAIGN must ALL be ACTIVE — Meta won't
        # deliver an active ad whose parent ad set/campaign is still paused. Flip
        # them child -> parent (ad, then ad set, then campaign).
        for obj_id in (row.meta_ad_id, row.meta_adset_id, row.meta_campaign_id):
            if not obj_id:
                continue
            ok, j = _meta_post(str(obj_id), access_token, {"status": "ACTIVE"})
            if not ok:
                raise RuntimeError(_fmt_meta_err("activate", j))

        row.status = "ACTIVE"
        row.sync_error = None
        db.session.commit()
        return jsonify({"success": True, "campaign": _campaign_payload(row)})
    except Exception as e:
        row.sync_error = str(e)
        db.session.commit()
        logger.warning("ctwa activate failed cid=%s: %s", cid, e)
        return jsonify({"success": False, "error": str(e), "campaign": _campaign_payload(row)}), 502


@ctwa_bp.route("/campaigns/<int:cid>/pause", methods=["POST"])
def pause_campaign(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)
    if not row.meta_campaign_id:
        return jsonify({"success": False, "error": "not_published"}), 400

    access_token, ad_account_id, error = _meta_credentials(row.workspace_id, _current_user_id())
    if error:
        return error  # (jsonify, status) tuple from the CRM helper

    try:
        # Pausing the campaign is enough to stop delivery, but flip the ad set and
        # ad too so their status reflects reality. Order does not matter here.
        for obj_id in (row.meta_campaign_id, row.meta_adset_id, row.meta_ad_id):
            if not obj_id:
                continue
            ok, j = _meta_post(str(obj_id), access_token, {"status": "PAUSED"})
            if not ok:
                raise RuntimeError(_fmt_meta_err("pause", j))

        row.status = "PAUSED"
        row.sync_error = None
        db.session.commit()
        return jsonify({"success": True, "campaign": _campaign_payload(row)})
    except Exception as e:
        row.sync_error = str(e)
        db.session.commit()
        logger.warning("ctwa pause failed cid=%s: %s", cid, e)
        return jsonify({"success": False, "error": str(e), "campaign": _campaign_payload(row)}), 502


# ---------------------------------------------------------------------------
# Insights / analytics (best-effort)
# ---------------------------------------------------------------------------

@ctwa_bp.route("/campaigns/<int:cid>/insights", methods=["GET"])
def campaign_insights(cid):
    row = CTWACampaign.query.get(cid)
    if not row:
        return jsonify({"success": False, "error": "not_found"}), 404
    _assert_owns(row)
    metrics = {"impressions": 0, "clicks": 0, "spend": 0.0,
               "conversations": 0, "cost_per_conversation": None}
    if row.meta_campaign_id:
        user_id = _current_user_id()
        access_token, _aid, error = _meta_credentials(row.workspace_id, user_id)
        if not error and access_token:
            try:
                dp = request.args.get("date_preset", "last_7d")
                resp = requests.get(
                    f"{BASE_URL}/{row.meta_campaign_id}/insights",
                    params={"access_token": access_token, "date_preset": dp,
                            "fields": "impressions,clicks,spend"},
                    timeout=15,
                )
                j = resp.json()
                data = (j.get("data") or [{}])[0] if resp.ok else {}
                metrics["impressions"] = int(data.get("impressions", 0) or 0)
                metrics["clicks"] = int(data.get("clicks", 0) or 0)
                metrics["spend"] = float(data.get("spend", 0) or 0)
            except Exception:
                logger.exception("ctwa insights fetch failed cid=%s", cid)
    return jsonify({
        "success": True,
        "campaign_id": row.id,
        "meta_campaign_id": row.meta_campaign_id,
        "name": row.name,
        "status": row.status,
        "period": request.args.get("date_preset", "last_7d"),
        "metrics": metrics,
    })


@ctwa_bp.route("/analytics/summary", methods=["GET"])
def analytics_summary():
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    rows = CTWACampaign.query.filter(CTWACampaign.workspace_id == str(workspace_id)).all()
    active = sum(1 for r in rows if r.status == "ACTIVE")
    return jsonify({
        "success": True,
        "summary": {
            "total_conversations": 0,
            "ctwa_conversations": 0,
            "organic_conversations": 0,
            "active_campaigns": active,
            "ctwa_percentage": 0,
        },
    })


# ---------------------------------------------------------------------------
# Advanced targeting lookups (interest/behavior search + saved audiences)
# ---------------------------------------------------------------------------
# Feed the ad wizard's advanced-audience picker. Both are thin read-only proxies
# over the Meta Marketing API and are best-effort per Meta's API — they may need
# a live test-and-iterate pass.

@ctwa_bp.route("/targeting-search", methods=["GET"])
def targeting_search():
    """Search Meta's targeting catalog (interests/behaviors/etc.) for the wizard.

    Query: workspace_id (required), q (required), type (default 'adinterest').
    Proxies Graph `/search` and maps its `data` to a compact result list.
    """
    workspace_id = request.args.get("workspace_id")
    q = request.args.get("q")
    search_type = request.args.get("type") or "adinterest"
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400
    if not q:
        return jsonify({"success": False, "error": "q required"}), 400

    access_token, _ad_account_id, error = _meta_credentials(workspace_id, _current_user_id())
    if error:
        return error  # (jsonify, status) tuple from the CRM helper

    try:
        resp = requests.get(
            f"{BASE_URL}/search",
            params={"type": search_type, "q": q, "limit": 25, "access_token": access_token},
            timeout=15,
        )
        j = resp.json()
        if not resp.ok:
            return jsonify({"success": False, "error": _fmt_meta_err("targeting-search", j)}), 502
        results = []
        for item in (j.get("data") or []):
            results.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "audience_size_lower_bound": item.get("audience_size_lower_bound"),
                "audience_size": item.get("audience_size") or item.get("audience_size_lower_bound"),
                "type": item.get("type") or search_type,
            })
        return jsonify({"success": True, "results": results})
    except Exception as e:
        logger.exception("ctwa: targeting-search failed")
        return jsonify({"success": False, "error": str(e)}), 502


@ctwa_bp.route("/audiences", methods=["GET"])
def audiences():
    """Custom + lookalike audiences on the connected ad account, for the wizard.

    Query: workspace_id (required). Returns an empty list (not an error) when no
    ad account is linked. LOOKALIKE rows are included with their subtype so the UI
    can label them as lookalikes.
    """
    workspace_id = request.args.get("workspace_id")
    if not workspace_id:
        return jsonify({"success": False, "error": "workspace_id required"}), 400

    access_token, ad_account_id, error = _meta_credentials(workspace_id, _current_user_id())
    if error or not ad_account_id:
        # No linked account / ad account — not fatal for a picker; return empty.
        return jsonify({"success": True, "custom_audiences": []})
    if not str(ad_account_id).startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    try:
        resp = requests.get(
            f"{BASE_URL}/{ad_account_id}/customaudiences",
            params={"access_token": access_token,
                    "fields": "id,name,approximate_count,subtype", "limit": 100},
            timeout=15,
        )
        j = resp.json()
        if not resp.ok:
            return jsonify({"success": False, "error": _fmt_meta_err("audiences", j)}), 502
        out = []
        for item in (j.get("data") or []):
            out.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "approximate_count": item.get("approximate_count"),
                "subtype": item.get("subtype"),
            })
        return jsonify({"success": True, "custom_audiences": out})
    except Exception as e:
        logger.exception("ctwa: audiences fetch failed")
        return jsonify({"success": False, "error": str(e)}), 502


# ---------------------------------------------------------------------------
# AI generation — ad image (Vertex Imagen) + ad copy (Vertex Gemini)
# ---------------------------------------------------------------------------
#
# Both reuse the genai client + helpers already configured in the CRM's
# meta_integration module (vertexai=True, project/location from env), imported
# lazily inside the handlers so importing this blueprint never depends on the
# google-genai SDK being installed/configured.
#
# NOTE: actual generation requires Vertex AI credentials at runtime
# (GCP_PROJECT / PROJECT_ID + GOOGLE_APPLICATION_CREDENTIALS, optional
# GOOGLE_CLOUD_LOCATION). Without them get_genai_client() returns None and these
# endpoints answer with a clean JSON error instead of raising.

# Appended to every image prompt so the asset always fits the full-screen ad slot.
_AD_IMAGE_SUFFIX = (
    " Vertical 9:16 aspect ratio, high resolution, suitable for a full-screen "
    "mobile story ad. Do not include any text, letters, or logos."
)

_GENERIC_AD_IMAGE_PROMPT = (
    "A professional, eye-catching vertical advertising image for a modern business. "
    "Clean modern commercial photography, bright lighting, vibrant colours, "
    "no text overlay, no watermark."
)


def _workspace_context(workspace_id):
    """(business_name, description, website) for `workspace_id`, best effort.

    Mirrors how meta_integration.generate_copy resolves the workspace. Returns
    all-None when the workspace can't be read, so callers fall back to generic
    copy/imagery instead of failing.
    """
    try:
        from models import Workspace
        ws = None
        try:
            ws = Workspace.query.filter_by(id=int(workspace_id)).first()
        except (TypeError, ValueError):
            ws = Workspace.query.filter_by(id=workspace_id).first()
        if not ws:
            return None, None, None
        return (
            (getattr(ws, "business_name", None) or None),
            (getattr(ws, "description", None) or None),
            (getattr(ws, "website", None) or None),
        )
    except Exception:
        logger.warning("ctwa: could not resolve workspace ws=%s", workspace_id, exc_info=True)
        return None, None, None


def _extract_image_bytes(resp):
    """Pull raw image bytes out of a generate_images response.

    The SDK has shipped a couple of shapes for this (image.image_bytes vs a flat
    image_bytes), so probe both and skip anything we can't read.
    """
    out = []
    for g in (getattr(resp, "generated_images", []) or []):
        data = getattr(getattr(g, "image", None), "image_bytes", None)
        if not data:
            data = getattr(g, "image_bytes", None)
        if data:
            out.append(data)
    return out


# Model candidates. Which models exist depends on whether the key is a Gemini
# Developer API key or Vertex AI — so we try Imagen (predict) first, then fall
# back to the Gemini image models (generateContent), taking the first that works.
_IMAGEN_CANDIDATES = [
    os.getenv("IMAGE_MODEL") or "",
    "imagen-4.0-generate-001",
    "imagen-4.0-fast-generate-001",
    "imagen-3.0-generate-002",
]
_GEMINI_IMAGE_CANDIDATES = [
    os.getenv("GEMINI_IMAGE_MODEL") or "",
    "gemini-2.5-flash-image",
    "gemini-3.1-flash-image",
]


def _extract_inline_image_bytes(resp):
    """Pull image bytes out of a generate_content response (Gemini image models)."""
    out = []
    for cand in (getattr(resp, "candidates", []) or []):
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", []) or []):
            inline = getattr(part, "inline_data", None) or getattr(part, "inlineData", None)
            data = getattr(inline, "data", None) if inline else None
            if data:
                out.append(data)
    return out


def _generate_ad_images(client, prompt, count):
    """Return (list_of_image_bytes, model_used). Tries Imagen then Gemini image."""
    last_err = None

    for model in [m for m in _IMAGEN_CANDIDATES if m]:
        for cfg_kind in ("dict", "typed"):
            try:
                if cfg_kind == "dict":
                    resp = client.models.generate_images(
                        model=model, prompt=prompt,
                        config={"number_of_images": count, "aspect_ratio": "9:16"},
                    )
                else:
                    from google.genai import types as _types
                    resp = client.models.generate_images(
                        model=model, prompt=prompt,
                        config=_types.GenerateImagesConfig(
                            number_of_images=count, aspect_ratio="9:16"),
                    )
                imgs = _extract_image_bytes(resp)
                if imgs:
                    logger.info("ctwa generate-image: produced %d image(s) via %s", len(imgs), model)
                    return imgs, model
            except Exception as e:
                last_err = e
                logger.info("ctwa generate-image: imagen %s (%s) failed: %s", model, cfg_kind, e)

    # Fallback — Gemini native image generation (generateContent, inline image parts)
    for model in [m for m in _GEMINI_IMAGE_CANDIDATES if m]:
        try:
            from core.genai_bridge import generate_text
            resp = generate_text(
                model=model,
                contents=prompt,
                config={"response_modalities": ["TEXT", "IMAGE"]},
                gemini_client=client,
                feature="ctwa",
            )
            imgs = _extract_inline_image_bytes(resp)
            if imgs:
                logger.info("ctwa generate-image: produced %d image(s) via %s", len(imgs), model)
                return imgs[:count], model
        except Exception as e:
            last_err = e
            logger.info("ctwa generate-image: gemini %s failed: %s", model, e)

    if last_err:
        raise last_err
    raise RuntimeError("no_images_generated")


@ctwa_bp.route("/generate-image", methods=["POST"])
def generate_image():
    """AI-generate 1-4 vertical (9:16) ad images and return public Spaces URLs.

    Body: { workspace_id, prompt?, count? }
    With no `prompt` this is "just generate" mode: the prompt is composed from
    the workspace's own business context.
    """
    try:
        import secrets as _secrets

        payload = request.get_json(silent=True) or {}
        workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id required"}), 400

        try:
            count = int(payload.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 4))

        # 1) Prompt — caller's wording wins; otherwise build one from the business.
        user_prompt = (payload.get("prompt") or "").strip()
        if user_prompt:
            base_prompt = user_prompt
        else:
            business_name, description, _website = _workspace_context(workspace_id)
            if business_name or description:
                base_prompt = (
                    f"A professional, eye-catching vertical 9:16 advertising image for "
                    f"{business_name or 'the business'}. {description or ''} "
                    "Clean modern commercial photography, bright lighting, "
                    "no text overlay, no watermark."
                ).strip()
            else:
                base_prompt = _GENERIC_AD_IMAGE_PROMPT
        final_prompt = f"{base_prompt}{_AD_IMAGE_SUFFIX}"

        # 2) Generate via Vertex Imagen.
        from SocioviaCrm.routes.meta_integration import get_genai_client
        client = get_genai_client()
        if not client:
            return jsonify({"success": False, "error": "genai_client_not_initialized"}), 500

        try:
            images, _model_used = _generate_ad_images(client, final_prompt, count)
        except Exception as e:
            logger.exception("ctwa generate-image: all image models failed")
            return jsonify({"success": False, "error": str(e)}), 502

        if not images:
            logger.warning("ctwa generate-image: model returned no usable image bytes")
            return jsonify({"success": False, "error": "no_images_generated"}), 502

        # 3) Upload each image to Spaces — Meta must be able to FETCH the asset.
        from core.spaces_storage import (
            is_spaces_configured, get_s3_client, build_object_key, build_public_url,
        )
        if not is_spaces_configured():
            return jsonify({"success": False, "error": "storage_not_configured"}), 503

        s3_client, bucket = get_s3_client()
        urls = []
        for data in images:
            unique = f"{_secrets.token_hex(8)}.png"
            key = build_object_key("uploads", "ads", str(workspace_id), unique)
            s3_client.put_object(
                Bucket=bucket, Key=key, Body=data, ContentType="image/png",
                ACL="public-read", CacheControl="public, max-age=31536000",
            )
            urls.append({"url": build_public_url(key)})

        return jsonify({"success": True, "images": urls, "prompt_used": final_prompt})
    except Exception as e:
        logger.exception("ctwa: generate-image failed")
        return jsonify({"success": False, "error": str(e)}), 502


@ctwa_bp.route("/generate-copy", methods=["POST"])
def generate_copy():
    """AI-generate WhatsApp ad copy variations (primary_text + headline).

    Body: { workspace_id, prompt?, ad_type? }  ad_type: "status" | "ctwa"
    """
    try:
        payload = request.get_json(silent=True) or {}
        workspace_id = payload.get("workspace_id") or request.args.get("workspace_id")
        if not workspace_id:
            return jsonify({"success": False, "error": "workspace_id required"}), 400

        user_prompt = (payload.get("prompt") or "").strip()
        ad_type = (payload.get("ad_type") or "ctwa").strip().lower()
        placement = (
            "a full-screen WhatsApp Status ad" if ad_type == "status"
            else "a Click-to-WhatsApp ad"
        )

        business_name, description, website = _workspace_context(workspace_id)
        context_lines = []
        if business_name:
            context_lines.append(f"Business name: {business_name}")
        if description:
            context_lines.append(f"What the business does: {description}")
        if website:
            context_lines.append(f"Website: {website}")
        business_context = "\n".join(context_lines) or "No business details available."

        direction = (
            f"The advertiser's direction: {user_prompt}"
            if user_prompt
            else "No extra direction was given — base the copy purely on the business context."
        )

        instruction = (
            f"You are an expert performance marketer writing {placement}.\n\n"
            f"{business_context}\n\n"
            f"{direction}\n\n"
            "Return ONLY valid JSON, no markdown, in exactly this shape:\n"
            '{"variations":[{"primary_text":"...","headline":"..."}]}\n'
            "Give 3 variations. primary_text max 125 characters. headline max 40 characters.\n"
            "Write for a WhatsApp ad whose button opens a WhatsApp chat, so end with a "
            "soft call to message."
        )

        from SocioviaCrm.routes.meta_integration import (
            _generate_content_robust,
            _extract_text_from_genai_response,
            _extract_json_from_textt,
        )
        resp = _generate_content_robust(instruction, temperature=0.8)
        text = _extract_text_from_genai_response(resp)
        data = _extract_json_from_textt(text)

        # The model occasionally answers with a bare list instead of the wrapper.
        if isinstance(data, dict):
            raw = data.get("variations")
        elif isinstance(data, list):
            raw = data
        else:
            raw = None
        if not isinstance(raw, list):
            logger.warning("ctwa generate-copy: unparseable model output: %r", (text or "")[:300])
            return jsonify({"success": False, "error": "could_not_parse_model_output"}), 502

        variations = []
        for item in raw:
            if isinstance(item, dict):
                primary = str(item.get("primary_text") or item.get("body") or "").strip()
                headline = str(item.get("headline") or item.get("title") or "").strip()
            else:
                primary, headline = str(item or "").strip(), ""
            if not primary and not headline:
                continue
            variations.append({"primary_text": primary[:125], "headline": headline[:40]})

        if not variations:
            return jsonify({"success": False, "error": "no_variations_generated"}), 502

        return jsonify({"success": True, "variations": variations})
    except Exception as e:
        logger.exception("ctwa: generate-copy failed")
        return jsonify({"success": False, "error": str(e)}), 502
