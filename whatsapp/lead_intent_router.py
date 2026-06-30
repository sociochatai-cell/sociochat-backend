"""
WhatsApp Lead Intent Router
===========================

A self-contained, keyword-based intent classifier for inbound WhatsApp messages.

This is a direct port of the Instagram growth module's `IgIntentRouter.analyze_message`
(Sociovia/market_research_social/ig_growth_service.py) plus its `INTENT_KEYWORDS`
table and entity-extraction helpers. It deliberately imports NOTHING from the
monolith — everything it needs (keyword scorer, entity extraction, sentiment) is
inlined here so the WhatsApp service can classify inbound text without a cross-
service dependency.

Public surface:
    classify_lead_intent(text) -> {
        "intent": str,            # top intent label, or "unknown"
        "confidence": float,      # 0.0 .. 0.96
        "sentiment": str,         # "positive" | "neutral" | "negative"
        "entities": {name,email,phone,company,product,order_id},
        "scores": {intent_label: float, ...},
    }

Everything here is pure/best-effort: it never raises on bad input (empty / None /
non-string is treated as an empty message that yields intent="unknown").
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# ── Keyword table (ported verbatim from IG INTENT_KEYWORDS) ──────────────────
INTENT_KEYWORDS = {
    "pricing": [
        "price",
        "pricing",
        "cost",
        "rate",
        "quotation",
        "quote",
        "how much",
    ],
    "catalog": [
        "catalog",
        "catalogue",
        "collection",
        "products",
        "product list",
        "show me",
        "available",
    ],
    "shipping": [
        "shipping",
        "delivery",
        "deliver",
        "arrival",
        "arrive",
        "track",
        "tracking",
    ],
    "returns": [
        "return",
        "refund",
        "replace",
        "exchange",
        "cancel order",
        "cancellation",
    ],
    "purchase": [
        "buy",
        "purchase",
        "order",
        "checkout",
        "cart",
        "cod",
        "cash on delivery",
        "payment link",
    ],
    "support": [
        "help",
        "issue",
        "problem",
        "support",
        "complaint",
        "agent",
        "human",
        "executive",
    ],
    "lead_capture": [
        "call me",
        "contact me",
        "interested",
        "demo",
        "book",
        "enquiry",
        "inquiry",
        "wholesale",
        "bulk order",
    ],
}

POSITIVE_SENTIMENT_TERMS = {
    "great",
    "good",
    "awesome",
    "love",
    "nice",
    "perfect",
    "interested",
    "want",
    "buy",
}
NEGATIVE_SENTIMENT_TERMS = {
    "bad",
    "poor",
    "hate",
    "worst",
    "issue",
    "problem",
    "angry",
    "refund",
    "return",
}

# ── Entity extraction patterns (ported from IgLeadCaptureService) ────────────
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?:(?:\+?\d{1,3})?[\s\-()]*)?(?:\d[\s\-()]*){8,15}")
_ORDER_RE = re.compile(r"\b(?:order|tracking|awb|id)[\s:#-]*([A-Z0-9-]{5,})\b", re.I)


def _normalize_phone(value: Optional[str]) -> Optional[str]:
    """Light phone normalizer (mirrors the IG helper): keep digits/+, require >=8 digits."""
    if not value:
        return None
    digits = re.sub(r"[^\d+]", "", str(value))
    if len(re.sub(r"\D", "", digits)) < 8:
        return None
    return digits


def _extract_entities(
    message_text: str, variables: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Extract {name,email,phone,company,product,order_id} from free text + optional vars.

    Ported from IgLeadCaptureService.extract_entities. `variables` is an optional
    dict of already-collected fields (e.g. flow-collected values) used as fallbacks.
    """
    text = (message_text or "").strip()
    variables = variables or {}

    email = None
    email_match = _EMAIL_RE.search(text)
    if email_match:
        email = email_match.group(0).strip()
    else:
        for key in ("email", "mail"):
            if variables.get(key):
                email = str(variables.get(key)).strip()
                break

    phone = None
    phone_match = _PHONE_RE.search(text)
    if phone_match:
        phone = _normalize_phone(phone_match.group(0))
    if not phone:
        for key in ("phone", "mobile", "phone_number", "whatsapp"):
            if variables.get(key):
                phone = _normalize_phone(str(variables.get(key)))
                if phone:
                    break

    name = None
    for key in ("name", "full_name", "customer_name"):
        if variables.get(key):
            name = str(variables.get(key)).strip()
            break
    if not name:
        name_match = re.search(r"\b(?:i am|i'm|my name is)\s+([a-z][a-z .'-]{1,50})", text, re.I)
        if name_match:
            name = name_match.group(1).strip().title()

    company = None
    for key in ("company", "brand", "business_name"):
        if variables.get(key):
            company = str(variables.get(key)).strip()
            break

    product = None
    for key in ("product", "sku", "item", "collection", "category"):
        if variables.get(key):
            product = str(variables.get(key)).strip()
            break

    order_id = None
    order_match = _ORDER_RE.search(text)
    if order_match:
        order_id = order_match.group(1).strip()
    elif variables.get("order_id"):
        order_id = str(variables.get("order_id")).strip()

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "company": company,
        "product": product,
        "order_id": order_id,
    }


def analyze_message(
    message_text: str, variables: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Keyword intent classifier (port of IgIntentRouter.analyze_message).

    Returns {intent, confidence, sentiment, entities, scores}. Never raises.
    """
    text = (message_text or "").strip()
    text_lower = text.lower()
    scores: Dict[str, float] = {}

    for intent_name, keywords in INTENT_KEYWORDS.items():
        score = 0.0
        for keyword in keywords:
            if keyword in text_lower:
                score += 0.22 if " " in keyword else 0.16
        if score > 0:
            scores[intent_name] = min(score, 0.96)

    extracted = _extract_entities(text, variables=variables)
    if extracted.get("email") or extracted.get("phone"):
        scores["lead_capture"] = max(scores.get("lead_capture", 0.0), 0.82)

    purchase_boosters = ("buy", "order", "checkout", "cart", "payment")
    if any(term in text_lower for term in purchase_boosters):
        scores["purchase"] = max(scores.get("purchase", 0.0), 0.72)

    sentiment = "neutral"
    if any(term in text_lower for term in NEGATIVE_SENTIMENT_TERMS):
        sentiment = "negative"
    elif any(term in text_lower for term in POSITIVE_SENTIMENT_TERMS):
        sentiment = "positive"

    if scores:
        top_intent = max(scores, key=scores.get)
        confidence = round(scores[top_intent], 3)
    else:
        top_intent = "unknown"
        confidence = 0.0

    return {
        "intent": top_intent,
        "confidence": confidence,
        "sentiment": sentiment,
        "entities": extracted,
        "scores": {key: round(value, 3) for key, value in scores.items()},
    }


def classify_lead_intent(text: str) -> Dict[str, Any]:
    """Module-level convenience entry point.

    Thin wrapper over `analyze_message` for inbound WhatsApp text. Best-effort:
    any unexpected failure degrades to an 'unknown' classification rather than
    raising into the flow engine.
    """
    try:
        return analyze_message(text)
    except Exception:
        return {
            "intent": "unknown",
            "confidence": 0.0,
            "sentiment": "neutral",
            "entities": {
                "name": None,
                "email": None,
                "phone": None,
                "company": None,
                "product": None,
                "order_id": None,
            },
            "scores": {},
        }
