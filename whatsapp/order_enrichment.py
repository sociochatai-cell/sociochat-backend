"""
Enrich WhatsApp catalog order webhook payloads with product names from Meta catalog.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests as http_requests

logger = logging.getLogger(__name__)

GRAPH_API_BASE = "https://graph.facebook.com"


def _api_version() -> str:
    return os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _default_button_label(btn_type: str) -> str:
    labels = {
        "QUICK_REPLY": "Quick Reply",
        "URL": "Visit Website",
        "PHONE_NUMBER": "Call",
        "CATALOG": "View Catalog",
        "FLOW": "Open Flow",
        "COPY_CODE": "Copy Code",
        "VOICE_CALL": "Call on WhatsApp",
    }
    return labels.get(str(btn_type or "").upper(), "Button")


def template_buttons_from_schema(components_schema: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Extract displayable template buttons from stored Meta template components."""
    if not components_schema:
        return []

    buttons: List[Dict[str, Any]] = []
    for comp in components_schema:
        if str(comp.get("type", "")).upper() != "BUTTONS":
            continue
        for btn in comp.get("buttons", []) or []:
            btn_type = str(btn.get("type", "")).upper()
            buttons.append(
                {
                    "title": btn.get("text") or _default_button_label(btn_type),
                    "type": btn_type,
                    "url": btn.get("url"),
                    "phone_number": btn.get("phone_number"),
                }
            )
    return buttons


def _normalize_order_items(raw_items: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_items, list):
        return []

    normalized: List[Dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        retailer_id = (
            item.get("product_retailer_id")
            or item.get("retailer_id")
            or item.get("id")
        )
        quantity = item.get("quantity", 1)
        try:
            quantity = int(quantity)
        except (TypeError, ValueError):
            quantity = 1

        price = item.get("item_price")
        if price is None:
            amount = item.get("amount")
            if isinstance(amount, dict):
                value = amount.get("value")
                offset = amount.get("offset") or 100
                try:
                    price = float(value) / float(offset) if value is not None else None
                except (TypeError, ValueError, ZeroDivisionError):
                    price = None

        normalized.append(
            {
                "product_retailer_id": retailer_id,
                "quantity": quantity,
                "item_price": price,
                "currency": item.get("currency") or "INR",
                "name": item.get("name") or item.get("product_name"),
                "image_url": item.get("image_url"),
            }
        )
    return normalized


def _sanitize_catalog_image_url(url: str) -> str:
    """Convert common hosted links (e.g. Google Drive) to browser-loadable URLs."""
    if not url:
        return ""
    cleaned = url.strip()
    if not cleaned:
        return ""

    patterns = [
        r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/open\?id=([a-zA-Z0-9_-]+)",
        r"docs\.google\.com/file/d/([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/uc\?(?:export=download&)?id=([a-zA-Z0-9_-]+)",
        r"drive\.google\.com/uc\?id=([a-zA-Z0-9_-]+)(?:&export=download)?",
        r"lh3\.googleusercontent\.com/d/([a-zA-Z0-9_-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned)
        if match:
            return f"https://lh3.googleusercontent.com/d/{match.group(1)}"

    return cleaned


def _is_safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in ("localhost", "127.0.0.1", "0.0.0.0"):
        return False
    if host.endswith(".local"):
        return False
    return True


def _product_image_url(product: Dict[str, Any]) -> Optional[str]:
    """Resolve a displayable image URL from Meta catalog product payload."""
    direct = product.get("image_url") or product.get("image_link") or product.get("picture")
    if isinstance(direct, str) and direct.strip():
        return _sanitize_catalog_image_url(direct.strip())

    additional = product.get("additional_image_urls")
    if isinstance(additional, list):
        for entry in additional:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()

    images = product.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, str) and entry.strip():
                return _sanitize_catalog_image_url(entry.strip())
            if isinstance(entry, dict):
                nested = entry.get("url") or entry.get("image_url") or entry.get("src")
                if isinstance(nested, str) and nested.strip():
                    return _sanitize_catalog_image_url(nested.strip())

    return None


def fetch_catalog_image_bytes(image_url: str, access_token: str = "") -> Tuple[Optional[bytes], str]:
    """
    Fetch catalog product image bytes server-side (avoids browser hotlink/CORS blocks).
    Returns (bytes, content_type).
    """
    sanitized = _sanitize_catalog_image_url(image_url)
    if not sanitized or not _is_safe_public_url(sanitized):
        return None, "image/jpeg"

    headers = {"User-Agent": "Mozilla/5.0 (compatible; SocioviaCatalog/1.0)"}
    # Meta CDN images occasionally require auth when hotlinked
    if access_token and "fbcdn" in sanitized:
        headers["Authorization"] = f"Bearer {access_token}"

    try:
        resp = http_requests.get(sanitized, headers=headers, timeout=20, allow_redirects=True)
        if not resp.ok:
            return None, "image/jpeg"
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        if not str(content_type).startswith("image/"):
            content_type = "image/jpeg"
        return resp.content, content_type
    except Exception as exc:
        logger.warning("Catalog image fetch failed for %s: %s", sanitized, exc)
        return None, "image/jpeg"


def _product_meta_from_record(product: Dict[str, Any]) -> Dict[str, Any]:
    price = product.get("price")
    try:
        price_value = float(price) / 100 if price is not None else None
    except (TypeError, ValueError):
        price_value = None

    return {
        "name": product.get("name"),
        "image_url": _product_image_url(product),
        "currency": product.get("currency"),
        "price": price_value,
        "retailer_id": product.get("retailer_id"),
        "id": product.get("id"),
    }


def _index_product_meta(product_map: Dict[str, Dict[str, Any]], product: Dict[str, Any]) -> None:
    """Index product metadata by Meta product id and retailer_id (WhatsApp may send either)."""
    meta = _product_meta_from_record(product)
    meta_id = str(product.get("id") or "").strip()
    retailer_id = str(product.get("retailer_id") or "").strip()

    if meta_id:
        product_map[meta_id] = meta
    if retailer_id and retailer_id != meta_id:
        product_map[retailer_id] = meta


def _fetch_catalog_product_map(catalog_id: str, access_token: str) -> Dict[str, Dict[str, Any]]:
    """Build product id / retailer_id -> metadata map for a catalog."""
    if not catalog_id or not access_token:
        return {}

    url = f"{GRAPH_API_BASE}/{_api_version()}/{catalog_id}/products"
    fields = "id,name,description,price,currency,image_url,image_link,picture,retailer_id,additional_image_urls,images"
    product_map: Dict[str, Dict[str, Any]] = {}
    next_url: Optional[str] = None
    params: Dict[str, Any] = {"fields": fields, "limit": 100}

    try:
        for _ in range(10):
            resp = http_requests.get(
                next_url or url,
                params=None if next_url else params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=15,
            )
            data = resp.json() if resp.content else {}
            if not resp.ok:
                logger.warning("Catalog product lookup failed for %s: %s", catalog_id, data)
                break

            for product in data.get("data", []) or []:
                if isinstance(product, dict):
                    _index_product_meta(product_map, product)

            paging = data.get("paging", {}) or {}
            next_url = (paging.get("next") or "").strip() or None
            if not next_url:
                break
    except Exception as exc:
        logger.warning("Catalog enrichment request failed: %s", exc)

    return product_map


def _fetch_product_by_id(product_id: str, access_token: str) -> Dict[str, Any]:
    """Fetch a single catalog product directly when list lookup misses."""
    if not product_id or not access_token:
        return {}

    url = f"{GRAPH_API_BASE}/{_api_version()}/{product_id}"
    fields = "id,name,description,price,currency,image_url,image_link,picture,retailer_id,additional_image_urls,images"

    try:
        resp = http_requests.get(
            url,
            params={"fields": fields},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
        if resp.ok and isinstance(data, dict):
            return _product_meta_from_record(data)
        logger.warning("Direct product lookup failed for %s: %s", product_id, data)
    except Exception as exc:
        logger.warning("Direct product lookup error for %s: %s", product_id, exc)

    return {}


def _apply_product_meta(item: Dict[str, Any], meta: Dict[str, Any]) -> None:
    if not meta:
        return
    if meta.get("name") and not item.get("name"):
        item["name"] = meta["name"]
    incoming_image = meta.get("image_url")
    if incoming_image:
        sanitized = _sanitize_catalog_image_url(str(incoming_image))
        current = str(item.get("image_url") or "")
        if not current or "drive.google.com" in current or current != sanitized:
            item["image_url"] = sanitized
    if item.get("item_price") is None and meta.get("price") is not None:
        item["item_price"] = meta["price"]
    if not item.get("currency") and meta.get("currency"):
        item["currency"] = meta["currency"]
    if meta.get("retailer_id") and not item.get("product_retailer_id"):
        item["product_retailer_id"] = meta["retailer_id"]


def enrich_order_content(content: Dict[str, Any], account: Any) -> Dict[str, Any]:
    """
    Normalize and enrich order/cart message content for inbox display and notifications.
    """
    raw_message = content.get("raw") if isinstance(content.get("raw"), dict) else {}
    raw_order = raw_message.get("order") if isinstance(raw_message.get("order"), dict) else {}

    order_block = content.get("order") if isinstance(content.get("order"), dict) else {}
    if not order_block and raw_order:
        order_block = raw_order
    catalog_id = content.get("catalog_id") or order_block.get("catalog_id")
    note_text = content.get("text") or order_block.get("text")

    raw_items = (
        content.get("product_items")
        or order_block.get("product_items")
        or order_block.get("items")
        or []
    )
    items = _normalize_order_items(raw_items)

    token = ""
    try:
        token = account.get_access_token() if account else ""
    except Exception:
        token = ""

    if token and items:
        product_map: Dict[str, Dict[str, Any]] = {}
        if catalog_id:
            product_map = _fetch_catalog_product_map(str(catalog_id), token)

        for item in items:
            lookup_key = str(item.get("product_retailer_id") or "").strip()
            meta = product_map.get(lookup_key, {}) if lookup_key else {}

            if (not meta.get("name") or not meta.get("image_url")) and lookup_key:
                meta = _fetch_product_by_id(lookup_key, token) or meta

            _apply_product_meta(item, meta)

    enriched_order = {
        "catalog_id": catalog_id,
        "text": note_text,
        "product_items": items,
    }

    content["catalog_id"] = catalog_id
    content["text"] = note_text
    content["product_items"] = items
    content["order"] = enriched_order
    return content
