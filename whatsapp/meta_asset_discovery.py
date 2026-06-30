"""
Canonical Meta WhatsApp asset discovery
======================================

Single module for resolving Business Manager → WABA → phone numbers and
`subscribed_apps` state. All onboarding paths should use this instead of
treating Meta Business ID as WABA ID or assuming `/me` nested pagination is complete.

Graph flow (canonical):
  1. GET /vX.Y/me/businesses (paginated)
  2. GET /vX.Y/{business-id}/owned_whatsapp_business_accounts (paginated)
  3. GET /vX.Y/{waba-id}/phone_numbers (paginated)
  4. GET /vX.Y/{waba-id}/subscribed_apps

Supplements (merged, not replacements):
  - GET /me/client_whatsapp_business_accounts (solution partner / client assets)
  - debug_token granular_scopes target_ids when business portfolio calls fail
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_API_VERSION = os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION", "v24.0")

PHONE_FIELDS = "id,display_phone_number,verified_name,quality_rating,name_status"
WABA_LIST_FIELDS = "id,name"
BUSINESS_LIST_FIELDS = "id,name"


class DiscoveryAmbiguousError(Exception):
    """More than one (business, waba, phone) binding matches the token without sufficient hints."""

    def __init__(self, message: str, discovery: Dict[str, Any]):
        super().__init__(message)
        self.discovery = discovery


def _graph_root(api_version: str) -> str:
    return f"https://graph.facebook.com/{api_version}"


def _get_json(url: str, *, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> Dict[str, Any]:
    r = requests.get(url, params=params or {}, timeout=timeout)
    try:
        return r.json()
    except Exception:
        return {"error": {"message": f"invalid_json_http_{r.status_code}"}}


def _paged_data(
    api_version: str,
    path: str,
    access_token: str,
    *,
    extra_params: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Collect all `data` items for a Graph edge, following `paging.next`.
    Returns (items, raw_error_payloads).
    """
    items: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    root = _graph_root(api_version)
    first_url = f"{root}/{path.lstrip('/')}"
    params: Dict[str, Any] = {"access_token": access_token, "limit": 100}
    if extra_params:
        params.update(extra_params)

    url: Optional[str] = first_url
    use_params = True
    while url:
        if use_params:
            j = _get_json(url, params=params, timeout=timeout)
        else:
            j = _get_json(url, params=None, timeout=timeout)
        if "error" in j:
            errors.append(j)
            break
        batch = j.get("data") or []
        items.extend(batch)
        next_url = (j.get("paging") or {}).get("next")
        if next_url:
            url = next_url
            use_params = False
        else:
            url = None
    return items, errors


def fetch_token_debug_sanitized(
    input_token: str,
    app_id: str,
    app_secret: str,
    api_version: str,
    *,
    timeout: int = 30,
) -> Dict[str, Any]:
    """debug_token fields useful for ops; never include app_secret."""
    root = _graph_root(api_version)
    j = _get_json(
        f"{root}/debug_token",
        params={
            "input_token": input_token,
            "access_token": f"{app_id}|{app_secret}",
        },
        timeout=timeout,
    )
    if "error" in j:
        return {"ok": False, "error": j.get("error"), "app_access_token_prefix": f"{app_id}|" + "***"}
    data = j.get("data") or {}
    return {
        "ok": bool(data.get("is_valid")),
        "is_valid": data.get("is_valid"),
        "app_id": data.get("app_id"),
        "user_id": data.get("user_id"),
        "type": data.get("type"),
        "application": data.get("application"),
        "expires_at": data.get("expires_at"),
        "data_access_expires_at": data.get("data_access_expires_at"),
        "scopes": data.get("scopes") or [],
        "granular_scopes": data.get("granular_scopes") or [],
        "missing_scopes_sample": None,
    }


def _subscribed_apps_for_waba(
    waba_id: str,
    access_token: str,
    api_version: str,
    *,
    timeout: int = 20,
) -> Dict[str, Any]:
    root = _graph_root(api_version)
    j = _get_json(
        f"{root}/{waba_id}/subscribed_apps",
        params={"access_token": access_token},
        timeout=timeout,
    )
    if "error" in j:
        return {"ok": False, "error": j.get("error"), "data": []}
    apps = j.get("data") or []
    return {"ok": True, "data": apps, "raw": j}


def _app_id_in_subscribed_list(apps: List[Dict[str, Any]], our_app_id: Optional[str]) -> Optional[bool]:
    if not our_app_id:
        return None
    sid = str(our_app_id).strip()
    for row in apps:
        aid = row.get("id") or (row.get("whatsapp_business_api_data") or {}).get("id")
        if aid is not None and str(aid) == sid:
            return True
    return False if apps else None


def _merge_client_wabas(
    api_version: str,
    access_token: str,
    *,
    timeout: int = 30,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """GET /me/client_whatsapp_business_accounts — Tech Provider style assets."""
    return _paged_data(
        api_version,
        "me/client_whatsapp_business_accounts",
        access_token,
        extra_params={"fields": WABA_LIST_FIELDS},
        timeout=timeout,
    )


def _wabas_from_debug_granular(debug: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for scope in debug.get("granular_scopes") or []:
        if scope.get("scope") in (
            "whatsapp_business_management",
            "whatsapp_business_messaging",
        ):
            for tid in scope.get("target_ids") or []:
                if tid and str(tid) not in out:
                    out.append(str(tid))
    return out


def discover_whatsapp_assets(
    access_token: str,
    *,
    api_version: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    hints: Optional[Dict[str, Optional[str]]] = None,
    include_debug_token: bool = True,
    include_subscribed_apps: bool = True,
    include_client_waba_edge: bool = True,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Full portfolio discovery with pagination and webhook subscription visibility.

    hints: optional keys business_id, waba_id, phone_number_id (strings) to narrow resolution.
    """
    ver = api_version or DEFAULT_API_VERSION
    aid = app_id or os.getenv("META_APP_ID") or os.getenv("FB_APP_ID")
    asec = app_secret or os.getenv("META_APP_SECRET") or os.getenv("FB_APP_SECRET")

    hints = hints or {}
    hb = (hints.get("business_id") or "").strip() or None
    hw = (hints.get("waba_id") or "").strip() or None
    hp = (hints.get("phone_number_id") or "").strip() or None

    token_debug: Optional[Dict[str, Any]] = None
    if include_debug_token and aid and asec:
        try:
            token_debug = fetch_token_debug_sanitized(access_token, aid, asec, ver, timeout=timeout)
        except Exception as e:
            logger.warning("debug_token fetch failed: %s", e)
            token_debug = {"ok": False, "error": str(e)}

    businesses_out: List[Dict[str, Any]] = []
    portfolio_errors: List[Dict[str, Any]] = []

    # 1) /me/businesses
    biz_items, biz_err = _paged_data(
        ver,
        "me/businesses",
        access_token,
        extra_params={"fields": BUSINESS_LIST_FIELDS},
        timeout=timeout,
    )
    portfolio_errors.extend(biz_err)

    seen_waba: set = set()

    for biz in biz_items:
        bid = str(biz.get("id") or "")
        if hb and bid != hb:
            continue
        bname = biz.get("name")
        # 2) owned_whatsapp_business_accounts
        waba_items, waba_err = _paged_data(
            ver,
            f"{bid}/owned_whatsapp_business_accounts",
            access_token,
            extra_params={"fields": WABA_LIST_FIELDS},
            timeout=timeout,
        )
        portfolio_errors.extend(waba_err)
        wabas_struct: List[Dict[str, Any]] = []
        for waba in waba_items:
            wid = str(waba.get("id") or "")
            if not wid or wid in seen_waba:
                continue
            if hw and wid != hw:
                continue
            seen_waba.add(wid)
            wname = waba.get("name")
            phones, ph_err = _paged_data(
                ver,
                f"{wid}/phone_numbers",
                access_token,
                extra_params={"fields": PHONE_FIELDS},
                timeout=timeout,
            )
            portfolio_errors.extend(ph_err)
            if hp:
                phones = [p for p in phones if str(p.get("id") or "") == hp]
            subs: Dict[str, Any] = {"skipped": True}
            our_sub: Optional[bool] = None
            if include_subscribed_apps:
                subs = _subscribed_apps_for_waba(wid, access_token, ver, timeout=timeout)
                our_sub = _app_id_in_subscribed_list(subs.get("data") or [], str(aid) if aid else None)
            wabas_struct.append(
                {
                    "waba_id": wid,
                    "waba_name": wname,
                    "phone_numbers": phones,
                    "subscribed_apps": subs,
                    "our_app_subscribed": our_sub,
                }
            )
        businesses_out.append({"business_id": bid, "business_name": bname, "wabas": wabas_struct})

    # debug_token WABA targets (when businesses edge empty or missing WABA)
    if aid and asec and token_debug and token_debug.get("granular_scopes") is not None:
        for wid in _wabas_from_debug_granular(token_debug):
            if hw and wid != hw:
                continue
            if wid in seen_waba:
                continue
            seen_waba.add(wid)
            phones, ph_err = _paged_data(
                ver,
                f"{wid}/phone_numbers",
                access_token,
                extra_params={"fields": PHONE_FIELDS},
                timeout=timeout,
            )
            portfolio_errors.extend(ph_err)
            if hp:
                phones = [p for p in phones if str(p.get("id") or "") == hp]
            subs = {"skipped": not include_subscribed_apps}
            our_sub = None
            if include_subscribed_apps:
                subs = _subscribed_apps_for_waba(wid, access_token, ver, timeout=timeout)
                our_sub = _app_id_in_subscribed_list(subs.get("data") or [], str(aid) if aid else None)
            businesses_out.append(
                {
                    "business_id": None,
                    "business_name": None,
                    "source": "debug_token_granular",
                    "wabas": [
                        {
                            "waba_id": wid,
                            "waba_name": None,
                            "phone_numbers": phones,
                            "subscribed_apps": subs,
                            "our_app_subscribed": our_sub,
                        }
                    ],
                }
            )

    # client_whatsapp_business_accounts
    client_rows: List[Dict[str, Any]] = []
    if include_client_waba_edge:
        try:
            cw, cw_err = _merge_client_wabas(ver, access_token, timeout=timeout)
            portfolio_errors.extend(cw_err)
            for waba in cw:
                wid = str(waba.get("id") or "")
                if not wid or wid in seen_waba:
                    continue
                if hw and wid != hw:
                    continue
                seen_waba.add(wid)
                wname = waba.get("name")
                phones, ph_err = _paged_data(
                    ver,
                    f"{wid}/phone_numbers",
                    access_token,
                    extra_params={"fields": PHONE_FIELDS},
                    timeout=timeout,
                )
                portfolio_errors.extend(ph_err)
                if hp:
                    phones = [p for p in phones if str(p.get("id") or "") == hp]
                subs = {"skipped": not include_subscribed_apps}
                our_sub = None
                if include_subscribed_apps:
                    subs = _subscribed_apps_for_waba(wid, access_token, ver, timeout=timeout)
                    our_sub = _app_id_in_subscribed_list(subs.get("data") or [], str(aid) if aid else None)
                client_rows.append(
                    {
                        "waba_id": wid,
                        "waba_name": wname,
                        "phone_numbers": phones,
                        "subscribed_apps": subs,
                        "our_app_subscribed": our_sub,
                    }
                )
        except Exception as e:
            logger.warning("client_whatsapp_business_accounts merge failed: %s", e)

    if client_rows:
        businesses_out.append(
            {
                "business_id": None,
                "business_name": None,
                "source": "client_whatsapp_business_accounts",
                "wabas": client_rows,
            }
        )

    # Flat candidates
    candidates: List[Dict[str, Any]] = []
    for b in businesses_out:
        bid = b.get("business_id")
        bname = b.get("business_name")
        for w in b.get("wabas") or []:
            wid = w.get("waba_id")
            wname = w.get("waba_name")
            for p in w.get("phone_numbers") or []:
                pid = str(p.get("id") or "")
                if not pid:
                    continue
                candidates.append(
                    {
                        "business_id": bid,
                        "business_name": bname,
                        "waba_id": wid,
                        "waba_name": wname,
                        "phone_number_id": pid,
                        "display_phone_number": p.get("display_phone_number"),
                        "verified_name": p.get("verified_name"),
                        "quality_rating": p.get("quality_rating"),
                        "name_status": p.get("name_status"),
                        "our_app_subscribed": w.get("our_app_subscribed"),
                        "subscribed_apps": w.get("subscribed_apps"),
                    }
                )

    # WABAs with zero phones (still useful for diagnostics)
    wabas_without_phones: List[Dict[str, str]] = []
    for b in businesses_out:
        for w in b.get("wabas") or []:
            if not (w.get("phone_numbers") or []):
                wabas_without_phones.append(
                    {
                        "business_id": str(b.get("business_id") or ""),
                        "waba_id": str(w.get("waba_id") or ""),
                    }
                )

    counts = {
        "businesses": len([b for b in businesses_out if b.get("wabas")]),
        "wabas": sum(len(b.get("wabas") or []) for b in businesses_out),
        "phones": len(candidates),
    }

    narrowed = list(candidates)
    if hb:
        narrowed = [
            c
            for c in narrowed
            if c.get("business_id") is None or str(c.get("business_id") or "") == hb
        ]
    if hw:
        narrowed = [c for c in narrowed if str(c.get("waba_id") or "") == hw]
    if hp:
        narrowed = [c for c in narrowed if str(c.get("phone_number_id") or "") == hp]

    default_candidate: Optional[Dict[str, Any]] = None
    if len(narrowed) == 1:
        default_candidate = narrowed[0]
    elif len(narrowed) == 0 and len(candidates) == 1:
        default_candidate = candidates[0]

    reasons: List[str] = []
    if len(candidates) > 1:
        distinct_biz = {str(c.get("business_id") or "") for c in candidates if c.get("business_id")}
        if len(distinct_biz) > 1:
            reasons.append("multiple_businesses")
        distinct_waba = {str(c.get("waba_id") or "") for c in candidates}
        if len(distinct_waba) > 1:
            reasons.append("multiple_wabas")
        reasons.append("multiple_phone_bindings")

    requires_user_choice = len(candidates) > 1 and default_candidate is None

    return {
        "api_version": ver,
        "businesses": businesses_out,
        "candidates": candidates,
        "narrowed_candidates": narrowed,
        "default_candidate": default_candidate,
        "requires_user_choice": requires_user_choice,
        "ambiguity": {
            "requires_user_choice": requires_user_choice,
            "reasons": reasons,
            "counts": counts,
        },
        "token_debug": token_debug,
        "wabas_without_phones": wabas_without_phones,
        "portfolio_errors": portfolio_errors,
    }


def resolve_binding_from_session_hints(
    access_token: str,
    hints: Dict[str, Optional[str]],
    *,
    api_version: Optional[str] = None,
    timeout: int = 20,
) -> Optional[Dict[str, Any]]:
    """
    Tech Provider / Embedded Signup: session logging supplies waba_id + phone_number_id.
    Verify the exchanged token can read the phone asset directly.
    """
    waba_id = str(hints.get("waba_id") or "").strip()
    phone_number_id = str(hints.get("phone_number_id") or "").strip()
    if not waba_id or not phone_number_id:
        return None

    ver = api_version or DEFAULT_API_VERSION
    root = _graph_root(ver)
    j = _get_json(
        f"{root}/{phone_number_id}",
        params={
            "access_token": access_token,
            "fields": "id,display_phone_number,verified_name,quality_rating",
        },
        timeout=timeout,
    )
    if "error" in j:
        logger.warning(
            "hint binding phone lookup failed phone_number_id=%s error=%s",
            phone_number_id,
            j.get("error"),
        )
        return None

    waba_name = None
    wj = _get_json(
        f"{root}/{waba_id}",
        params={"access_token": access_token, "fields": "id,name"},
        timeout=timeout,
    )
    if "error" not in wj:
        waba_name = wj.get("name")

    return {
        "waba_id": waba_id,
        "phone_number_id": phone_number_id,
        "display_phone_number": j.get("display_phone_number"),
        "verified_name": j.get("verified_name"),
        "waba_name": waba_name or j.get("verified_name") or "WhatsApp Business",
        "meta_business_id": str(hints.get("business_id") or "").strip() or None,
        "quality_rating": j.get("quality_rating"),
    }


def resolve_binding_for_auto_connect(
    access_token: str,
    *,
    api_version: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: Optional[str] = None,
    hints: Optional[Dict[str, Optional[str]]] = None,
    allow_legacy_single_guess: bool = True,
    timeout: int = 30,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Pick a single (business_id, waba_id, phone_number_id) binding.

    If discovery is ambiguous and allow_legacy_single_guess is True, falls back to
    the first candidate (legacy behavior) but sets discovery["legacy_guess_used"]=True.
    """
    disc = discover_whatsapp_assets(
        access_token,
        api_version=api_version,
        app_id=app_id,
        app_secret=app_secret,
        hints=hints,
        timeout=timeout,
    )
    disc["legacy_guess_used"] = False
    chosen = disc.get("default_candidate")
    narrowed = disc.get("narrowed_candidates") or []

    hints = hints or {}
    has_hints = bool(
        str(hints.get("business_id") or "").strip()
        or str(hints.get("waba_id") or "").strip()
        or str(hints.get("phone_number_id") or "").strip()
    )
    if has_hints and not narrowed and disc.get("candidates"):
        raise DiscoveryAmbiguousError(
            "Discovery hints did not match any candidate asset.",
            disc,
        )

    if chosen:
        binding = {
            "waba_id": chosen["waba_id"],
            "phone_number_id": chosen["phone_number_id"],
            "display_phone_number": chosen.get("display_phone_number"),
            "verified_name": chosen.get("verified_name"),
            "waba_name": chosen.get("waba_name") or chosen.get("verified_name") or "WhatsApp Business",
            "meta_business_id": chosen.get("business_id"),
            "quality_rating": chosen.get("quality_rating"),
        }
        return binding, disc

    if not narrowed and not disc.get("candidates"):
        hint_binding = resolve_binding_from_session_hints(
            access_token,
            hints,
            api_version=api_version,
            timeout=timeout,
        )
        if hint_binding:
            disc["hint_binding_used"] = True
            return hint_binding, disc

        token_debug = disc.get("token_debug") or {}
        scopes = token_debug.get("scopes") or []
        granular = token_debug.get("granular_scopes") or []
        has_wa_scope = any(
            s in scopes
            for s in (
                "whatsapp_business_management",
                "whatsapp_business_messaging",
                "business_management",
            )
        )
        has_granular_wa = any(
            (g.get("scope") or "") in ("whatsapp_business_management", "whatsapp_business_messaging")
            for g in granular
        )
        if not has_wa_scope and not has_granular_wa:
            raise ValueError(
                "Token exchange succeeded but this is not a WhatsApp Embedded Signup business token. "
                "Finish the Meta coexistence popup (select your WhatsApp Business account and phone), "
                "then click Connect again."
            )
        raise ValueError(
            "No WhatsApp Business Accounts or phone numbers found for this token. "
            "Complete Embedded Signup in Meta and ensure session logging captured waba_id and phone_number_id."
        )

    if disc.get("requires_user_choice") or len(narrowed) > 1:
        if allow_legacy_single_guess and disc.get("candidates"):
            guess = disc["candidates"][0]
            disc["legacy_guess_used"] = True
            logger.warning(
                "Ambiguous Meta portfolio; using legacy first-candidate guess. "
                "Prefer passing hints (business_id, waba_id, phone_number_id). "
                "waba_id=%s phone_number_id=%s",
                guess.get("waba_id"),
                guess.get("phone_number_id"),
            )
            binding = {
                "waba_id": guess["waba_id"],
                "phone_number_id": guess["phone_number_id"],
                "display_phone_number": guess.get("display_phone_number"),
                "verified_name": guess.get("verified_name"),
                "waba_name": guess.get("waba_name") or guess.get("verified_name") or "WhatsApp Business",
                "meta_business_id": guess.get("business_id"),
                "quality_rating": guess.get("quality_rating"),
            }
            return binding, disc
        raise DiscoveryAmbiguousError(
            "Multiple WhatsApp assets match this token; supply business_id, waba_id, and/or phone_number_id.",
            disc,
        )

    if len(narrowed) == 1:
        c = narrowed[0]
        binding = {
            "waba_id": c["waba_id"],
            "phone_number_id": c["phone_number_id"],
            "display_phone_number": c.get("display_phone_number"),
            "verified_name": c.get("verified_name"),
            "waba_name": c.get("waba_name") or c.get("verified_name") or "WhatsApp Business",
            "meta_business_id": c.get("business_id"),
            "quality_rating": c.get("quality_rating"),
        }
        return binding, disc

    raise DiscoveryAmbiguousError(
        "Could not resolve a unique WhatsApp asset; adjust hints.",
        disc,
    )


def candidate_to_legacy_oauth_dict(binding: Dict[str, Any]) -> Dict[str, Any]:
    """Shape expected by oauth.save_whatsapp_account."""
    return {
        "waba_id": binding["waba_id"],
        "waba_name": binding.get("waba_name") or binding.get("verified_name") or "Unknown",
        "phone_number_id": binding["phone_number_id"],
        "display_phone_number": binding.get("display_phone_number"),
        "verified_name": binding.get("verified_name"),
        "meta_business_id": binding.get("meta_business_id"),
    }


def check_business_readiness(
    phone_number_id: str,
    access_token: str,
    *,
    api_version: Optional[str] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    """
    Business Info Completeness Check.
    Fetches the WhatsApp Business Profile to verify it meets Meta's integrity requirements
    (HTTPS website, email, address, vertical).
    Incomplete profiles trigger instant restrictions.
    """
    ver = api_version or DEFAULT_API_VERSION
    root = _graph_root(ver)
    
    url = f"{root}/{phone_number_id}/whatsapp_business_profile"
    j = _get_json(
        url,
        params={"access_token": access_token, "fields": "about,address,description,email,profile_picture_url,websites,vertical"},
        timeout=timeout,
    )
    
    if "error" in j:
        return {
            "is_ready": False,
            "error": j.get("error"),
            "missing_fields": ["profile_fetch_failed"]
        }
        
    data = (j.get("data") or [{}])[0]
    
    missing_fields = []
    issues = []
    
    # Check email
    if not data.get("email"):
        missing_fields.append("business_email")
        
    # Check address
    if not data.get("address"):
        missing_fields.append("business_address")
        
    # Check vertical/category
    if not data.get("vertical"):
        missing_fields.append("category_alignment")
        
    # Check website and HTTPS
    websites = data.get("websites") or []
    if not websites:
        missing_fields.append("business_website")
    else:
        has_https = False
        for site in websites:
            if site.startswith("https://"):
                has_https = True
                break
        if not has_https:
            issues.append("website_not_https")
            
    is_ready = len(missing_fields) == 0 and len(issues) == 0
    
    return {
        "is_ready": is_ready,
        "missing_fields": missing_fields,
        "issues": issues,
        "profile_data": data
    }
