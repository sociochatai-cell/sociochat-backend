"""
Map WhatsApp Flow JSON field ``name`` → UI ``label`` for inbox display.

Response payloads use opaque ``name`` values (e.g. ``MOI…_2_XOIC``). Labels are
defined on each screen component in ``flow_json``; we union maps from all
flows for the account (names rarely collide across flows on one WABA).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)

_FIELD_COMPONENT_TYPES: Set[str] = {
    "TextInput",
    "TextArea",
    "Dropdown",
    "RadioButtonsGroup",
    "CheckboxGroup",
    "DatePicker",
}


def _title_fallback(name: str) -> str:
    """Readable fallback when no label is set in flow JSON."""
    base = re.sub(r"^[A-Z0-9]{6,}_\d+_", "", name, flags=re.IGNORECASE)
    if not base or base == name:
        base = name
    parts = [p for p in re.split(r"[_\s]+", base) if p]
    if not parts:
        return name
    return " ".join(w[:1].upper() + w[1:].lower() if len(w) > 1 else w.upper() for w in parts)


def build_flow_field_label_map_for_account(account_id: int) -> Dict[str, str]:
    """
    Return ``{ field_name: display_label }`` for every input field across all
    draft/published flows owned by this WhatsApp account.
    """
    # Local import avoids circular imports at app startup
    from .models import WhatsAppFlow

    labels: Dict[str, str] = {}
    try:
        flows = WhatsAppFlow.query.filter_by(account_id=account_id).all()
    except Exception as exc:
        logger.warning("build_flow_field_label_map_for_account: query failed: %s", exc)
        return labels

    for flow in flows:
        fj = flow.flow_json
        if not isinstance(fj, dict):
            continue
        for screen in fj.get("screens") or []:
            if not isinstance(screen, dict):
                continue
            layout = screen.get("layout") or {}
            children = layout.get("children") or []
            for comp in children:
                if not isinstance(comp, dict):
                    continue
                if comp.get("type") not in _FIELD_COMPONENT_TYPES:
                    continue
                name = comp.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                name = name.strip()
                lab = comp.get("label") or comp.get("title")
                if isinstance(lab, str) and lab.strip():
                    labels[name] = lab.strip()
                elif name not in labels:
                    labels[name] = _title_fallback(name)
    return labels


_SKIP_RESPONSE_KEYS: Set[str] = {
    "flow_token",
    "wa_id",
    "data",
    "version",
    "action",
    "screen",
    "error",
    "errors",
    "endpoint_flow_data_merged",
    "endpoint_flow_completed_at",
}


def attach_flow_field_labels_to_nfm_content(content: Dict[str, Any], account_id: int) -> None:
    """
    Mutates ``content`` in place: sets ``flow_field_labels`` mapping each
    top-level response_json key to a display label (from flow JSON or fallback).
    """
    if content.get("interactive_type") != "nfm_reply":
        return
    rj = content.get("response_json")
    if not isinstance(rj, dict):
        return

    label_map = build_flow_field_label_map_for_account(account_id)
    out: Dict[str, str] = {}
    for key in rj.keys():
        if key in _SKIP_RESPONSE_KEYS or str(key).startswith("__"):
            continue
        if key in label_map:
            out[str(key)] = label_map[key]
        else:
            out[str(key)] = _title_fallback(str(key))

    if out:
        content["flow_field_labels"] = out
