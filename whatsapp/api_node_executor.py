"""
API Node Executor — Interactive flow HTTP integration
=====================================================

Flow builder node type: ``api``

Node ``data`` schema (frontend):

{
  "label": "Lookup order",
  "method": "POST",
  "url": "https://api.example.com/v1/orders/lookup",
  "headers": [{"key": "Authorization", "value": "Bearer {{flow_api_token}}", "enabled": true}],
  "queryParams": [{"key": "phone", "value": "{{phone}}", "enabled": true}],
  "bodyType": "json",          // json | form | none
  "body": "{\"phone\": \"{{phone}}\"}",
  "timeoutSec": 15,
  "responseFormat": "auto",    // auto | json | text | xml
  "storeAs": "order_lookup",   // optional — saved to flow collected fields
  "branches": [
    {"id": "found", "path": "status", "operator": "equals", "value": "found"},
    {"id": "missing", "path": "status", "operator": "equals", "value": "not_found"}
  ],
  "output": {
    "onSuccess": { "mode": "auto", "textPath": "message", "buttonsPath": "quickReplies" },
    "onError": { "text": "Something went wrong." }
  },
  "buttonCapture": [
    {"matchType": "uuid", "field": "entity_id", "valueFrom": "button_id"},
    {"matchType": "exact", "value": "option_a", "field": "choice", "setValue": "a"}
  ]
}

Outgoing flow edges use React Flow ``sourceHandle``:
  - ``success`` — HTTP 2xx
  - ``error`` — network/HTTP/SSRF failure
  - ``branch-{id}`` — matched branch rule (evaluated before success/error)
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
API_INTERACTIVE_OUTBOUND_TYPES = frozenset({"buttons", "list", "carousel"})
# Outbound types that require the user to tap before the flow continues.
API_USER_PICK_OUTBOUND_TYPES = frozenset({"buttons", "list", "carousel", "template"})
MAX_RESPONSE_BYTES = int(os.getenv("API_NODE_MAX_RESPONSE_BYTES", str(256 * 1024)))
DEFAULT_TIMEOUT = float(os.getenv("API_NODE_DEFAULT_TIMEOUT", "15"))
_PLACEHOLDER_RE = re.compile(r"\{\{([\w.]+)\}\}")


@dataclass
class ApiNodeResult:
    success: bool
    status_code: Optional[int] = None
    handle: str = "error"
    parsed: Any = None
    raw_text: str = ""
    error: Optional[str] = None
    stored_value: Any = None
    outbound_messages: List[Dict[str, Any]] = field(default_factory=list)


def resolve_placeholders(value: Any, variables: Dict[str, Any]) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value

    def _replace(match: re.Match) -> str:
        key = match.group(1)
        resolved = get_path(variables, key)
        return "" if resolved is None else str(resolved)

    return _PLACEHOLDER_RE.sub(_replace, value)


def get_path(data: Any, path: str) -> Any:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _hostname_resolves_to_private(hostname: str) -> bool:
    if not hostname:
        return True
    host = hostname.lower().strip(".")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".local"):
        return True
    try:
        for info in socket.getaddrinfo(host, None):
            ip = info[4][0]
            addr = ipaddress.ip_address(ip)
            if (
                addr.is_private
                or addr.is_loopback
                or addr.is_link_local
                or addr.is_reserved
                or addr.is_multicast
            ):
                return True
    except socket.gaierror:
        return True
    return False


def validate_api_url(url: str) -> Tuple[bool, str]:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in {"https", "http"}:
        return False, "URL must use http or https"
    if os.getenv("K_SERVICE") and parsed.scheme != "https":
        return False, "Only HTTPS URLs are allowed in production"
    if not parsed.hostname:
        return False, "URL must include a hostname"
    if _hostname_resolves_to_private(parsed.hostname):
        return False, "Private or internal URLs are not allowed"
    return True, ""


def _normalize_header_pair(key: str, value: str) -> Tuple[str, str]:
    """Fix common flow-builder mistakes (Authorization:Bearer xxx all in key field)."""
    k = (key or "").strip()
    v = (value or "").strip()

    if ":" in k and not v:
        name, _, rest = k.partition(":")
        k = name.strip()
        v = rest.strip()

    if k.lower().startswith("authorization") and ":" in k and not v:
        parts = k.split(":", 1)
        k = parts[0].strip()
        v = parts[1].strip()

    # "Authorization:Bearer token" pasted entirely into key
    for prefix in ("authorization:", "Authorization:"):
        if k.lower().startswith(prefix.lower()) and len(k) > len(prefix):
            v = k[len(prefix) :].strip()
            k = "Authorization"
            break

    if k.lower() == "authorization" and v and not v.lower().startswith("bearer "):
        v = f"Bearer {v}"

    return k, v


def _build_kv_list(items: Optional[List[dict]], variables: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        key = resolve_placeholders(item.get("key"), variables)
        if not key:
            continue
        val = resolve_placeholders(item.get("value", ""), variables)
        key, val = _normalize_header_pair(str(key), str(val))
        if not key:
            continue
        result[key] = val
    return result


def _parse_response_body(raw: bytes, content_type: str, response_format: str) -> Tuple[Any, str]:
    text = raw.decode("utf-8", errors="replace")
    fmt = (response_format or "auto").lower()
    ctype = (content_type or "").lower()

    if fmt == "auto":
        if "json" in ctype or text.lstrip().startswith(("{", "[")):
            fmt = "json"
        else:
            fmt = "text"

    if fmt == "json":
        try:
            return json.loads(text) if text.strip() else {}, text
        except json.JSONDecodeError:
            return {"_raw": text}, text
    return text, text


def _evaluate_branch(branch: dict, parsed: Any) -> bool:
    path = branch.get("path") or branch.get("field")
    operator = (branch.get("operator") or "equals").lower()
    expected = branch.get("value")
    actual = get_path(parsed, path) if path else parsed

    if operator == "exists":
        return actual is not None and actual != ""
    if operator == "not_exists":
        return actual is None or actual == ""
    if operator == "contains":
        return str(expected) in str(actual or "")
    if operator in {"equals", "eq"}:
        return str(actual) == str(expected)
    if operator in {"not_equals", "neq"}:
        return str(actual) != str(expected)
    if operator == "gt":
        try:
            return float(actual) > float(expected)
        except (TypeError, ValueError):
            return False
    if operator == "lt":
        try:
            return float(actual) < float(expected)
        except (TypeError, ValueError):
            return False
    return False


def _is_no_plans_payload(parsed: Any) -> bool:
    """BML returns HTTP 200 + status=error when a library has zero plans configured."""
    if not isinstance(parsed, dict):
        return False
    data = parsed.get("data")
    if isinstance(data, dict) and str(data.get("error_code") or "").upper() == "NO_PLANS":
        return True
    if str(parsed.get("status") or "").lower() == "error":
        qrs = parsed.get("quickReplies")
        if isinstance(qrs, list) and len(qrs) == 0:
            msg = str(parsed.get("message") or "").lower()
            if "no membership plans" in msg or "no plans" in msg:
                return True
    return False


def _is_business_error_payload(parsed: Any) -> bool:
    """BookMyLibrary and similar APIs often return HTTP 200 with status=error in JSON."""
    if not isinstance(parsed, dict):
        return False
    status = str(parsed.get("status") or "").lower()
    if status in {"error", "not_found", "failed", "invalid", "missing_library_id"}:
        return True
    data = parsed.get("data")
    if isinstance(data, dict) and data.get("error_code"):
        return True
    return False


def _resolve_handle(node_data: dict, success: bool, parsed: Any) -> str:
    for branch in node_data.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        branch_id = branch.get("id")
        if branch_id and _evaluate_branch(branch, parsed):
            return f"branch-{branch_id}"
    if success and _is_no_plans_payload(parsed):
        return "branch-no_plans"
    if success and _is_business_error_payload(parsed):
        return "error"
    return "success" if success else "error"


def _coerce_buttons(value: Any) -> List[Dict[str, str]]:
    """Legacy helper — caps at 3 for plain quickReplies. Prefer _build_interactive_options."""
    return _build_interactive_options({"quickReplies": value})[:3]


def _normalize_bml_media_url(url: Optional[str]) -> Optional[str]:
    """BML dev/staging hosts are not public; Meta cannot fetch them for WhatsApp media."""
    if not url:
        return None
    cleaned = str(url).strip()
    if not cleaned or cleaned.lower() in ("null", "none"):
        return None
    for bad_host in ("devb.bookmylibrary.in", "staging.bookmylibrary.in", "localhost"):
        if bad_host in cleaned:
            cleaned = cleaned.replace(f"https://{bad_host}", "https://appb.bookmylibrary.in")
            cleaned = cleaned.replace(f"http://{bad_host}", "https://appb.bookmylibrary.in")
    return cleaned


def _library_image_candidates(library: dict) -> List[str]:
    """All normalized, deduped image URLs for a library (cover first, then gallery)."""
    if not isinstance(library, dict):
        return []
    seen: set[str] = set()
    out: List[str] = []
    for raw in [library.get("cover_image"), library.get("coverImage"), *(library.get("images") or [])]:
        normalized = _normalize_bml_media_url(str(raw) if raw else None)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _library_image_url(library: dict) -> Optional[str]:
    """Best image URL for a BookMyLibrary search row (cover_image or first gallery image)."""
    candidates = _library_image_candidates(library)
    return candidates[0] if candidates else None


def _build_library_carousel_outbound(parsed: dict, body_text: str) -> Optional[Dict[str, Any]]:
    """
    Build WhatsApp interactive media carousel from BML /libraries/search payload.

    Uses data.libraries[].cover_image (or images[]) with quick-reply select buttons
    keyed by library UUID — replaces plain list messages when 2+ libraries have images.
    """
    libraries = get_path(parsed, "data.libraries")
    if not isinstance(libraries, list) or not libraries:
        return None

    qrs = parsed.get("quickReplies") if isinstance(parsed.get("quickReplies"), list) else []
    qr_by_id: Dict[str, dict] = {}
    for qr in qrs:
        if isinstance(qr, dict) and qr.get("id"):
            qr_by_id[str(qr["id"])] = qr

    cards: List[Dict[str, Any]] = []
    for lib in libraries[:10]:
        if not isinstance(lib, dict):
            continue
        lib_id = str(lib.get("id") or "").strip()
        image_url = _library_image_url(lib)
        if not lib_id or not image_url:
            continue

        qr = qr_by_id.get(lib_id)
        label = (qr.get("label") or qr.get("title") if qr else None) or lib.get("name") or "Library"
        name = str(lib.get("name") or label)
        area = str(lib.get("area") or lib.get("city") or "").strip()
        card_body = f"*{name}*"
        if area:
            card_body += f"\n{area}"
        card_body = card_body[:160]

        btn_title = str(label)[:20] if len(str(label)) <= 20 else "Select"

        cards.append(
            {
                "card_index": len(cards),
                "type": "cta_url",
                "header": {"type": "image", "image": {"link": image_url}},
                "body": {"text": card_body},
                "action": {
                    "buttons": [
                        {
                            "type": "quick_reply",
                            "quick_reply": {"id": lib_id, "title": btn_title},
                        }
                    ]
                },
            }
        )

    if len(cards) < 2:
        return None

    body = str(body_text or "Choose a library:")[:1024]
    return {
        "type": "carousel",
        "text": body,
        "interactive": {
            "type": "carousel",
            "body": {"text": body},
            "action": {"cards": cards},
        },
    }


def _build_library_gallery_messages(
    parsed: dict,
    body_text: str,
    interactive_buttons: List[Dict[str, str]],
    *,
    list_button_text: str = "View libraries",
    list_section_title: str = "Libraries nearby",
) -> Optional[List[Dict[str, Any]]]:
    """
    Library search UX that works on every WABA — no carousel template required.

    Sends one standard image message per library (cover_image from BML API), then a
    list or button message so the user can pick a library.
    """
    libraries = get_path(parsed, "data.libraries")
    if not isinstance(libraries, list) or not libraries or not interactive_buttons:
        return None

    lib_by_id: Dict[str, dict] = {}
    for lib in libraries:
        if isinstance(lib, dict) and lib.get("id"):
            lib_by_id[str(lib["id"])] = lib

    messages: List[Dict[str, Any]] = []
    for btn in interactive_buttons[:10]:
        lib_id = str(btn.get("id") or "")
        lib = lib_by_id.get(lib_id)
        if not lib:
            continue
        image_url = _library_image_url(lib)
        if not image_url:
            continue
        name = str(lib.get("name") or btn.get("label") or btn.get("title") or "Library")
        area = str(lib.get("area") or lib.get("city") or "").strip()
        caption = f"📚 *{name}*"
        if area:
            caption += f"\n📍 {area}"
        messages.append(
            {
                "type": "image",
                "url": image_url,
                "url_candidates": _library_image_candidates(lib),
                "caption": caption[:1024],
            }
        )

    if not messages:
        return None

    body = str(body_text or "Choose a library:")[:1024]
    if len(interactive_buttons) > 3:
        rows = []
        for btn in interactive_buttons[:10]:
            lib_id = str(btn.get("id") or "")
            lib = lib_by_id.get(lib_id)
            area = str(lib.get("area") or lib.get("city") or "").strip() if lib else ""
            rows.append(
                {
                    "id": btn["id"],
                    "title": (btn.get("title") or btn.get("label") or "Library")[:24],
                    "description": area[:72],
                }
            )
        messages.append(
            {
                "type": "list",
                "text": body,
                "buttonText": list_button_text,
                "sections": [{"title": list_section_title, "rows": rows}],
            }
        )
    else:
        messages.append({"type": "buttons", "text": body, "buttons": interactive_buttons})

    return messages


def _build_interactive_options(parsed: Any) -> List[Dict[str, str]]:
    """
    Build WhatsApp-safe plan/library options from BookMyLibrary responses.

    BML often returns duplicate quickReply ids (e.g. four buttons all id=monthly).
    We derive unique ids from data.plans (type + price_inr) when available.
    """
    if not isinstance(parsed, dict):
        return []

    plans = get_path(parsed, "data.plans")
    qrs = parsed.get("quickReplies") if isinstance(parsed.get("quickReplies"), list) else []
    buttons: List[Dict[str, str]] = []

    if isinstance(plans, list) and plans:
        for idx, plan in enumerate(plans):
            if not isinstance(plan, dict):
                continue
            ptype = str(plan.get("type") or "plan")
            price = plan.get("price_inr")
            btn_id = f"{ptype}__{price}" if price is not None else f"{ptype}__{idx}"
            label = None
            if idx < len(qrs) and isinstance(qrs[idx], dict):
                label = qrs[idx].get("label") or qrs[idx].get("title") or qrs[idx].get("text")
            if not label:
                name = plan.get("name") or ptype.replace("_", " ").title()
                label = f"{name} ₹{price}" if price is not None else str(name)
            title = str(label)[:24]
            buttons.append({"id": btn_id, "title": title, "label": title})
        return buttons

    seen: Dict[str, int] = {}
    for idx, item in enumerate(qrs):
        if isinstance(item, str):
            btn_id = f"api_btn_{idx}"
            title = item[:24]
            buttons.append({"id": btn_id, "title": title, "label": title})
            continue
        if not isinstance(item, dict):
            continue
        label = item.get("label") or item.get("title") or item.get("text")
        btn_id = str(item.get("id") or f"api_btn_{idx}")
        if btn_id in seen:
            seen[btn_id] += 1
            btn_id = f"{btn_id}__{seen[btn_id]}"
        else:
            seen[btn_id] = 0
        if label:
            title = str(label)[:24]
            buttons.append({"id": btn_id, "title": title, "label": title})
    return buttons


def outbound_requires_user_pick(outbound_messages: List[Dict[str, Any]]) -> bool:
    """True when API outbound includes buttons/list/carousel that need a user tap before continuing."""
    return any(
        (msg.get("type") or "").lower() in API_USER_PICK_OUTBOUND_TYPES
        for msg in (outbound_messages or [])
    )


def extract_pending_api_buttons(outbound_messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten selectable ids from API outbound messages for mid-flow button matching."""
    pending: List[Dict[str, Any]] = []
    for outbound in outbound_messages or []:
        otype = (outbound.get("type") or "").lower()
        if otype == "buttons":
            pending.extend(outbound.get("buttons") or [])
            continue
        if otype == "list":
            for section in outbound.get("sections") or []:
                if isinstance(section, dict):
                    pending.extend(section.get("rows") or [])
            continue
        if otype == "carousel":
            interactive = outbound.get("interactive") or {}
            cards = (interactive.get("action") or {}).get("cards") or outbound.get("cards") or []
            for card in cards:
                if not isinstance(card, dict):
                    continue
                for btn in (card.get("action") or {}).get("buttons") or []:
                    if not isinstance(btn, dict) or btn.get("type") != "quick_reply":
                        continue
                    qr = btn.get("quick_reply") or {}
                    title = qr.get("title") or ""
                    pending.append({"id": qr.get("id"), "title": title, "label": title})
            continue
        if otype == "template":
            for btn in outbound.get("pending_buttons") or []:
                if isinstance(btn, dict):
                    pending.append(btn)
            continue
    return pending


def _enrich_bml_location_contact_text(text: str, parsed: Any) -> str:
    """
    BookMyLibrary often returns maps_url/manager_phone as null in JSON but puts the
    address in FAQ message text after booking. Build a Maps link from that text.
    """
    if not text or not isinstance(text, str):
        return text

    ctx = parsed if isinstance(parsed, dict) else {}
    data = ctx.get("data") if isinstance(ctx.get("data"), dict) else {}
    maps_url = data.get("maps_url") or ctx.get("maps_url")
    manager_phone = data.get("manager_phone") or ctx.get("manager_phone")

    cleaned = text
    if "address & manager details below" in cleaned.lower():
        if not maps_url and not manager_phone:
            cleaned = re.sub(
                r"\s*address\s*&\s*manager\s*details\s*below\.?\s*",
                "",
                cleaned,
                flags=re.IGNORECASE,
            ).strip()

    extras: List[str] = []
    if maps_url and str(maps_url).strip().lower() not in ("null", "none"):
        extras.append(f"🗺️ Google Maps:\n{maps_url}")
    else:
        lines = [ln.strip() for ln in cleaned.splitlines() if ln.strip()]
        if len(lines) >= 2 and lines[0].startswith("📍"):
            address = " ".join(lines[1:])
            if len(address) > 8 and "not available" not in address.lower():
                extras.append(
                    "🗺️ Open in Google Maps:\n"
                    f"https://www.google.com/maps/search/?api=1&query={quote(address)}"
                )

    if manager_phone and str(manager_phone).strip().lower() not in ("null", "none", ""):
        phone_str = str(manager_phone).strip()
        if "not available" not in phone_str.lower():
            extras.append(f"📞 Library manager: {phone_str}")

    if extras:
        return cleaned + "\n\n" + "\n\n".join(extras)
    return cleaned


def _is_silent_api_output(node_data: dict, spec: dict, success: bool) -> bool:
    """Fire-and-forget API nodes (e.g. CRM lead capture) must not message the user."""
    if node_data.get("silent") is True:
        return True
    output = node_data.get("output") or {}
    if output.get("silent") is True:
        return True
    mode = str(spec.get("mode") or "").lower()
    return mode in {"silent", "none", "skip"}


def build_outbound_messages(node_data: dict, parsed: Any, success: bool, variables: Dict[str, Any]) -> List[Dict[str, Any]]:
    output = node_data.get("output") or {}
    spec = output.get("onSuccess" if success else "onError") or {}
    messages: List[Dict[str, Any]] = []

    if _is_silent_api_output(node_data, spec, success):
        return []

    if not success:
        text = spec.get("text") or node_data.get("errorMessage") or "Something went wrong. Please try again."
        return [{"type": "text", "text": resolve_placeholders(text, variables)}]

    mode = (spec.get("mode") or "auto").lower()
    ctx = parsed if isinstance(parsed, dict) else {"_raw": parsed}

    text = spec.get("text")
    if text:
        messages.append({"type": "text", "text": resolve_placeholders(text, {**variables, **ctx, "api": ctx})})

    text_path = spec.get("textPath") or spec.get("messagePath")
    image_path = spec.get("imagePath")
    document_path = spec.get("documentPath")
    caption_path = spec.get("captionPath")
    filename_path = spec.get("documentFilenamePath")
    buttons_path = spec.get("buttonsPath")

    interactive_buttons: List[Dict[str, str]] = []
    if mode in {"auto", "buttons", "interactive"} and buttons_path:
        interactive_buttons = _build_interactive_options(ctx)
        if not interactive_buttons:
            raw_buttons = get_path(ctx, buttons_path)
            interactive_buttons = _build_interactive_options({"quickReplies": raw_buttons})

    if mode in {"auto", "text"} and text_path and not text and not interactive_buttons:
        val = get_path(ctx, text_path)
        if val:
            messages.append({"type": "text", "text": str(val)})

    if mode in {"auto", "image"} and image_path:
        url = get_path(ctx, image_path)
        if url:
            caption = get_path(ctx, caption_path) if caption_path else None
            messages.append({"type": "image", "url": str(url), "caption": str(caption) if caption else None})

    if mode in {"auto", "document"} and document_path:
        url = get_path(ctx, document_path)
        if url:
            filename = get_path(ctx, filename_path) if filename_path else "document.pdf"
            caption = get_path(ctx, caption_path) if caption_path else None
            messages.append(
                {
                    "type": "document",
                    "url": str(url),
                    "filename": str(filename),
                    "caption": str(caption) if caption else None,
                }
            )

    if mode in {"auto", "buttons", "interactive"} and interactive_buttons:
        body = get_path(ctx, text_path) if text_path else spec.get("fallbackText") or "Choose an option:"
        display_mode = (spec.get("displayMode") or spec.get("libraryDisplay") or "auto").lower()
        list_button_text = spec.get("listButtonText") or (
            "View libraries"
            if get_path(ctx, "data.libraries")
            else "View recruiters"
            if get_path(ctx, "data.recruiters")
            else "View plans"
        )
        list_section_title = spec.get("listSectionTitle") or (
            "Libraries nearby"
            if get_path(ctx, "data.libraries")
            else "Verified recruiters"
            if get_path(ctx, "data.recruiters")
            else "Membership plans"
        )
        carousel_entity = (spec.get("carouselEntity") or "").lower()
        if isinstance(ctx, dict) and (
            get_path(ctx, "data.recruiters") or carousel_entity == "recruiters"
        ):
            use_template = display_mode in {"auto", "carousel_template", "template", "carousel"}
            template_sent = False
            if use_template:
                from . import vaish_carousel_template as vaish_ct

                tpl_out = vaish_ct.build_outbound_from_search(ctx, spec, variables)
                if tpl_out:
                    messages.append(tpl_out)
                    interactive_buttons = []
                    template_sent = True
        elif isinstance(ctx, dict) and get_path(ctx, "data.libraries"):
            use_template = display_mode in {"auto", "carousel_template", "template", "carousel"}
            template_sent = False
            if use_template:
                from . import bml_carousel_template as bml_ct

                tpl_out = bml_ct.build_outbound_from_search(ctx, spec, variables)
                if tpl_out:
                    messages.append(tpl_out)
                    interactive_buttons = []
                    template_sent = True
            if not template_sent and display_mode in {"gallery", "collage"}:
                gallery_msgs = _build_library_gallery_messages(
                    ctx,
                    str(body),
                    interactive_buttons,
                    list_button_text=list_button_text,
                    list_section_title=list_section_title,
                )
                if gallery_msgs:
                    messages.extend(gallery_msgs)
                    interactive_buttons = []
        if interactive_buttons and len(interactive_buttons) > 3:
            rows = []
            for btn in interactive_buttons[:10]:
                rows.append(
                    {
                        "id": btn["id"],
                        "title": (btn.get("title") or btn.get("label") or "Plan")[:24],
                        "description": (btn.get("label") or "")[:72] if btn.get("label") else "",
                    }
                )
            messages.append(
                {
                    "type": "list",
                    "text": str(body),
                    "buttonText": list_button_text,
                    "sections": [{"title": list_section_title, "rows": rows}],
                }
            )
        elif interactive_buttons:
            messages.append({"type": "buttons", "text": str(body), "buttons": interactive_buttons})

    if mode == "raw" and isinstance(parsed, str):
        messages.append({"type": "text", "text": parsed[:4096]})

    if not messages:
        fallback = spec.get("fallbackText") or "Done."
        messages.append({"type": "text", "text": resolve_placeholders(fallback, variables)})

    for msg in messages:
        if msg.get("type") == "text" and msg.get("text"):
            msg["text"] = _enrich_bml_location_contact_text(str(msg["text"]), ctx)

    return messages


def execute_api_node(node_data: dict, variables: Dict[str, Any]) -> ApiNodeResult:
    method = (node_data.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        return ApiNodeResult(success=False, error=f"Unsupported HTTP method: {method}")

    url = resolve_placeholders(node_data.get("url"), variables)
    ok, err = validate_api_url(str(url or ""))
    if not ok:
        logger.warning("[api_node] Blocked URL: %s (%s)", url, err)
        outbound = build_outbound_messages(node_data, {}, False, variables)
        return ApiNodeResult(success=False, error=err, handle="error", outbound_messages=outbound)

    headers = _build_kv_list(node_data.get("headers"), variables)
    params = _build_kv_list(node_data.get("queryParams"), variables)
    timeout = float(node_data.get("timeoutSec") or DEFAULT_TIMEOUT)
    timeout = min(timeout, 30.0)
    body_type = (node_data.get("bodyType") or "none").lower()

    json_body = None
    data_body = None
    if method != "GET" and body_type != "none":
        raw_body = resolve_placeholders(node_data.get("body") or "", variables)
        if body_type == "json":
            try:
                json_body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                return ApiNodeResult(success=False, error="Request body is not valid JSON")
        elif body_type == "form":
            try:
                data_body = json.loads(raw_body) if raw_body else {}
            except json.JSONDecodeError:
                data_body = {"payload": raw_body}

    try:
        response = requests.request(
            method=method,
            url=str(url),
            headers=headers,
            params=params,
            json=json_body,
            data=data_body if data_body is not None else None,
            timeout=timeout,
        )
        raw = response.content[:MAX_RESPONSE_BYTES]
        parsed, raw_text = _parse_response_body(
            raw,
            response.headers.get("Content-Type", ""),
            node_data.get("responseFormat") or "auto",
        )
        success = 200 <= response.status_code < 300
        handle = _resolve_handle(node_data, success, parsed)
        stored = parsed if node_data.get("storeAs") else None
        outbound = build_outbound_messages(node_data, parsed, success, variables)

        return ApiNodeResult(
            success=success,
            status_code=response.status_code,
            handle=handle,
            parsed=parsed,
            raw_text=raw_text,
            stored_value=stored,
            outbound_messages=outbound,
            error=None if success else f"HTTP {response.status_code}",
        )
    except requests.Timeout:
        outbound = build_outbound_messages(node_data, {}, False, variables)
        return ApiNodeResult(success=False, error="Request timed out", handle="error", outbound_messages=outbound)
    except requests.RequestException as exc:
        outbound = build_outbound_messages(node_data, {}, False, variables)
        return ApiNodeResult(success=False, error=str(exc), handle="error", outbound_messages=outbound)


def validate_api_node_data(node_data: dict) -> List[str]:
    errors: List[str] = []
    if not (node_data.get("url") or "").strip():
        errors.append("API node requires a URL")
    method = (node_data.get("method") or "GET").upper()
    if method not in ALLOWED_METHODS:
        errors.append(f"Invalid HTTP method: {method}")
    url_resolved = node_data.get("url", "")
    ok, err = validate_api_url(url_resolved.replace("{{", "").replace("}}", "example"))
    if not ok and "{{" not in (node_data.get("url") or ""):
        errors.append(err)
    return errors
