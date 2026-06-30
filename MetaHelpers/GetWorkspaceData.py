# MetaHelpers/GetWorkspaceData.py
import os
import json
import requests
from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import List, Dict, Any, Optional
from flask import Blueprint, request, jsonify, current_app

from sqlalchemy import cast
from sqlalchemy.types import String, Integer
from sqlalchemy.exc import SQLAlchemyError

FB_API_VERSION = os.getenv("FB_API_VERSION", "v22.0")
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

bp = Blueprint("meta_consolidation", __name__, url_prefix="/api/meta")


def iso_date_or_none(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None


def _col_is_int_like(col):
    try:
        t = getattr(col, "type", None)
        return isinstance(t, Integer)
    except Exception:
        return False


def get_social_accounts_for_workspace_user(workspace_id: Any, user_id: Any) -> List[dict]:
    """
    Return list of dicts: {access_token, ad_account_id, page_id, social_id, provider_user_id}
    workspace_id and user_id may be ints or strings depending on your DB schema.
    """
    db = current_app.db
    crm_models = getattr(current_app, "crm_models", {}) or {}

    SocialAccount = (
        crm_models.get("SocialAccount")
        or crm_models.get("Social_Account")
        or getattr(current_app, "SocialAccount", None)
    )

    if SocialAccount is None:
        # Fallback: try direct import (if you ever expose it this way)
        try:
            from models import SocialAccount as SA  # type: ignore
            SocialAccount = SA
        except Exception:
            current_app.logger.debug(
                "No SocialAccount model available on current_app.crm_models or models."
            )
            return []

    # Normalize workspace_id & user_id to strings where appropriate
    ws_str: Optional[str] = str(workspace_id) if workspace_id is not None else None
    user_str: Optional[str] = str(user_id) if user_id is not None else None

    ws_col = getattr(SocialAccount, "workspace_id", None)
    user_col = getattr(SocialAccount, "user_id", None)

    try:
        q = db.session.query(SocialAccount).filter(
            SocialAccount.provider == "facebook"
        )

        # workspace_id – treat as TEXT via cast to String and compare to ws_str
        if ws_col is not None and ws_str is not None:
            q = q.filter(cast(ws_col, String) == ws_str)

        # user_id – be type-aware: if the column is int-like, compare as int;
        # otherwise, cast to string as well.
        if user_col is not None and user_id is not None:
            if _col_is_int_like(user_col):
                try:
                    user_int = int(user_id)
                    q = q.filter(user_col == user_int)
                except Exception:
                    q = q.filter(cast(user_col, String) == user_str)
            else:
                q = q.filter(cast(user_col, String) == user_str)

        rows = (
            q.order_by(SocialAccount.updated_at.desc())
             .all()
        )

        return [
            {
                "social_id": getattr(r, "id", None),
                "access_token": getattr(r, "access_token", None),
                "ad_account_id": getattr(r, "ad_account_id", None),
                "page_id": getattr(r, "provider_user_id", None),
                "raw_row": r,
            }
            for r in rows
        ]

    except SQLAlchemyError as e:
        current_app.logger.exception(
            "get_social_accounts_for_workspace_user DB error: %s", e, exc_info=True
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return []


def discover_ad_accounts_for_token(access_token: str, timeout=8, api_version: Optional[str] = None) -> List[str]:
    api_version = api_version or FB_API_VERSION
    url = f"https://graph.facebook.com/{api_version}/me/adaccounts"
    try:
        resp = requests.get(url, params={"access_token": access_token}, timeout=timeout)
        j = resp.json()
    except Exception as e:
        current_app.logger.debug("Failed to call /me/adaccounts: %s", e, exc_info=True)
        return []
    if not resp.ok:
        current_app.logger.debug("adaccounts call returned non-ok: %s", j.get("error"))
        return []
    data = j.get("data", []) or []
    out = []
    for item in data:
        # item may contain 'id' (often 'act_<id>') or 'account_id' (numeric)
        aid = item.get("id") or item.get("account_id")
        if aid:
            out.append(aid)
    return out


# ensure ad account path uses act_ prefix
def build_ad_account_path(raw_account_id: str) -> str:
    """
    Given a DB-stored ad account id (could be '1234' or 'act_1234' or 'act_1234...'),
    return the Graph path component that must be used in URLs: 'act_1234'.
    """
    if raw_account_id is None:
        raise ValueError("ad_account_id is required")
    s = str(raw_account_id).strip()
    if not s:
        raise ValueError("ad_account_id is empty")
    return s if s.lower().startswith("act_") else f"act_{s}"



class FacebookTokenError(Exception):
    pass

def call_meta_insights(ad_account_id: str, access_token: str, params: Dict[str, Any], timeout=30, api_version: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Calls /{ad_account}/insights — ensures the account path is prefixed with 'act_'.
    Returns aggregated rows (handles paging).
    """
    try:
        acct_path = build_ad_account_path(ad_account_id)
    except Exception:
        acct_path = ad_account_id

    base_url = f"https://graph.facebook.com/{api_version}" if api_version else BASE_URL
    url = f"{base_url}/{acct_path}/insights"
    safe_params = {k: ("***" if k == "access_token" else v) for k, v in params.items()}
    current_app.logger.debug("Calling Meta insights for %s params=%s", acct_path, safe_params)

    all_rows = []
    resp = requests.get(url, params=params, timeout=timeout)
    try:
        j = resp.json()
    except Exception:
        current_app.logger.exception("Non-JSON response from Meta for account %s: %s", acct_path, resp.text[:400])
        raise RuntimeError("Non-JSON response from Meta")

    if not resp.ok:
        err = j.get("error", {})
        code = err.get("code")
        msg = err.get("message", "unknown error")
        if code == 190:
            raise FacebookTokenError(f"Facebook token issue (code 190) - {msg}")
        raise RuntimeError(f"Meta API error: {msg}")

    data = j.get("data", [])
    all_rows.extend(data)

    paging = j.get("paging", {})
    next_url = paging.get("next")
    while next_url:
        current_app.logger.debug("Fetching next page for ad_account %s", acct_path)
        resp = requests.get(next_url, timeout=timeout)
        try:
            j = resp.json()
        except Exception:
            current_app.logger.exception("Non-JSON next-page response from Meta")
            break
        if not resp.ok:
            current_app.logger.warning("Meta next-page error: %s", j.get("error", {}))
            break
        data = j.get("data", [])
        if not data:
            break
        all_rows.extend(data)
        paging = j.get("paging", {})
        next_url = paging.get("next")

    return all_rows


def sum_int(x):
    try:
        return int(x or 0)
    except:
        try:
            return int(float(x))
        except:
            return 0


def sum_decimal(x):
    try:
        return Decimal(str(x or 0))
    except:
        return Decimal(0)


def actions_to_dict(actions):
    out = {}
    if not actions:
        return out
    for a in actions:
        at = a.get("action_type") or a.get("action_type")
        val = a.get("value") or a.get("values") or 0
        try:
            v = float(val) if val is not None else 0
        except:
            try:
                v = float(a.get("value", 0) or 0)
            except:
                v = 0
        out[at] = out.get(at, 0) + v
    return out

def sum_int_or_none(x):
    try:
        return int(x)
    except Exception:
        return None


def flatten_and_aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    campaigns_map = {}

    for r in rows:
        camp_id = r.get("campaign_id") or "unknown"
        camp_name = r.get("campaign_name") or ""

        impressions = sum_int(r.get("impressions"))
        clicks = sum_int(r.get("clicks"))
        spend = sum_decimal(r.get("spend"))
        reach = sum_int_or_none(r.get("reach"))

        actions = actions_to_dict(r.get("actions") or [])
        action_values = actions_to_dict(r.get("action_values") or [])

        leads = int(actions.get("lead", 0))
        purchases = int(actions.get("purchase", 0))
        link_clicks = int(actions.get("link_click", 0))
        revenue = Decimal(action_values.get("purchase", 0))

        if camp_id not in campaigns_map:
            campaigns_map[camp_id] = {
                "id": camp_id,
                "name": camp_name,
                "impressions": impressions,
                "clicks": clicks,
                "spend": spend,
                "leads": leads,
                "purchases": purchases,
                "link_clicks": link_clicks,
                "revenue": revenue,
                "reach": reach or 0,
            }
        else:
            c = campaigns_map[camp_id]
            c["impressions"] += impressions
            c["clicks"] += clicks
            c["spend"] += spend
            c["leads"] += leads
            c["purchases"] += purchases
            c["link_clicks"] += link_clicks
            c["revenue"] += revenue

            # 🔥 reach → take MAX
            if reach:
                c["reach"] = max(c["reach"], reach)

    totals = {
        "impressions": 0,
        "clicks": 0,
        "spend": Decimal(0),
        "leads": 0,
        "revenue": Decimal(0),
        "reach": 0,
    }

    campaigns = []

    for c in campaigns_map.values():
        impressions = c["impressions"]
        clicks = c["clicks"]
        spend = c["spend"]
        leads = c["leads"]
        reach = c["reach"]
        revenue = c["revenue"]

        ctr = (Decimal(clicks) / Decimal(impressions) * 100) if impressions else 0
        cpc = (spend / clicks) if clicks else 0
        cpm = (spend / (Decimal(impressions) / 1000)) if impressions else 0
        cpl = (spend / leads) if leads else 0
        roas = (revenue / spend) if spend else 0

        campaigns.append({
            "id": c["id"],
            "name": c["name"],
            "impressions": impressions,
            "reach": reach,
            "clicks": clicks,
            "spend": float(round(spend, 2)),
            "leads": leads,
            "purchases": c["purchases"],
            "link_clicks": c["link_clicks"],
            "revenue": float(round(revenue, 2)),
            "ctr_pct": float(round(ctr, 2)),
            "cpc": float(round(cpc, 2)),
            "cpm": float(round(cpm, 2)),
            "cpl": float(round(cpl, 2)),
            "roas": float(round(roas, 2)),
        })

        totals["impressions"] += impressions
        totals["clicks"] += clicks
        totals["spend"] += spend
        totals["leads"] += leads
        totals["revenue"] += revenue
        totals["reach"] = max(totals["reach"], reach)

    totals_out = {
        "impressions": totals["impressions"],
        "clicks": totals["clicks"],
        "spend": float(round(totals["spend"], 2)),
        "leads": totals["leads"],
        "revenue": float(round(totals["revenue"], 2)),
        "reach": totals["reach"],
        "cpl": float(round((totals["spend"] / totals["leads"]) if totals["leads"] else 0, 2)),
        "roas": float(round((totals["revenue"] / totals["spend"]) if totals["spend"] else 0, 2)),
    }

    campaigns.sort(key=lambda x: x["spend"], reverse=True)

    return {"campaigns": campaigns, "totals": totals_out}

def upsert_campaign_metrics_to_db(metrics_rows: List[Dict[str, Any]], workspace_id: str):
    try:
        CampaignMetric = getattr(current_app, "analytics_model", None)
        db = current_app.db
        if not CampaignMetric:
            current_app.logger.debug("No analytics_model attached - skipping DB upsert")
            return

        for r in metrics_rows:
            campaign_id = r.get("id")
            date_obj = date.today()
            existing = db.session.query(CampaignMetric).filter(
                CampaignMetric.campaign_id == campaign_id,
                CampaignMetric.date == date_obj
            ).one_or_none()
            if existing:
                existing.impressions = r.get("impressions", 0)
                existing.clicks = r.get("clicks", 0)
                existing.spend = r.get("spend", 0)
                existing.leads = r.get("leads", 0)
                existing.revenue = r.get("revenue", 0)
                existing.cpl = r.get("cpl", 0)
                existing.roas = r.get("roas", 0)
                existing.raw_payload = json.dumps(r)
                db.session.add(existing)
            else:
                obj = CampaignMetric(
                    campaign_id=campaign_id,
                    date=date_obj,
                    impressions=r.get("impressions", 0),
                    clicks=r.get("clicks", 0),
                    spend=r.get("spend", 0),
                    leads=r.get("leads", 0),
                    revenue=r.get("revenue", 0),
                    cpl=r.get("cpl", 0),
                    roas=r.get("roas", 0),
                    raw_payload=r
                )
                db.session.add(obj)
        db.session.commit()
    except Exception as e:
        current_app.logger.exception("Failed to upsert campaign metrics: %s", e)
        try:
            current_app.db.session.rollback()
        except Exception:
            pass


@bp.route("/consolidated-campaigns", methods=["GET"])
def consolidated_campaigns():
    """
    GET /api/meta/consolidated-campaigns?workspace_id=8&user_id=2
    Optional:
      - ?meta_only=true&access_token=<token>  => query Meta using provided token directly (no DB SocialAccount required)
      - ?date_preset=last_7d
      - ?since=YYYY-MM-DD&until=YYYY-MM-DD
      - ?persist=true  => upsert to analytics model if configured
    Behaviour:
      - For each SocialAccount returned by the DB query we WILL discover all ad accounts accessible by that token
        (via /me/adaccounts) and call insights for *each discovered ad account*.
      - Falls back to stored ad_account_id if discovery returns none.
    """
    # parse workspace/user OR meta_only mode
    meta_only = request.args.get("meta_only", "false").lower() == "true"
    ws_raw = request.args.get("workspace_id")
    user_raw = request.args.get("user_id")

    # convert possible date params
    date_preset = request.args.get("date_preset")
    time_increment = request.args.get("time_increment", "all_days")
    since_raw = request.args.get("since")
    until_raw = request.args.get("until")

    since = iso_date_or_none(since_raw)
    until = iso_date_or_none(until_raw)
    params_common = {}

    if date_preset and (date_preset != ""):
        params_common["date_preset"] = date_preset
    elif since and until:
        params_common["time_range"] = json.dumps({"since": since.isoformat(), "until": until.isoformat()})
    else:
        params_common["date_preset"] = "last_30d"

    params_common["time_increment"] = time_increment
    params_common["level"] = "campaign"
    fields = [
        "campaign_id", "campaign_name", "objective",
        "impressions", "reach", "frequency", "clicks", "unique_clicks", "ctr", "unique_ctr",
        "spend", "cpc", "cpm", "cpp", "actions", "action_values", "date_start", "date_stop"
    ]
    params_common["fields"] = ",".join(fields)

    # Per-tenant Graph API version (falls back to env / module default for
    # T0000 / unconfigured / when no workspace_id is supplied).
    from tenant.integration import get_tenant_meta_config
    tenant_api_version = (
        get_tenant_meta_config(workspace_id=ws_raw).fb_api_version or FB_API_VERSION
    )

    social_accounts = []

    # meta_only mode: use provided access_token and discover accounts directly
    if meta_only:
        access_token = request.args.get("access_token")
        if not access_token:
            return jsonify({"error": "meta_only requires access_token query param"}), 400
        # Discover adaccounts for this token
        discovered = discover_ad_accounts_for_token(access_token, api_version=tenant_api_version)
        if not discovered:
            return jsonify({"error": "no_ad_accounts_found_for_token"}), 404
        # Build a synthetic social_accounts list (single entry)
        social_accounts = [{
            "social_id": None,
            "access_token": access_token,
            "ad_account_id": None,
            "page_id": None
        }]
    else:
        # normal DB-backed mode. workspace_id and user_id required
        try:
            workspace_id = int(ws_raw) if ws_raw is not None else None
            user_id = int(user_raw) if user_raw is not None else None
        except Exception:
            return jsonify({"error": "workspace_id and user_id must be integer-like"}), 400

        social_accounts = get_social_accounts_for_workspace_user(workspace_id, user_id)
        if not social_accounts:
            return jsonify({"error": "No Facebook SocialAccount found for given workspace/user"}), 404

    all_rows = []
    errors = []
    accounts_used = []

    for acct in social_accounts:
        social_id = acct.get("social_id")
        stored_ad_account_id = acct.get("ad_account_id")
        access_token = acct.get("access_token")
        page_id = acct.get("page_id")

        if not access_token:
            errors.append({"social_id": social_id, "error": "missing access_token"})
            continue

        # Discover ad accounts from token first (preferred)
        discovered_accounts = []
        try:
            discovered_accounts = discover_ad_accounts_for_token(access_token, api_version=tenant_api_version)
        except Exception as e:
            current_app.logger.debug("Auto-discovery of ad accounts failed for social_id %s: %s", social_id, e, exc_info=True)
            discovered_accounts = []

        candidate_accounts: List[str] = []

        # Prefer discovered accounts; if none discovered, fall back to stored ad_account_id
        if discovered_accounts:
            for a in discovered_accounts:
                norm_a = build_ad_account_path(a)
                if norm_a not in candidate_accounts:
                    candidate_accounts.append(norm_a)
                    
        if stored_ad_account_id:
            norm_stored = build_ad_account_path(stored_ad_account_id)
            if norm_stored not in candidate_accounts:
                candidate_accounts.append(norm_stored)

        if not candidate_accounts:
            errors.append({"social_id": social_id, "error": "no_ad_accounts_found"})
            accounts_used.append({"social_id": social_id, "ad_account_id": None, "page_id": page_id})
            continue

        # For each ad account, call insights and aggregate
        for acct_id in candidate_accounts:
            params = dict(params_common)
            params["access_token"] = access_token
            try:
                rows = call_meta_insights(ad_account_id=acct_id, access_token=access_token, params=params, api_version=tenant_api_version)
                if rows:
                    all_rows.extend(rows)
                accounts_used.append({"social_id": social_id, "ad_account_id": acct_id, "page_id": page_id})
            except FacebookTokenError as fte:
                current_app.logger.warning("Token error for account %s: %s", acct_id, fte)
                errors.append({"social_id": social_id, "ad_account_id": acct_id, "error": str(fte), "code": "token_expired"})
            except Exception as e:
                current_app.logger.exception("Failed fetching insights for account %s: %s", acct_id, e)
                errors.append({"social_id": social_id, "ad_account_id": acct_id, "error": str(e)})

    # consolidate results
    consolidated = flatten_and_aggregate(all_rows)

    # Build timeseries (overview + per-campaign)
    def _normalize_date_key(dval):
        if not dval:
            return datetime.utcnow().date().isoformat()
        if isinstance(dval, (datetime, date)):
            return dval.date().isoformat() if isinstance(dval, datetime) else dval.isoformat()
        try:
            return datetime.fromisoformat(str(dval)).date().isoformat()
        except Exception:
            try:
                return datetime.strptime(str(dval)[:10], "%Y-%m-%d").date().isoformat()
            except Exception:
                return str(dval)[:10]

    overview_by_date: Dict[str, Dict[str, Any]] = {}
    campaign_by_date: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for r in (all_rows or []):
        date_key = (
            r.get("date_start") or r.get("date") or r.get("date_stop") or r.get("day") or None
        )
        date_key = _normalize_date_key(date_key)
        impressions = sum_int(r.get("impressions"))
        clicks = sum_int(r.get("clicks"))
        spend = sum_decimal(r.get("spend") or r.get("spend", 0))
        actions = actions_to_dict(r.get("actions") or [])
        leads = int(actions.get("lead", actions.get("leadgen", 0) or 0))
        campaign_id = r.get("campaign_id") or r.get("campaign") or r.get("id") or "unknown"

        od = overview_by_date.setdefault(date_key, {"date": date_key, "impressions": 0, "clicks": 0, "spend": Decimal(0), "leads": 0})
        od["impressions"] += impressions
        od["clicks"] += clicks
        od["spend"] += spend
        od["leads"] += leads

        camp_map = campaign_by_date.setdefault(str(campaign_id), {})
        cd = camp_map.setdefault(date_key, {"date": date_key, "impressions": 0, "clicks": 0, "spend": Decimal(0), "leads": 0})
        cd["impressions"] += impressions
        cd["clicks"] += clicks
        cd["spend"] += spend
        cd["leads"] += leads

    overview_dates = sorted(overview_by_date.keys())
    overview_series = []
    recharts_overview = []
    for d in overview_dates:
        o = overview_by_date[d]
        impressions = o["impressions"]
        clicks = o["clicks"]
        spend = o["spend"]
        leads = o["leads"]
        ctr_pct = float((Decimal(clicks) / Decimal(impressions) * 100)) if impressions else 0.0
        cpm = float((spend / (Decimal(impressions) / Decimal(1000)))) if impressions else 0.0
        overview_series.append({
            "date": d,
            "impressions": int(impressions),
            "clicks": int(clicks),
            "spend": float(round(spend, 2)),
            "leads": int(leads),
            "ctr_pct": float(round(ctr_pct, 2)),
            "cpm": float(round(cpm, 2)),
        })
        recharts_overview.append({
            "name": d,
            "spend": float(round(spend, 2)),
            "leads": int(leads),
            "impressions": int(impressions),
            "clicks": int(clicks),
        })

    campaign_series = {}
    for camp_id, dates_map in campaign_by_date.items():
        sorted_dates = sorted(dates_map.keys())
        arr = []
        for d in sorted_dates:
            cd = dates_map[d]
            impressions = cd["impressions"]
            clicks = cd["clicks"]
            spend = cd["spend"]
            leads = cd["leads"]
            ctr_pct = float((Decimal(clicks) / Decimal(impressions) * 100)) if impressions else 0.0
            cpm = float((spend / (Decimal(impressions) / Decimal(1000)))) if impressions else 0.0
            arr.append({
                "date": d,
                "impressions": int(impressions),
                "clicks": int(clicks),
                "spend": float(round(spend, 2)),
                "leads": int(leads),
                "ctr_pct": float(round(ctr_pct, 2)),
                "cpm": float(round(cpm, 2)),
            })
        campaign_series[camp_id] = arr

    # merge local Campaign names/status if available (non-blocking)
    try:
        crm_models = getattr(current_app, "crm_models", {})
        Campaign = crm_models.get("Campaign")
        if Campaign:
            db = current_app.db
            workspace_col_is_int = False
            try:
                col = Campaign.__table__.columns.get("workspace_id")
                if col is not None:
                    workspace_col_is_int = isinstance(col.type, Integer)
            except Exception:
                workspace_col_is_int = False

            local_campaigns = []
            try:
                if workspace_col_is_int and ws_raw is not None:
                    try:
                        local_campaigns = db.session.query(Campaign).filter(Campaign.workspace_id == int(ws_raw)).all()
                    except Exception:
                        local_campaigns = db.session.query(Campaign).filter(cast(Campaign.workspace_id, String) == str(ws_raw)).all()
                else:
                    try:
                        local_campaigns = db.session.query(Campaign).filter(cast(Campaign.workspace_id, String) == str(ws_raw)).all()
                    except Exception:
                        try:
                            local_campaigns = db.session.query(Campaign).filter(Campaign.workspace_id == int(ws_raw)).all()
                        except Exception:
                            local_campaigns = []
            except Exception as qe:
                current_app.logger.debug("Campaign local fetch attempt failed, rolling back and continuing: %s", qe, exc_info=True)
                try:
                    db.session.rollback()
                except Exception:
                    pass
                local_campaigns = []

            local_map = {c.id: c for c in (local_campaigns or [])}
            for c in consolidated["campaigns"]:
                local = local_map.get(c["id"])
                if local:
                    c["name"] = getattr(local, "name", c.get("name"))
                    if getattr(local, "status", None):
                        c["status"] = getattr(local, "status")
    except Exception:
        current_app.logger.debug("No local Campaign merge or failed to merge", exc_info=True)

    try:
        upsert_flag = request.args.get("persist", "false").lower() == "true"
        if upsert_flag:
            upsert_campaign_metrics_to_db(consolidated["campaigns"], str(ws_raw or ""))
    except Exception:
        current_app.logger.exception("persist upsert failed")

    result = {
        "meta": {
            "workspace_id": str(ws_raw) if ws_raw is not None else None,
            "user_id": str(user_raw) if user_raw is not None else None,
            "accounts_used": accounts_used,
            "errors": errors,
            "requested": {
                "date_preset": params_common.get("date_preset"),
                "time_range": json.loads(params_common["time_range"]) if params_common.get("time_range") else None,
                "time_increment": params_common.get("time_increment")
            }
        },
        "totals": consolidated["totals"],
        "campaigns": consolidated["campaigns"],
        "chart_series": {
            "overview": overview_series,
            "campaigns": campaign_series
        },
        "recharts_overview": recharts_overview
    }

    return jsonify(result)
