"""
BookMyLibrary — WhatsApp media-card carousel template helpers.

Template: bml_library_search_carousel_v1 (5 cards, quick_reply Select, MARKETING)
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .api_node_executor import (
    _build_interactive_options,
    _library_image_candidates,
    _normalize_bml_media_url,
    get_path,
)

if TYPE_CHECKING:
    from .messaging_service import WhatsAppMessagingService

logger = logging.getLogger(__name__)

BML_LIBRARY_CAROUSEL_TEMPLATE = os.getenv(
    "BML_LIBRARY_CAROUSEL_TEMPLATE", "bml_library_search_carousel_v1"
)
BML_LIBRARY_CAROUSEL_LANG = os.getenv("BML_LIBRARY_CAROUSEL_LANG", "en_US")
BML_LIBRARY_CAROUSEL_CARD_COUNT = int(os.getenv("BML_LIBRARY_CAROUSEL_CARD_COUNT", "5"))
# Meta hydrated carousel card body limit includes static template text around {{1}}.
_CAROUSEL_CARD_BODY_STATIC = (
    "Study library: "
    + " — tap Select to view membership plans, amenities, and pricing for this location."
)
BML_CAROUSEL_CARD_PARAM_MAX = max(
    20,
    160 - len(_CAROUSEL_CARD_BODY_STATIC),
)


def _library_rows(parsed: dict, interactive_buttons: List[Dict[str, str]]) -> List[Dict[str, str]]:
    libraries = get_path(parsed, "data.libraries")
    lib_by_id: Dict[str, dict] = {}
    if isinstance(libraries, list):
        for lib in libraries:
            if isinstance(lib, dict) and lib.get("id"):
                lib_by_id[str(lib["id"])] = lib

    rows: List[Dict[str, str]] = []
    for btn in interactive_buttons[:BML_LIBRARY_CAROUSEL_CARD_COUNT]:
        lib_id = str(btn.get("id") or "")
        lib = lib_by_id.get(lib_id) or {}
        name = str(lib.get("name") or btn.get("label") or btn.get("title") or "Library")
        area = str(lib.get("area") or lib.get("city") or "").strip() or "India"
        rows.append(
            {
                "library_id": lib_id,
                "name": name[:160],
                "area": area[:160],
                "image_urls": _library_image_candidates(lib),
            }
        )
    return rows


def build_library_carousel_template_outbound(
    parsed: dict,
    body_text: str,
    interactive_buttons: List[Dict[str, str]],
    *,
    template_name: str = BML_LIBRARY_CAROUSEL_TEMPLATE,
    language: str = BML_LIBRARY_CAROUSEL_LANG,
) -> Optional[Dict[str, Any]]:
    """Outbound spec consumed by interactive engine — uploads images and sends template."""
    rows = _library_rows(parsed, interactive_buttons)
    if len(rows) < 2:
        return None

    location = str(body_text or "your area").strip()
    if len(location) > 80:
        location = location[:77] + "..."

    return {
        "type": "template",
        "carousel_kind": "library",
        "template_name": template_name,
        "language": language,
        "location": location,
        "cards": rows,
        "card_count": BML_LIBRARY_CAROUSEL_CARD_COUNT,
        "pending_buttons": [
            {"id": r["library_id"], "title": "Select", "label": r["name"]} for r in rows
        ],
    }


def build_carousel_template_components(
    service: "WhatsAppMessagingService",
    *,
    location: str,
    cards: List[Dict[str, Any]],
    card_count: int = BML_LIBRARY_CAROUSEL_CARD_COUNT,
) -> Optional[List[Dict[str, Any]]]:
    """Build Meta template components; uploads each card image to WhatsApp media API."""
    if not cards:
        return None

    padded: List[Dict[str, Any]] = list(cards[:card_count])
    while len(padded) < card_count:
        padded.append(
            {
                "library_id": f"pad_{len(padded)}",
                "name": "More libraries",
                "area": "Tap Select for options",
                "image_urls": cards[0].get("image_urls") if cards else [],
            }
        )

    carousel_cards: List[Dict[str, Any]] = []
    for idx, card in enumerate(padded[:card_count]):
        media_id = None
        for url in card.get("image_urls") or []:
            normalized = _normalize_bml_media_url(str(url))
            if not normalized:
                continue
            media_id = service.upload_media_from_url(normalized)
            if media_id:
                break
        if not media_id:
            logger.warning("[bml_carousel] no media for card %s lib=%s", idx, card.get("library_id"))
            return None

        card_label = (
            f"{str(card.get('name') or 'Library')}, {str(card.get('area') or 'India')}"
        )[:BML_CAROUSEL_CARD_PARAM_MAX]
        card_components: List[Dict[str, Any]] = [
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"id": media_id}}],
            },
            {
                "type": "body",
                "parameters": [{"type": "text", "text": card_label}],
            },
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": "0",
                "parameters": [{"type": "payload", "payload": str(card.get("library_id") or f"card_{idx}")}],
            },
        ]
        carousel_cards.append({"card_index": idx, "components": card_components})

    return [
        {"type": "body", "parameters": [{"type": "text", "text": location[:1024]}]},
        {"type": "carousel", "cards": carousel_cards},
    ]


def build_outbound_from_search(
    parsed: dict, spec: dict, variables: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    buttons_path = spec.get("buttonsPath") or "quickReplies"
    ctx = parsed if isinstance(parsed, dict) else {}
    interactive_buttons = _build_interactive_options(ctx)
    if not interactive_buttons:
        raw = get_path(ctx, buttons_path)
        interactive_buttons = _build_interactive_options({"quickReplies": raw})

    vars_ = variables or {}
    location = str(vars_.get("location") or vars_.get("pin_code") or "").strip()
    if not location:
        text_path = spec.get("textPath") or spec.get("messagePath")
        location = str(get_path(ctx, text_path) if text_path else spec.get("fallbackText") or "your area")
    template_name = spec.get("carouselTemplateName") or BML_LIBRARY_CAROUSEL_TEMPLATE
    language = spec.get("carouselTemplateLanguage") or BML_LIBRARY_CAROUSEL_LANG

    return build_library_carousel_template_outbound(
        ctx,
        location,
        interactive_buttons,
        template_name=template_name,
        language=language,
    )
