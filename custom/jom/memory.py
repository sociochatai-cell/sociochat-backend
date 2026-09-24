"""
JOM memory card — what we know about a traveller, and the block we inject into EVERY
AI turn so the agent (a) never re-asks something already collected and (b) can resume
after a days-long gap "as if the conversation never paused".

Two sources feed the card:
  1. the campaign / lead form (seeded before the first message), and
  2. the AI itself as it learns things mid-conversation.

`destination` is special: it starts as the CAMPAIGN destination but is overridable
mid-chat (Kashmir -> Bali). Whatever is on the card is the destination the AI should
talk about and retrieve knowledge for.

All functions are workspace-scoped; call behind is_jom_workspace(). Fail-safe: a
failure here must never break a reply, so callers get None/"" rather than an exception.
"""
import logging

from models import db
from .models import JomLifecycleState

logger = logging.getLogger(__name__)

# Card fields in the order we show them to the model.
FIELDS = ("customer_name", "destination", "budget", "travel_dates", "party_size")

_LABELS = {
    "customer_name": "Name",
    "destination": "Destination",
    "budget": "Budget",
    "travel_dates": "Travel dates",
    "party_size": "Travellers",
}


def _row(workspace_id, customer_phone):
    return JomLifecycleState.query.filter_by(
        workspace_id=int(workspace_id), customer_phone=str(customer_phone)).first()


def get_card(workspace_id, customer_phone):
    """Everything we know. Returns {} when there's no lead row yet."""
    try:
        row = _row(workspace_id, customer_phone)
        if not row:
            return {}
        card = {f: getattr(row, f, None) for f in FIELDS}
        card["preferences"] = getattr(row, "preferences", None) or {}
        card["last_summary"] = getattr(row, "last_summary", None)
        return {k: v for k, v in card.items() if v}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] get_card failed: %s", exc)
        return {}


def update_card(workspace_id, customer_phone, **fields):
    """Write learned/seeded attributes. Only non-empty values overwrite, so the AI can
    enrich the card without wiping what the lead form already gave us. Returns the row."""
    try:
        row = _row(workspace_id, customer_phone)
        if not row:
            row = JomLifecycleState(workspace_id=int(workspace_id), customer_phone=str(customer_phone))
            db.session.add(row)
        changed = []
        for key, val in fields.items():
            if val in (None, "", {}, []):
                continue
            if key == "preferences":
                merged = dict(getattr(row, "preferences", None) or {})
                if isinstance(val, dict):
                    merged.update(val)
                    row.preferences = merged
                    changed.append(key)
                continue
            if key in FIELDS or key == "last_summary":
                if getattr(row, key, None) != val:
                    setattr(row, key, val)
                    changed.append(key)
        if changed:
            db.session.commit()
            logger.info("[jom] memory updated for %s: %s", customer_phone, ", ".join(changed))
        return row
    except Exception as exc:  # noqa: BLE001
        logger.debug("[jom] update_card failed: %s", exc)
        return None


def set_destination(workspace_id, customer_phone, destination):
    """The traveller changed their mind (e.g. Kashmir -> Bali). Updating the card also
    changes which destination knowledge the AI retrieves from here on."""
    if not destination:
        return None
    row = update_card(workspace_id, customer_phone, destination=destination)
    logger.info("[jom] destination for %s set to %s", customer_phone, destination)
    return row


def known_fields(workspace_id, customer_phone):
    """Which facts we already have — the AI must NOT ask for these again."""
    card = get_card(workspace_id, customer_phone)
    return [f for f in FIELDS if card.get(f)]


def prompt_block(workspace_id, customer_phone):
    """The text injected into the AI's system instruction on every turn.
    Returns "" when we know nothing (so a brand-new lead gets no noise)."""
    card = get_card(workspace_id, customer_phone)
    if not card:
        return ""
    lines = []
    facts = [f"{_LABELS[f]}: {card[f]}" for f in FIELDS if card.get(f)]
    if facts:
        lines.append("WHAT YOU ALREADY KNOW ABOUT THIS PERSON (never ask these again):")
        lines.append("  " + " · ".join(facts))
    prefs = card.get("preferences") or {}
    if prefs:
        pretty = " · ".join(f"{k}: {v}" for k, v in prefs.items() if v)
        if pretty:
            lines.append(f"  Preferences: {pretty}")
    if card.get("last_summary"):
        lines.append(f"WHERE YOU LEFT OFF: {card['last_summary']}")
        lines.append("Continue warmly as if the conversation never paused — do not restart or re-introduce yourself.")
    if card.get("destination"):
        lines.append(f"Talk about {card['destination']} specifically, using the {card['destination']} knowledge you have.")
    return "\n".join(lines)
