"""
FirstConnection / Vaish — WhatsApp recruiter media-card carousel template helpers.

Template: fc_recruiter_search_carousel_v1 (5 cards, quick_reply View profile, MARKETING)

Expected search API shape (partner / DTR backend):

{
  "message": "We found verified recruiters for Software Engineer.",
  "quickReplies": [{ "id": "rec-uuid", "label": "Neha Gupta" }],
  "data": {
    "recruiters": [{
      "id": "rec-uuid",
      "name": "Neha Gupta",
      "title": "HR Director",
      "company": "Infosys",
      "verified": true,
      "photo_url": "https://..."
    }]
  }
}
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from .api_node_executor import (
    _build_interactive_options,
    get_path,
)

if TYPE_CHECKING:
    from .messaging_service import WhatsAppMessagingService

logger = logging.getLogger(__name__)

VAISH_RECRUITER_CAROUSEL_TEMPLATE = os.getenv(
    "VAISH_RECRUITER_CAROUSEL_TEMPLATE", "fc_recruiter_search_carousel_v1"
)
VAISH_RECRUITER_CAROUSEL_LANG = os.getenv("VAISH_RECRUITER_CAROUSEL_LANG", "en_US")
VAISH_RECRUITER_CAROUSEL_CARD_COUNT = int(os.getenv("VAISH_RECRUITER_CAROUSEL_CARD_COUNT", "5"))

_CAROUSEL_CARD_BODY_STATIC = (
    "Verified Recruiter: "
    + " — tap View profile to see details and connect."
)
VAISH_CAROUSEL_CARD_PARAM_MAX = max(
    20,
    160 - len(_CAROUSEL_CARD_BODY_STATIC),
)


def _recruiter_image_candidates(recruiter: dict) -> List[str]:
    urls: List[str] = []
    for key in ("photo_url", "avatar_url", "profile_image", "image_url", "photo"):
        raw = recruiter.get(key)
        if raw and str(raw).strip() and str(raw) not in urls:
            urls.append(str(raw).strip())
    return urls


def _recruiter_rows(parsed: dict, interactive_buttons: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    recruiters = get_path(parsed, "data.recruiters")
    by_id: Dict[str, dict] = {}
    if isinstance(recruiters, list):
        for row in recruiters:
            if isinstance(row, dict) and row.get("id"):
                by_id[str(row["id"])] = row

    rows: List[Dict[str, Any]] = []
    for btn in interactive_buttons[:VAISH_RECRUITER_CAROUSEL_CARD_COUNT]:
        rec_id = str(btn.get("id") or "")
        rec = by_id.get(rec_id) or {}
        name = str(rec.get("name") or btn.get("label") or btn.get("title") or "Recruiter")
        title = str(rec.get("title") or rec.get("role") or "").strip()
        company = str(rec.get("company") or rec.get("organization") or "").strip()
        rows.append(
            {
                "recruiter_id": rec_id,
                "name": name[:120],
                "title": title[:120],
                "company": company[:120] or "India",
                "verified": bool(rec.get("verified", True)),
                "image_urls": _recruiter_image_candidates(rec),
            }
        )
    return rows


def build_recruiter_carousel_template_outbound(
    parsed: dict,
    search_context: str,
    interactive_buttons: List[Dict[str, str]],
    *,
    template_name: str = VAISH_RECRUITER_CAROUSEL_TEMPLATE,
    language: str = VAISH_RECRUITER_CAROUSEL_LANG,
) -> Optional[Dict[str, Any]]:
    rows = _recruiter_rows(parsed, interactive_buttons)
    if len(rows) < 2:
        return None

    context = str(search_context or "your search").strip()
    if len(context) > 80:
        context = context[:77] + "..."

    return {
        "type": "template",
        "carousel_kind": "recruiter",
        "template_name": template_name,
        "language": language,
        "search_context": context,
        "cards": rows,
        "card_count": VAISH_RECRUITER_CAROUSEL_CARD_COUNT,
        "pending_buttons": [
            {"id": r["recruiter_id"], "title": "View profile", "label": r["name"]} for r in rows
        ],
    }


def _card_label(card: Dict[str, Any]) -> str:
    name = str(card.get("name") or "Recruiter")
    title = str(card.get("title") or "").strip()
    company = str(card.get("company") or "India").strip()
    if title:
        return f"{name} · {title}, {company}"[:VAISH_CAROUSEL_CARD_PARAM_MAX]
    return f"{name}, {company}"[:VAISH_CAROUSEL_CARD_PARAM_MAX]


def build_carousel_template_components(
    service: "WhatsAppMessagingService",
    *,
    search_context: str,
    cards: List[Dict[str, Any]],
    card_count: int = VAISH_RECRUITER_CAROUSEL_CARD_COUNT,
) -> Optional[List[Dict[str, Any]]]:
    if not cards:
        return None

    padded: List[Dict[str, Any]] = list(cards[:card_count])
    while len(padded) < card_count:
        padded.append(
            {
                "recruiter_id": f"pad_{len(padded)}",
                "name": "More recruiters",
                "title": "Recruiter",
                "company": "Tap View profile",
                "image_urls": cards[0].get("image_urls") if cards else [],
            }
        )

    carousel_cards: List[Dict[str, Any]] = []
    for idx, card in enumerate(padded[:card_count]):
        media_id = None
        for url in card.get("image_urls") or []:
            media_id = service.upload_media_from_url(str(url))
            if media_id:
                break
        if not media_id:
            logger.warning(
                "[vaish_carousel] no media for card %s recruiter=%s",
                idx,
                card.get("recruiter_id"),
            )
            return None

        card_components: List[Dict[str, Any]] = [
            {
                "type": "header",
                "parameters": [{"type": "image", "image": {"id": media_id}}],
            },
            {
                "type": "body",
                "parameters": [{"type": "text", "text": _card_label(card)}],
            },
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": "0",
                "parameters": [
                    {
                        "type": "payload",
                        "payload": str(card.get("recruiter_id") or f"card_{idx}"),
                    }
                ],
            },
        ]
        carousel_cards.append({"card_index": idx, "components": card_components})

    return [
        {"type": "body", "parameters": [{"type": "text", "text": search_context[:1024]}]},
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
    search_context = str(
        vars_.get("search_context")
        or vars_.get("role")
        or vars_.get("job_title")
        or vars_.get("query")
        or ""
    ).strip()
    if not search_context:
        text_path = spec.get("textPath") or spec.get("messagePath")
        search_context = str(
            get_path(ctx, text_path) if text_path else spec.get("fallbackText") or "your search"
        )

    template_name = spec.get("carouselTemplateName") or VAISH_RECRUITER_CAROUSEL_TEMPLATE
    language = spec.get("carouselTemplateLanguage") or VAISH_RECRUITER_CAROUSEL_LANG

    return build_recruiter_carousel_template_outbound(
        ctx,
        search_context,
        interactive_buttons,
        template_name=template_name,
        language=language,
    )


def dummy_recruiters() -> List[Dict[str, Any]]:
    """Sample recruiters for partner API tests / template previews."""
    samples = [
        ("Neha Gupta", "HR Director", "Infosys", 1),
        ("Rahul Sharma", "Senior Recruiter", "TCS", 2),
        ("Priya Nair", "Talent Lead", "Wipro", 3),
        ("Amit Verma", "Hiring Manager", "HCL", 4),
        ("Sneha Reddy", "Campus Recruiter", "Accenture", 5),
    ]
    rows: List[Dict[str, Any]] = []
    for idx, (name, title, company, img) in enumerate(samples, start=1):
        rows.append(
            {
                "recruiter_id": f"dummy-rec-{idx}",
                "name": name,
                "title": title,
                "company": company,
                "verified": True,
                "image_urls": [f"https://i.pravatar.cc/512?img={img}"],
            }
        )
    return rows
