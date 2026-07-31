# crm_management/routes/campaigns.py
import os
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

import requests
from flask import (
    Blueprint,
    jsonify,
    current_app,
    request,
    abort,
    make_response,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import cast
from sqlalchemy.types import String, Integer

from models import SocialAccount  # use your existing SocialAccount model
from MetaHelpers.GetWorkspaceData import discover_ad_accounts_for_token

FB_API_VERSION = os.getenv("FB_API_VERSION", "v22.0")
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

bp = Blueprint("campaigns", __name__, url_prefix="/campaigns")


# ---------- SMALL HELPERS ----------

def _dt_to_iso(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        return datetime.fromisoformat(str(value)).isoformat()
    except Exception:
        return str(value)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _get_db_and_campaign_model():
    db = getattr(current_app, "db", None)
    crm_models = getattr(current_app, "crm_models", {}) or {}
    Campaign = crm_models.get("Campaign")

    if db is None or Campaign is None:
        raise RuntimeError("CRM DB / Campaign model not configured on current_app")
    return db, Campaign


def _require_owned_workspace(requested_workspace_id=None):
    """Resolve + verify the caller owns a workspace (fail CLOSED).

    Returns (workspace, None) on success or (None, (response, status)) so callers
    can ``return err``. Lazy import of tenant.context avoids circular imports.
    """
    from tenant.context import get_current_user, resolve_owned_workspace
    user = get_current_user()
    if not user:
        return None, (jsonify({"success": False, "error": "authentication_required"}), 401)
    return resolve_owned_workspace(user, requested_workspace_id)


def _apply_workspace_and_user_filters(query, Campaign, workspace_id: Optional[str], user_id: Optional[str]):
    try:
        if workspace_id is not None and hasattr(Campaign, "workspace_id"):
            col = Campaign.__table__.columns.get("workspace_id")
            if col is not None and isinstance(col.type, Integer):
                try:
                    query = query.filter(Campaign.workspace_id == int(workspace_id))
                except Exception:
                    query = query.filter(cast(Campaign.workspace_id, String) == str(workspace_id))
            else:
                try:
                    query = query.filter(cast(Campaign.workspace_id, String) == str(workspace_id))
                except Exception:
                    query = query.filter(Campaign.workspace_id == workspace_id)

        if user_id is not None and hasattr(Campaign, "user_id"):
            col_u = Campaign.__table__.columns.get("user_id")
            if col_u is not None and isinstance(col_u.type, Integer):
                try:
                    query = query.filter(Campaign.user_id == int(user_id))
                except Exception:
                    query = query.filter(cast(Campaign.user_id, String) == str(user_id))
            else:
                try:
                    query = query.filter(cast(Campaign.user_id, String) == str(user_id))
                except Exception:
                    query = query.filter(Campaign.user_id == user_id)
    except Exception as e:
        current_app.logger.debug("Failed applying workspace/user filters: %s", e, exc_info=True)
    return query


def _serialize_campaign(c: Any) -> Dict[str, Any]:
    def _get_decimal(field_name: str) -> Optional[float]:
        val = getattr(c, field_name, None)
        if val is None:
            return None
        try:
            return float(val)
        except Exception:
            try:
                return float(Decimal(str(val)))
            except Exception:
                return None

    return {
        "id": str(getattr(c, "id", None)),
        "workspace_id": getattr(c, "workspace_id", None),
        "user_id": getattr(c, "user_id", None),
        "name": getattr(c, "name", None),
        "status": getattr(c, "status", None),
        "objective": getattr(c, "objective", None),
        "buying_type": getattr(c, "buying_type", None),
        "meta_campaign_id": getattr(c, "meta_campaign_id", None),
        "daily_budget": _get_decimal("daily_budget"),
        "lifetime_budget": _get_decimal("lifetime_budget"),
        "spend": _get_decimal("spend"),
        "impressions": getattr(c, "impressions", None),
        "clicks": getattr(c, "clicks", None),
        "leads": getattr(c, "leads", None),
        "revenue": _get_decimal("revenue"),
        "created_at": _dt_to_iso(getattr(c, "created_at", None)),
        "updated_at": _dt_to_iso(getattr(c, "updated_at", None)),
        "start_time": _dt_to_iso(getattr(c, "start_time", None)),
        "end_time": _dt_to_iso(getattr(c, "end_time", None)),
    }


def _get_fb_account_for_workspace_user(workspace_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the Facebook account for a workspace.
    In multi-tenant SaaS, the Facebook account is shared at workspace level,
    so we only filter by workspace_id (user_id is optional for audit purposes).
    """
    if not workspace_id:
        current_app.logger.debug(
            "_get_fb_account_for_workspace_user called without workspace_id (%s)",
            workspace_id,
        )
        return None

    # SECURITY (IDOR): the FB access token / ad account is workspace-scoped. Only
    # return it when the AUTHENTICATED caller owns the requested workspace —
    # otherwise any caller could pull another tenant's Meta token by passing its
    # workspace_id. Callers already treat None as "no account" (HTTP 400).
    from tenant.context import get_current_user, user_owns_workspace
    _user = get_current_user()
    if not _user or not user_owns_workspace(_user, workspace_id):
        current_app.logger.warning(
            "cross_workspace_fb_account_denied user=%s requested_ws=%s",
            getattr(_user, "id", None), workspace_id,
        )
        return None

    try:
        # Query by workspace_id only - all users in a workspace share the connected FB account
        q = SocialAccount.query.filter(SocialAccount.provider == "facebook")
        q = q.filter(cast(SocialAccount.workspace_id, String) == str(workspace_id))
        acct = q.order_by(SocialAccount.updated_at.desc()).first()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error querying SocialAccount in _get_fb_account_for_workspace_user: %s", e)
        return None

    if not acct or not getattr(acct, "access_token", None):
        current_app.logger.debug(
            "No SocialAccount with token found for workspace_id=%s", workspace_id
        )
        return None

    access_token = acct.access_token
    stored_ad_account_id = getattr(acct, "ad_account_id", None)
    final_ad_account_id = stored_ad_account_id

    # Per-tenant Graph API version (env / module default for T0000 / unconfigured).
    from tenant.integration import get_tenant_meta_config
    _api_version = get_tenant_meta_config(workspace_id=workspace_id).fb_api_version or FB_API_VERSION

    # NEW: Verify stored ad_account_id is accessible OR discover one if missing
    if not stored_ad_account_id:
        current_app.logger.info("No ad_account_id stored for workspace_id=%s. Attempting discovery.", workspace_id)
        try:
            discovered = discover_ad_accounts_for_token(access_token, api_version=_api_version)
            if discovered and len(discovered) > 0:
                final_ad_account_id = discovered[0]
                current_app.logger.info("Discovered ad_account_id=%s for workspace_id=%s", final_ad_account_id, workspace_id)
            else:
                current_app.logger.warning("No ad accounts discovered for token in workspace_id=%s", workspace_id)
        except Exception as e:
            current_app.logger.warning("Failed to discover ad accounts for workspace_id=%s: %s", workspace_id, e)
    else:
        # Verify stored ad_account_id is accessible
        GRAPH = f"https://graph.facebook.com/{_api_version}"
        verify_url = f"{GRAPH}/act_{stored_ad_account_id}"
        try:
            resp = requests.get(verify_url, params={"access_token": access_token}, timeout=5)
            verify_data = resp.json()
            if verify_data.get("error"):
                error_code = verify_data.get("error", {}).get("code")
                error_msg = verify_data.get("error", {}).get("message", "")
                current_app.logger.warning(
                    "Stored ad_account_id=%s not accessible (error %s: %s). Attempting fallback discovery.",
                    stored_ad_account_id,
                    error_code,
                    error_msg,
                )
                # Try to discover an accessible account
                try:
                    discovered = discover_ad_accounts_for_token(access_token, api_version=_api_version)
                    if discovered and len(discovered) > 0:
                        final_ad_account_id = discovered[0]
                        current_app.logger.info(
                            "Using discovered ad_account_id=%s (stored ID %s was not accessible)",
                            final_ad_account_id,
                            stored_ad_account_id
                        )
                    else:
                        current_app.logger.error("No ad accounts discovered for token")
                except Exception as e:
                    current_app.logger.warning("Failed to discover ad accounts: %s", e)
        except Exception as e:
            current_app.logger.warning("Failed to verify ad_account_id=%s: %s", stored_ad_account_id, e)

    current_app.logger.debug(
        "Using SocialAccount id=%s ad_account_id=%s (original: %s) workspace_id=%s user_id=%s",
        getattr(acct, "id", None),
        final_ad_account_id,
        stored_ad_account_id,
        getattr(acct, "workspace_id", None),
        getattr(acct, "user_id", None),
    )

    return {
        "social_account_id": getattr(acct, "id", None),
        "access_token": access_token,
        "ad_account_id": final_ad_account_id,
        "page_id": getattr(acct, "provider_user_id", None),
    }


def _format_ad_account_id(ad_account_id: Optional[str]) -> Optional[str]:
    """
    Ensure ad account id is in the 'act_<id>' form for Graph API paths.
    If ad_account_id is None return None.
    """
    if not ad_account_id:
        return None
    s = str(ad_account_id).strip()
    if s.lower().startswith("act_"):
        return s
    # if it's digits (common) prefix with act_
    if s.isdigit():
        return f"act_{s}"
    # otherwise return as-is (best-effort)
    return s


def _actions_to_dict(actions: Optional[List[Dict[str, Any]]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    if not actions:
        return out
    for a in actions:
        at = a.get("action_type")
        if not at:
            continue
        val = a.get("value") or a.get("values") or 0
        try:
            v = float(val)
        except Exception:
            try:
                v = float(a.get("value", 0) or 0)
            except Exception:
                v = 0.0
        out[at] = out.get(at, 0.0) + v
    return out


# ---------- Meta/Graph helpers (safe requests) ----------

def _safe_get(url: str, params: dict, timeout: int = 25):
    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"HTTP error contacting Meta: {e}")
    try:
        j = resp.json()
    except Exception:
        j = None
    if not resp.ok:
        raise RuntimeError({"status": resp.status_code, "error": j or resp.text})
    return j or {}


# ---------- core Meta fetchers ----------

def _fetch_meta_campaign_view(meta_campaign_id: str, access_token: str) -> Dict[str, Any]:
    camp_fields = ",".join([
        "id", "name", "status", "objective", "account_id", "created_time",
        "start_time", "stop_time", "special_ad_categories", "buying_type", "effective_status",
    ])
    camp_url = f"{BASE_URL}/{meta_campaign_id}"
    camp_json = _safe_get(camp_url, params={"fields": camp_fields, "access_token": access_token}, timeout=25)

    # adsets
    adset_fields = ",".join([
        "id", "name", "daily_budget", "lifetime_budget", "billing_event",
        "optimization_goal", "effective_status", "start_time", "end_time", "targeting",
    ])
    adset_url = f"{BASE_URL}/{meta_campaign_id}/adsets"
    adset_json = {}
    try:
        adset_json = _safe_get(adset_url, params={"fields": adset_fields, "limit": 50, "access_token": access_token}, timeout=25)
    except Exception:
        adset_json = {"data": []}
    adsets_raw = adset_json.get("data", []) or []

    adsets: List[Dict[str, Any]] = []
    def _budget_to_float(b):
        if b is None:
            return None
        try:
            return float(b) / 100.0
        except Exception:
            return None

    for a in adsets_raw:
        adsets.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "daily_budget": _budget_to_float(a.get("daily_budget")),
            "lifetime_budget": _budget_to_float(a.get("lifetime_budget")),
            "billing_event": a.get("billing_event"),
            "optimization_goal": a.get("optimization_goal"),
            "effective_status": a.get("effective_status"),
            "start_time": a.get("start_time"),
            "end_time": a.get("end_time"),
            "targeting": a.get("targeting") or {},
        })

    # insights (aggregated)
    insights_fields = ",".join([
        "impressions", "reach", "clicks", "spend", "ctr", "cpc", "cpm", "actions", "action_values",
    ])
    ins_url = f"{BASE_URL}/{meta_campaign_id}/insights"
    ins_json = {}
    try:
        ins_json = _safe_get(ins_url, params={"fields": insights_fields, "time_increment": "all_days", "access_token": access_token}, timeout=25)
    except Exception:
        ins_json = {"data": []}

    ins_row = None
    d = ins_json.get("data") or []
    if d:
        ins_row = d[0]

    insights: Dict[str, Any] = {}
    if ins_row:
        impressions = int(ins_row.get("impressions") or 0)
        clicks = int(ins_row.get("clicks") or 0)
        spend = Decimal(str(ins_row.get("spend") or "0"))
        actions = _actions_to_dict(ins_row.get("actions"))
        action_values = _actions_to_dict(ins_row.get("action_values"))

        leads = int(actions.get("lead", 0) or 0)
        purchases = int(actions.get("purchase", 0) or 0)
        revenue = Decimal(action_values.get("purchase", 0) or 0)

        ctr = float(ins_row.get("ctr") or 0.0)
        try:
            cpc = float(ins_row.get("cpc") or 0.0)
            cpm = float(ins_row.get("cpm") or 0.0)
        except Exception:
            cpc = 0.0
            cpm = 0.0

        cpl = float(spend / leads) if leads > 0 and spend > 0 else 0.0
        roas = float(revenue / spend) if spend > 0 else 0.0

        insights = {
            "impressions": impressions,
            "clicks": clicks,
            "spend": float(round(spend, 2)),
            "leads": leads,
            "purchases": purchases,
            "revenue": float(round(revenue, 2)),
            "ctr": float(round(ctr, 2)),
            "cpc": float(round(cpc, 2)),
            "cpm": float(round(cpm, 2)),
            "cpl": float(round(cpl, 2)),
            "roas": float(round(roas, 2)),
        }

    return {
        "campaign": camp_json,
        "adsets": adsets,
        "insights": insights,
    }


def _fetch_meta_insights_only(meta_campaign_id: str, access_token: str, date_preset: str = "last_30d") -> Dict[str, Any]:
    fields = ",".join([
        "impressions", "reach", "clicks", "spend", "ctr", "cpc", "cpm", "actions", "action_values",
    ])
    url = f"{BASE_URL}/{meta_campaign_id}/insights"
    j = _safe_get(url, params={"fields": fields, "date_preset": date_preset, "time_increment": "all_days", "access_token": access_token}, timeout=25)
    data = j.get("data") or []
    if not data:
        return {}
    row = data[0]
    actions = _actions_to_dict(row.get("actions"))
    action_values = _actions_to_dict(row.get("action_values"))

    impressions = int(row.get("impressions") or 0)
    clicks = int(row.get("clicks") or 0)
    spend = Decimal(str(row.get("spend") or "0"))
    leads = int(actions.get("lead", 0) or 0)
    purchases = int(actions.get("purchase", 0) or 0)
    revenue = Decimal(action_values.get("purchase", 0) or 0)

    ctr = float(row.get("ctr") or 0.0)
    cpc = float(row.get("cpc") or 0.0) if clicks else 0.0
    cpm = float(row.get("cpm") or 0.0) if impressions else 0.0
    cpl = float(spend / leads) if leads > 0 and spend > 0 else 0.0
    roas = float(revenue / spend) if spend > 0 else 0.0

    return {
        "impressions": impressions,
        "clicks": clicks,
        "spend": float(round(spend, 2)),
        "leads": leads,
        "purchases": purchases,
        "revenue": float(round(revenue, 2)),
        "ctr": float(round(ctr, 2)),
        "cpc": float(round(cpc, 2)),
        "cpm": float(round(cpm, 2)),
        "cpl": float(round(cpl, 2)),
        "roas": float(round(roas, 2)),
    }


def _fetch_meta_daily_trend(meta_campaign_id: str, access_token: str, date_preset: str = "last_30d") -> List[Dict[str, Any]]:
    fields = ",".join([
        "date_start", "impressions", "reach", "clicks", "spend", "ctr", "cpc", "cpm", "actions", "action_values",
    ])
    url = f"{BASE_URL}/{meta_campaign_id}/insights"
    j = _safe_get(url, params={"fields": fields, "date_preset": date_preset, "time_increment": 1, "access_token": access_token}, timeout=30)
    rows = j.get("data") or []
    trend: List[Dict[str, Any]] = []
    for r in rows:
        actions = _actions_to_dict(r.get("actions"))
        action_values = _actions_to_dict(r.get("action_values"))
        spend = Decimal(str(r.get("spend") or "0"))
        impressions = int(r.get("impressions") or 0)
        clicks = int(r.get("clicks") or 0)
        leads = int(actions.get("lead", 0) or 0)
        revenue = Decimal(action_values.get("purchase", 0) or 0)
        ctr = float(r.get("ctr") or 0.0)
        cpc = float(r.get("cpc") or 0.0) if clicks else 0.0
        cpm = float(r.get("cpm") or 0.0) if impressions else 0.0
        trend.append({
            "date": r.get("date_start"),
            "impressions": impressions,
            "clicks": clicks,
            "spend": float(round(spend, 2)),
            "ctr": float(round(ctr, 2)),
            "cpc": float(round(cpc, 2)),
            "cpm": float(round(cpm, 2)),
            "leads": leads,
            "revenue": float(round(revenue, 2)),
        })
    return trend


# ---------- CRM-BACKED ROUTES ----------

@bp.route("", methods=["GET"])
def list_campaigns():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    # SECURITY: resolve + verify the caller owns the workspace and ALWAYS scope
    # to it. Was fail-OPEN: with no workspace_id every tenant's campaigns leaked.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        q = db.session.query(Campaign)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        campaigns = q.all()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error fetching campaigns: %s", e)
        campaigns = []

    result: List[Dict[str, Any]] = []

    if not campaigns:
        mock = [
            {
                "id": "c-1",
                "name": "Summer Sale – Prospects",
                "status": "ACTIVE",
                "objective": "OUTCOME_SALES",
                "daily_budget": 1500.0,
                "lifetime_budget": None,
                "impressions": 450000,
                "clicks": 12000,
                "leads": 450,
                "spend": 12500.0,
                "revenue": 43750.0,
            },
            {
                "id": "c-2",
                "name": "App Installs – Android Only",
                "status": "PAUSED",
                "objective": "OUTCOME_APP_PROMOTION",
                "daily_budget": 800.0,
                "lifetime_budget": None,
                "impressions": 150000,
                "clicks": 5400,
                "leads": 120,
                "spend": 4200.0,
                "revenue": 0.0,
            },
        ]
        for m in mock:
            spend = float(m["spend"])
            leads = int(m["leads"]) if m["leads"] else 0
            revenue = float(m["revenue"])
            roas = (revenue / spend) if spend > 0 else 0.0
            cpl = (spend / leads) if leads > 0 else None
            result.append(
                {
                    "id": m["id"],
                    "name": m["name"],
                    "status": m["status"],
                    "objective": m["objective"],
                    "daily_budget": m["daily_budget"],
                    "lifetime_budget": m["lifetime_budget"],
                    "spend": spend,
                    "impressions": m["impressions"],
                    "clicks": m["clicks"],
                    "leads": leads,
                    "cpl": round(cpl, 2) if cpl else None,
                    "roas": round(roas, 2),
                }
            )
        return jsonify(result)

    for c in campaigns:
        row = _serialize_campaign(c)
        spend = row.get("spend") or 0.0
        leads = row.get("leads") or 0
        revenue = row.get("revenue") or 0.0
        roas = (revenue / spend) if spend else 0.0
        cpl = (spend / leads) if leads else None
        row["cpl"] = round(cpl, 2) if cpl else None
        row["roas"] = round(roas, 2)
        result.append(row)

    result.sort(key=lambda r: r.get("spend") or 0.0, reverse=True)
    return jsonify(result)


@bp.route("/<string:campaign_id>", methods=["GET"])
def get_campaign(campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    # SECURITY: verify workspace ownership and scope the lookup to it.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        q = db.session.query(Campaign).filter(Campaign.id == campaign_id)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        c = q.one_or_none()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error fetching campaign by id: %s", e)
        return jsonify({"error": "db_error"}), 500

    if c is None:
        mock_map = {
            "c-1": {
                "id": "c-1",
                "name": "Summer Sale – Prospects",
                "status": "ACTIVE",
                "objective": "OUTCOME_SALES",
                "daily_budget": 1500.0,
                "lifetime_budget": None,
                "impressions": 450000,
                "clicks": 12000,
                "leads": 450,
                "spend": 12500.0,
                "revenue": 43750.0,
            },
            "c-2": {
                "id": "c-2",
                "name": "App Installs – Android Only",
                "status": "PAUSED",
                "objective": "OUTCOME_APP_PROMOTION",
                "daily_budget": 800.0,
                "lifetime_budget": None,
                "impressions": 150000,
                "clicks": 5400,
                "leads": 120,
                "spend": 4200.0,
                "revenue": 0.0,
            },
        }
        mock = mock_map.get(campaign_id)
        if not mock:
            abort(404, description="Campaign not found")
        spend = float(mock["spend"])
        leads = int(mock["leads"]) if mock["leads"] else 0
        revenue = float(mock["revenue"])
        roas = (revenue / spend) if spend > 0 else 0.0
        cpl = (spend / leads) if leads > 0 else None
        out = {
            "id": mock["id"],
            "workspace_id": workspace_id,
            "user_id": user_id,
            "name": mock["name"],
            "status": mock["status"],
            "objective": mock["objective"],
            "buying_type": None,
            "meta_campaign_id": None,
            "daily_budget": mock["daily_budget"],
            "lifetime_budget": mock["lifetime_budget"],
            "spend": spend,
            "impressions": mock["impressions"],
            "clicks": mock["clicks"],
            "leads": leads,
            "revenue": revenue,
            "cpl": round(cpl, 2) if cpl else None,
            "roas": round(roas, 2),
            "created_at": None,
            "updated_at": None,
            "start_time": None,
            "end_time": None,
            "meta": {
                "campaign": None,
                "adsets": [],
                "insights": {},
                "error": "mock_campaign_no_meta_connection",
            },
        }
        return jsonify(out), 200

    out = _serialize_campaign(c)
    meta_campaign_id = getattr(c, "meta_campaign_id", None) or out["id"]

    meta_block = None
    if workspace_id and user_id and meta_campaign_id:
        fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
        if fb_account and fb_account.get("access_token"):
            try:
                meta_block = _fetch_meta_campaign_view(
                    meta_campaign_id=meta_campaign_id,
                    access_token=fb_account["access_token"],
                )
            except Exception as e:
                current_app.logger.exception("Failed fetching Meta campaign view: %s", e)
                meta_block = {"error": str(e)}
        else:
            meta_block = {"error": "No Facebook SocialAccount / token configured"}

    out["meta"] = meta_block
    return jsonify(out)


@bp.route("", methods=["POST"])
def create_campaign():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    # SECURITY: verify the caller owns the workspace they're writing into.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    c = Campaign()
    if hasattr(c, "workspace_id") and workspace_id is not None:
        c.workspace_id = workspace_id
    if hasattr(c, "user_id") and user_id is not None:
        c.user_id = user_id

    for field in ["name", "status", "objective", "buying_type", "meta_campaign_id"]:
        if field in data and hasattr(c, field):
            setattr(c, field, data[field])

    for num_field in ["daily_budget", "lifetime_budget", "spend", "impressions", "clicks", "leads", "revenue"]:
        if num_field in data and hasattr(c, num_field):
            setattr(c, num_field, data[num_field])

    for dt_field in ["start_time", "end_time"]:
        if dt_field in data and hasattr(c, dt_field):
            setattr(c, dt_field, _parse_datetime(data[dt_field]))

    if hasattr(c, "created_at"):
        setattr(c, "created_at", datetime.utcnow())
    if hasattr(c, "updated_at"):
        setattr(c, "updated_at", datetime.utcnow())

    try:
        db.session.add(c)
        db.session.commit()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error creating campaign: %s", e)
        db.session.rollback()
        return jsonify({"error": "db_error"}), 500

    return jsonify(_serialize_campaign(c)), 201


@bp.route("/<string:campaign_id>", methods=["PUT", "PATCH"])
def update_campaign(campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    # SECURITY: verify workspace ownership before any local update OR Meta proxy.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        q = db.session.query(Campaign).filter(Campaign.id == campaign_id)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        c = q.one_or_none()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error fetching campaign for update: %s", e)
        return jsonify({"error": "db_error"}), 500

    if c is None:
        # Try finding by external_id
        try:
            q2 = db.session.query(Campaign).filter(Campaign.external_id == campaign_id)
            q2 = _apply_workspace_and_user_filters(q2, Campaign, workspace_id, user_id)
            c = q2.one_or_none()
        except SQLAlchemyError:
             pass

    if c is None:
        # Fallback: Proxy directly to Meta if we have credentials
        # This handles cases where frontend sends a Meta ID that hasn't been synced locally
        if workspace_id and user_id:
            try:
                sa = db.session.query(SocialAccount).filter(
                    SocialAccount.provider == "facebook",
                    cast(SocialAccount.workspace_id, String) == str(workspace_id),
                    cast(SocialAccount.user_id, String) == str(user_id)
                ).order_by(SocialAccount.updated_at.desc()).first()

                if sa and sa.access_token:
                    # Attempt direct update on Meta
                    meta_url = f"{BASE_URL}/{campaign_id}"
                    payload = data.copy()
                    payload["access_token"] = sa.access_token

                    # Remove local-only fields if present
                    for k in ["workspace_id", "user_id", "meta_campaign_id"]:
                        payload.pop(k, None)

                    current_app.logger.info("[update_campaign] Proxying to Meta: POST %s payload=%s", meta_url, {k: v for k, v in payload.items() if k != "access_token"})
                    resp = requests.post(meta_url, json=payload)
                    if resp.ok:
                        return jsonify(resp.json())
                    else:
                        # If Meta also fails, return the Meta error
                        current_app.logger.error("[update_campaign] Meta error: %s", resp.text)
                        return jsonify(resp.json()), resp.status_code

            except Exception as e:
                current_app.logger.warning("Meta fallback update failed: %s", e)

        abort(404, description="Campaign not found locally or on Meta")

    for field in ["name", "status", "objective", "buying_type", "meta_campaign_id"]:
        if field in data and hasattr(c, field):
            setattr(c, field, data[field])

    for num_field in ["daily_budget", "lifetime_budget", "spend", "impressions", "clicks", "leads", "revenue"]:
        if num_field in data and hasattr(c, num_field):
            setattr(c, num_field, data[num_field])

    for dt_field in ["start_time", "end_time"]:
        if dt_field in data and hasattr(c, dt_field):
            setattr(c, dt_field, _parse_datetime(data[dt_field]))

    if hasattr(c, "updated_at"):
        setattr(c, "updated_at", datetime.utcnow())

    try:
        db.session.add(c)
        db.session.commit()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error updating campaign: %s", e)
        db.session.rollback()
        return jsonify({"error": "db_error"}), 500

    return jsonify(_serialize_campaign(c)), 200


@bp.route("/<string:campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    # SECURITY: verify workspace ownership and scope the lookup/delete to it.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        q = db.session.query(Campaign).filter(Campaign.id == campaign_id)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        c = q.one_or_none()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error fetching campaign for delete: %s", e)
        return jsonify({"error": "db_error"}), 500

    if c is None:
        abort(404, description="Campaign not found")

    try:
        db.session.delete(c)
        db.session.commit()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error deleting campaign: %s", e)
        db.session.rollback()
        return jsonify({"error": "db_error"}), 500

    return jsonify({"deleted": True, "id": campaign_id}), 200


# ---------- STATUS / PAUSE / RESUME (LOCAL CRM) ----------

def _change_campaign_status(campaign_id: str, new_status: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    # SECURITY: verify workspace ownership and scope the lookup to it.
    ws, err = _require_owned_workspace(workspace_id)
    if err:
        return err
    workspace_id = ws.id

    try:
        db, Campaign = _get_db_and_campaign_model()
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500

    try:
        q = db.session.query(Campaign).filter(Campaign.id == campaign_id)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        c = q.one_or_none()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error fetching campaign for status change: %s", e)
        return jsonify({"error": "db_error"}), 500

    if c is None:
        abort(404, description="Campaign not found")

    if hasattr(c, "status"):
        setattr(c, "status", new_status)
    if hasattr(c, "updated_at"):
        setattr(c, "updated_at", datetime.utcnow())

    try:
        db.session.add(c)
        db.session.commit()
    except SQLAlchemyError as e:
        current_app.logger.exception("Error updating campaign status: %s", e)
        db.session.rollback()
        return jsonify({"error": "db_error"}), 500

    return jsonify(_serialize_campaign(c)), 200


@bp.route("/<string:campaign_id>/status", methods=["POST"])
def update_campaign_status(campaign_id: str):
    data = request.get_json(silent=True) or {}
    new_status = data.get("status")
    if not new_status:
        return jsonify({"error": "status is required"}), 400
    return _change_campaign_status(campaign_id, new_status)


@bp.route("/<string:campaign_id>/pause", methods=["POST"])
def pause_campaign(campaign_id: str):
    return _change_campaign_status(campaign_id, "PAUSED")


@bp.route("/<string:campaign_id>/resume", methods=["POST"])
def resume_campaign(campaign_id: str):
    return _change_campaign_status(campaign_id, "ACTIVE")


# ---------- PURE META "LIVE" ROUTES (A-Z like Ads Manager) ----------

@bp.route("/live", methods=["GET"])
def list_campaigns_live():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    date_preset = request.args.get("date_preset", "last_30d")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token") or not fb_account.get("ad_account_id"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    ad_account_id = _format_ad_account_id(fb_account["ad_account_id"])
    if not ad_account_id:
        return jsonify({"error": "invalid_ad_account_id"}), 400

    camp_fields = ",".join([
        "id", "name", "status", "objective", "effective_status",
        "daily_budget", "lifetime_budget", "created_time", "start_time", "stop_time", "buying_type",
    ])

    camp_url = f"{BASE_URL}/{ad_account_id}/campaigns"
    try:
        camp_resp = requests.get(
            camp_url,
            params={"fields": camp_fields, "limit": 100, "access_token": access_token},
            timeout=25,
        )
        try:
            camp_json = camp_resp.json()
        except Exception:
            camp_json = None
        if not camp_resp.ok:
            current_app.logger.error("Meta campaigns list error: %s", camp_json)
            return jsonify({"error": camp_json or "meta_error", "status_code": camp_resp.status_code}), 502
    except Exception as e:
        current_app.logger.exception("Error fetching campaigns from Meta: %s", e)
        return jsonify({"error": str(e)}), 502

    campaigns_raw = (camp_json.get("data") if camp_json else []) or []

    insights_fields = ",".join([
        "campaign_id", "campaign_name", "objective", "impressions", "reach", "clicks", "spend", "ctr", "cpc", "cpm", "actions", "action_values",
    ])

    ins_url = f"{BASE_URL}/{ad_account_id}/insights"
    try:
        ins_resp = requests.get(
            ins_url,
            params={"fields": insights_fields, "level": "campaign", "date_preset": date_preset, "time_increment": "all_days", "access_token": access_token},
            timeout=25,
        )
        try:
            ins_json = ins_resp.json()
        except Exception:
            ins_json = None
        if not ins_resp.ok:
            current_app.logger.warning("Meta insights fetch failed: %s", ins_json)
            ins_json = {"data": []}
    except Exception as e:
        current_app.logger.exception("Error fetching insights: %s", e)
        ins_json = {"data": []}

    insights_by_campaign: Dict[str, Dict[str, Any]] = {}
    for row in (ins_json.get("data") or []):
        cid = row.get("campaign_id")
        if not cid:
            continue
        actions = _actions_to_dict(row.get("actions"))
        action_values = _actions_to_dict(row.get("action_values"))
        impressions = int(row.get("impressions") or 0)
        clicks = int(row.get("clicks") or 0)
        spend = Decimal(str(row.get("spend") or "0"))
        leads = int(actions.get("lead", 0) or 0)
        purchases = int(actions.get("purchase", 0) or 0)
        revenue = Decimal(action_values.get("purchase", 0) or 0)
        ctr = float(row.get("ctr") or 0.0)
        try:
            cpc = float(row.get("cpc") or 0.0)
            cpm = float(row.get("cpm") or 0.0)
        except Exception:
            cpc = 0.0
            cpm = 0.0
        cpl = float(spend / leads) if leads > 0 and spend > 0 else 0.0
        roas = float(revenue / spend) if spend > 0 else 0.0
        insights_by_campaign[cid] = {
            "impressions": impressions,
            "clicks": clicks,
            "spend": float(round(spend, 2)),
            "leads": leads,
            "purchases": purchases,
            "revenue": float(round(revenue, 2)),
            "ctr": float(round(ctr, 2)),
            "cpc": float(round(cpc, 2)),
            "cpm": float(round(cpm, 2)),
            "cpl": float(round(cpl, 2)),
            "roas": float(round(roas, 2)),
        }

    def _budget_to_float(v):
        if v is None:
            return None
        try:
            return float(v) / 100.0
        except Exception:
            return None

    result: List[Dict[str, Any]] = []
    for c in campaigns_raw:
        cid = c.get("id")
        ins = insights_by_campaign.get(cid, {})
        result.append({
            "meta_campaign_id": cid,
            "name": c.get("name"),
            "status": c.get("status"),
            "effective_status": c.get("effective_status"),
            "objective": c.get("objective"),
            "buying_type": c.get("buying_type"),
            "daily_budget": _budget_to_float(c.get("daily_budget")),
            "lifetime_budget": _budget_to_float(c.get("lifetime_budget")),
            "created_time": c.get("created_time"),
            "start_time": c.get("start_time"),
            "stop_time": c.get("stop_time"),
            "insights": ins,
        })

    result.sort(key=lambda r: (r.get("insights") or {}).get("spend", 0.0), reverse=True)
    return jsonify(result)


@bp.route("/live/<string:meta_campaign_id>", methods=["GET"])
def get_campaign_live(meta_campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    try:
        meta_block = _fetch_meta_campaign_view(
            meta_campaign_id=meta_campaign_id,
            access_token=fb_account["access_token"],
        )
        return jsonify({"meta_campaign_id": meta_campaign_id, "meta": meta_block})
    except Exception as e:
        current_app.logger.exception("Error fetching Meta live campaign view: %s", e)
        return jsonify({"error": str(e)}), 502


@bp.route("/live/<string:meta_campaign_id>/adsets", methods=["GET"])
def list_adsets_live(meta_campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{meta_campaign_id}/adsets"
    fields = ",".join([
        "id", "name", "daily_budget", "lifetime_budget", "effective_status", "start_time", "end_time", "targeting", "billing_event", "optimization_goal",
    ])
    try:
        resp = requests.get(url, params={"fields": fields, "limit": 200, "access_token": access_token}, timeout=30)
        try:
            j = resp.json()
        except Exception:
            j = None
        if not resp.ok:
            return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
        return jsonify(j)
    except Exception as e:
        current_app.logger.exception("Error fetching adsets: %s", e)
        return jsonify({"error": str(e)}), 502


@bp.route("/live/<string:meta_campaign_id>/ads", methods=["GET"])
def list_ads_live(meta_campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{meta_campaign_id}/ads"
    fields = ",".join([
        "id", "name", "status", "effective_status", "adset_id",
        "creative{ id,effective_object_story_id,object_story_spec,thumbnail_url,body,title}",
    ])
    try:
        resp = requests.get(url, params={"fields": fields, "limit": 200, "access_token": access_token}, timeout=30)
        try:
            j = resp.json()
        except Exception:
            j = None
        if not resp.ok:
            return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
        return jsonify(j)
    except Exception as e:
        current_app.logger.exception("Error fetching ads: %s", e)
        return jsonify({"error": str(e)}), 502


@bp.route("/live/ads/<string:ad_id>/preview", methods=["GET"])
def ad_preview(ad_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    ad_format = request.args.get("ad_format", "DESKTOP_FEED_STANDARD")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{ad_id}/previews"
    try:
        resp = requests.get(url, params={"ad_format": ad_format, "access_token": access_token}, timeout=30)
    except Exception as e:
        current_app.logger.exception("Error fetching ad preview: %s", e)
        return jsonify({"error": str(e)}), 502

    try:
        j = resp.json()
    except ValueError:
        return make_response(resp.text, 200, {"Content-Type": "text/html"})

    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502

    for d in j.get("data", []):
        html = d.get("body") or d.get("html") or d.get("story")
        if html:
            return make_response(html, 200, {"Content-Type": "text/html"})

    return jsonify(j)


# ---------- Creatives / Ads endpoints keep same structure, but ensure 'ad_account_id' is formatted where used ----------
# Note: for upload_creative_image / list_creatives_live / create_ad_creative / create_ad_live / update_ad_live
# we will use the formatted ad_account_id helper above when constructing the URL

@bp.route("/live/creatives/upload", methods=["POST"])
def upload_creative_image():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token") or not fb_account.get("ad_account_id"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    if "file" not in request.files:
        return jsonify({"error": "file required"}), 400

    f = request.files["file"]
    access_token = fb_account["access_token"]
    ad_account_id = _format_ad_account_id(fb_account["ad_account_id"])
    if not ad_account_id:
        return jsonify({"error": "invalid_ad_account_id"}), 400

    url = f"{BASE_URL}/{ad_account_id}/adimages"
    files = {"file": (f.filename, f.stream, f.mimetype)}
    data = {"access_token": access_token}

    try:
        resp = requests.post(url, files=files, data=data, timeout=60)
    except Exception as e:
        current_app.logger.exception("Error uploading creative image: %s", e)
        return jsonify({"error": str(e)}), 502

    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "bad_meta_response"}), 502

    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502

    return jsonify(j)


@bp.route("/live/creatives", methods=["GET"])
def list_creatives_live():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token") or not fb_account.get("ad_account_id"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    ad_account_id = _format_ad_account_id(fb_account["ad_account_id"])
    if not ad_account_id:
        return jsonify({"error": "invalid_ad_account_id"}), 400

    url = f"{BASE_URL}/{ad_account_id}/adcreatives"
    fields = "id,name,object_story_spec,thumbnail_url,effective_authorization_category"
    try:
        resp = requests.get(url, params={"fields": fields, "limit": 200, "access_token": access_token}, timeout=30)
        try:
            j = resp.json()
        except Exception:
            j = None
        if not resp.ok:
            return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
        return jsonify(j)
    except Exception as e:
        current_app.logger.exception("Error listing creatives: %s", e)
        return jsonify({"error": str(e)}), 502


@bp.route("/live/creatives", methods=["POST"])
def create_ad_creative():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token") or not fb_account.get("ad_account_id"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    ad_account_id = _format_ad_account_id(fb_account["ad_account_id"])
    page_id = data.get("page_id") or fb_account.get("page_id")
    link_data = data.get("link_data")
    if not page_id or not link_data:
        return jsonify({"error": "page_id and link_data required"}), 400

    object_story_spec = {"page_id": page_id, "link_data": link_data}
    payload = {
        "name": data.get("name", "Creative"),
        "object_story_spec": json.dumps(object_story_spec),
        "access_token": access_token,
    }

    url = f"{BASE_URL}/{ad_account_id}/adcreatives" if ad_account_id else f"{BASE_URL}/adcreatives"
    resp = requests.post(url, data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "non_json_meta_response"}), 502
    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
    return jsonify(j), 201


@bp.route("/live/creatives/<string:creative_id>", methods=["PUT"])
def update_ad_creative(creative_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{creative_id}"
    payload: Dict[str, Any] = {"access_token": access_token}
    if "name" in data:
        payload["name"] = data["name"]
    if "object_story_spec" in data:
        payload["object_story_spec"] = json.dumps(data["object_story_spec"])
    if len(payload) == 1:
        return jsonify({"error": "nothing_to_update"}), 400

    resp = requests.post(url, data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "non_json_meta_response"}), 502
    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
    return jsonify(j)


@bp.route("/live/ads", methods=["POST"])
def create_ad_live():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token") or not fb_account.get("ad_account_id"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    ad_account_id = _format_ad_account_id(fb_account["ad_account_id"])

    adset_id = data.get("adset_id")
    creative_id = data.get("creative_id")
    name = data.get("name", "Ad")
    status = data.get("status", "PAUSED")
    if not adset_id or not creative_id:
        return jsonify({"error": "adset_id and creative_id required"}), 400

    url = f"{BASE_URL}/{ad_account_id}/ads" if ad_account_id else f"{BASE_URL}/ads"
    payload = {
        "name": name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status": status,
        "access_token": access_token,
    }

    resp = requests.post(url, data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "non_json_meta_response"}), 502
    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
    return jsonify(j), 201


@bp.route("/live/ads/<string:ad_id>", methods=["PUT"])
def update_ad_live(ad_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{ad_id}"
    payload: Dict[str, Any] = {"access_token": access_token}
    if "name" in data:
        payload["name"] = data["name"]
    if "status" in data:
        payload["status"] = data["status"]
    if len(payload) == 1:
        return jsonify({"error": "nothing_to_update"}), 400

    resp = requests.post(url, data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "non_json_meta_response"}), 502
    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
    return jsonify(j)


@bp.route("/live/ads/<string:ad_id>/pause", methods=["POST"])
def pause_ad_live(ad_id: str):
    return _change_ad_status(ad_id, "PAUSED")


@bp.route("/live/ads/<string:ad_id>/resume", methods=["POST"])
def resume_ad_live(ad_id: str):
    return _change_ad_status(ad_id, "ACTIVE")


def _change_ad_status(ad_id: str, new_status: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
    if not fb_account or not fb_account.get("access_token"):
        return jsonify({"error": "no_facebook_ad_account"}), 400

    access_token = fb_account["access_token"]
    url = f"{BASE_URL}/{ad_id}"
    payload = {"status": new_status, "access_token": access_token}

    resp = requests.post(url, data=payload, timeout=30)
    try:
        j = resp.json()
    except Exception:
        return jsonify({"error": "non_json_meta_response"}), 502
    if not resp.ok:
        return jsonify({"error": j or resp.text, "status_code": resp.status_code}), 502
    return jsonify(j)


# ---------- Campaign insights (summary + trend) ----------

@bp.route("/<string:campaign_id>/insights", methods=["GET"])
def campaign_insights(campaign_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    date_preset = request.args.get("date_preset", "last_30d")

    fb_account = None
    if workspace_id and user_id:
        try:
            fb_account = _get_fb_account_for_workspace_user(workspace_id, user_id)
        except Exception as e:
            current_app.logger.exception("campaign_insights: error in _get_fb_account_for_workspace_user: %s", e)
            fb_account = None

    if not fb_account or not fb_account.get("access_token"):
        current_app.logger.warning("campaign_insights: no_social_account for workspace_id=%s user_id=%s", workspace_id, user_id)
        return jsonify({"error": "no_social_account"}), 400

    access_token = fb_account["access_token"]

    meta_campaign_id: Optional[str] = None
    c = None
    try:
        db, Campaign = _get_db_and_campaign_model()
        q = db.session.query(Campaign).filter(Campaign.id == campaign_id)
        q = _apply_workspace_and_user_filters(q, Campaign, workspace_id, user_id)
        c = q.one_or_none()
    except Exception as e:
        current_app.logger.exception("campaign_insights: error fetching CRM campaign (will fall back to Meta-only): %s", e)
        c = None

    if c is not None:
        meta_campaign_id = getattr(c, "meta_campaign_id", None) or str(getattr(c, "id", campaign_id))
        current_app.logger.debug("campaign_insights: using CRM campaign %s mapped to Meta id %s", campaign_id, meta_campaign_id)
    else:
        meta_campaign_id = campaign_id
        current_app.logger.debug("campaign_insights: no CRM campaign for id=%s, treating it directly as Meta campaign id", campaign_id)

    try:
        insights_data = _fetch_meta_insights_only(meta_campaign_id=meta_campaign_id, access_token=access_token, date_preset=date_preset)
    except Exception as e:
        current_app.logger.exception("campaign_insights: error fetching Meta insights for meta_campaign_id=%s: %s", meta_campaign_id, e)
        return jsonify({"error": str(e)}), 500

    try:
        trend_data = _fetch_meta_daily_trend(meta_campaign_id=meta_campaign_id, access_token=access_token, date_preset=date_preset)
    except Exception as e:
        current_app.logger.exception("campaign_insights: error fetching Meta trend for meta_campaign_id=%s: %s", meta_campaign_id, e)
        trend_data = []

    return jsonify({
        "campaign_id": campaign_id,
        "meta_campaign_id": meta_campaign_id,
        "date_preset": date_preset,
        "insights": insights_data,
        "trend": trend_data,
    })
