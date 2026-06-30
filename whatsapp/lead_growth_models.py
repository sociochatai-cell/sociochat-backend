"""
WhatsApp Lead Growth Settings (per-workspace auto-discovery config)
===================================================================

Backs the lead auto-discovery / intent-routing / nurture feature with a single
per-workspace settings row on the shared Postgres DB.

The table `whatsapp_lead_growth_settings` is keyed by `workspace_id` (UNIQUE) and
holds the toggles + tunables that gate the inbound-message lead pipeline:

  * intent_rules          {intent_label: stage_key}  — how a classified intent maps
                          to a CRM pipeline stage when auto-creating/updating a lead.
  * confidence_threshold  float — minimum classifier confidence before we act.
  * auto_discovery_enabled bool — master switch for classifying every inbound msg
                          and auto-creating/updating a lead from high-intent ones.
  * notify_enabled        bool — whether to fire a notification on a new lead.
  * notify_template       str — template id/name for that notification.
  * notify_destinations   [phone strings] — where to send the notification.
  * nurture_enabled       bool — enroll new leads into matching 'new_lead' drips.

IMPORTANT TYPE NOTE: like the rest of the CRM bridge, `workspace_id` here is
INTEGER (matching leads.workspace_id / workspaces2.id). The WhatsApp service
carries workspace_id as a STRING elsewhere — always `int(workspace_id)` before
querying.

The model uses `__table_args__ = {"extend_existing": True}` so it never clashes
with any mapper the monolith may already have registered for the same table.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from shared_models import db
from sqlalchemy.dialects.postgresql import JSONB

logger = logging.getLogger(__name__)

# ── Process-level TTL cache for get_lead_growth_settings ──────────────────────
# get_lead_growth_settings runs on EVERY inbound message via fast_router, so an
# uncached SELECT per inbound is wasteful. Cache the resolved settings dict per
# workspace_id for a short TTL. Best-effort and never raises: any cache failure
# falls through to a live query. Keyed by ws_id -> (expiry_monotonic, dict).
# Uses time.monotonic so it is immune to wall-clock adjustments.
_SETTINGS_CACHE_TTL_SECONDS = 60.0
_settings_cache: Dict[int, Tuple[float, Dict[str, Any]]] = {}


def _cache_get(ws_id: int) -> Optional[Dict[str, Any]]:
    """Return a cached, non-expired settings dict (copy) for ws_id, else None."""
    try:
        entry = _settings_cache.get(ws_id)
        if entry is None:
            return None
        expiry, value = entry
        if time.monotonic() >= expiry:
            _settings_cache.pop(ws_id, None)
            return None
        # Return a copy so callers cannot mutate the cached object.
        return dict(value)
    except Exception:
        return None


def _cache_put(ws_id: int, value: Dict[str, Any]) -> None:
    """Store a settings dict (copy) for ws_id with a fresh TTL. Best-effort."""
    try:
        _settings_cache[ws_id] = (time.monotonic() + _SETTINGS_CACHE_TTL_SECONDS, dict(value))
    except Exception:
        pass


def _cache_invalidate(ws_id: int) -> None:
    """Drop any cached settings for ws_id (called on write). Best-effort."""
    try:
        _settings_cache.pop(ws_id, None)
    except Exception:
        pass


# Defaults returned by get_lead_growth_settings when no row exists for a workspace.
_DEFAULT_CONFIDENCE_THRESHOLD = 0.55

_DEFAULTS: Dict[str, Any] = {
    "intent_rules": {},
    "confidence_threshold": _DEFAULT_CONFIDENCE_THRESHOLD,
    "auto_discovery_enabled": False,
    "notify_enabled": False,
    "notify_template": None,
    "notify_destinations": [],
    "nurture_enabled": False,
}


class WhatsAppLeadGrowthSettings(db.Model):
    """Per-workspace lead auto-discovery / intent-routing / nurture settings."""

    __tablename__ = "whatsapp_lead_growth_settings"
    __table_args__ = {"extend_existing": True}

    id = db.Column(db.Integer, primary_key=True)
    # INTEGER to match leads.workspace_id / workspaces2.id. UNIQUE: one row per ws.
    workspace_id = db.Column(db.Integer, nullable=False, unique=True, index=True)

    # {intent_label: stage_key} — how a classified intent routes a lead's stage.
    intent_rules = db.Column(JSONB, nullable=False, default=dict)

    # Minimum classifier confidence (0..1) before auto-discovery acts.
    confidence_threshold = db.Column(db.Float, nullable=False, default=_DEFAULT_CONFIDENCE_THRESHOLD)

    # Master switch for classify-every-inbound + auto lead create/update.
    auto_discovery_enabled = db.Column(db.Boolean, nullable=False, default=False)

    # Notification on new lead.
    notify_enabled = db.Column(db.Boolean, nullable=False, default=False)
    notify_template = db.Column(db.String(255), nullable=True)
    # [phone strings] notified when a new lead is discovered.
    notify_destinations = db.Column(JSONB, nullable=False, default=list)

    # Enroll newly-discovered leads into matching 'new_lead' drip campaigns.
    nurture_enabled = db.Column(db.Boolean, nullable=False, default=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict with safe defaults for nullable JSON columns."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "intent_rules": self.intent_rules if isinstance(self.intent_rules, dict) else {},
            "confidence_threshold": (
                float(self.confidence_threshold)
                if self.confidence_threshold is not None
                else _DEFAULT_CONFIDENCE_THRESHOLD
            ),
            "auto_discovery_enabled": bool(self.auto_discovery_enabled),
            "notify_enabled": bool(self.notify_enabled),
            "notify_template": self.notify_template,
            "notify_destinations": (
                self.notify_destinations if isinstance(self.notify_destinations, list) else []
            ),
            "nurture_enabled": bool(self.nurture_enabled),
        }


def get_lead_growth_settings(workspace_id: Any) -> Dict[str, Any]:
    """Return the lead-growth settings for a workspace as a dict, or defaults.

    Best-effort: never raises. A non-integer workspace_id, a missing row, or any
    query failure all degrade to the defaults dict (with auto-discovery OFF), so
    callers can treat the result as a plain feature-flag bundle.
    """
    defaults = dict(_DEFAULTS)
    try:
        ws_id = int(workspace_id)
    except (TypeError, ValueError):
        logger.debug("[lead_growth] non-integer workspace_id=%r — returning defaults", workspace_id)
        return defaults

    # Serve from the process-level TTL cache when fresh (avoids a SELECT per inbound).
    cached = _cache_get(ws_id)
    if cached is not None:
        return cached

    try:
        row = (
            db.session.query(WhatsAppLeadGrowthSettings)
            .filter(WhatsAppLeadGrowthSettings.workspace_id == ws_id)
            .first()
        )
    except Exception:
        logger.exception("[lead_growth] settings query failed (best-effort) ws=%s", ws_id)
        return defaults

    # Cache the resolved result (the row dict, or defaults when no row exists) so
    # repeated inbound lookups within the TTL window skip the query entirely.
    result = dict(defaults) if row is None else row.to_dict()
    _cache_put(ws_id, result)
    return result


# Fields that the config API is allowed to write. Anything else in the request body
# is ignored. Each maps to a column on WhatsAppLeadGrowthSettings.
_WRITABLE_FIELDS = (
    "intent_rules",
    "confidence_threshold",
    "auto_discovery_enabled",
    "notify_enabled",
    "notify_template",
    "notify_destinations",
    "nurture_enabled",
)


def set_lead_growth_settings(workspace_id: Any, updates: Dict[str, Any]) -> Dict[str, Any]:
    """UPSERT the lead-growth settings row for a workspace and return its dict.

    Only keys in ``_WRITABLE_FIELDS`` are applied; unknown keys are ignored. Values
    are lightly coerced/validated:
      * intent_rules        -> dict (non-dict ignored)
      * confidence_threshold-> float clamped to [0.0, 1.0]
      * *_enabled           -> bool
      * notify_template     -> str or None
      * notify_destinations -> list of non-empty strings

    Raises ValueError for a non-integer workspace_id (callers map this to HTTP 400);
    DB errors propagate so the route can surface a 500. The caller owns commit.
    """
    try:
        ws_id = int(workspace_id)
    except (TypeError, ValueError):
        raise ValueError("workspace_id must be an integer")

    row = (
        db.session.query(WhatsAppLeadGrowthSettings)
        .filter(WhatsAppLeadGrowthSettings.workspace_id == ws_id)
        .first()
    )
    if row is None:
        row = WhatsAppLeadGrowthSettings(workspace_id=ws_id)
        db.session.add(row)

    updates = updates or {}

    if "intent_rules" in updates:
        value = updates.get("intent_rules")
        if isinstance(value, dict):
            # Coerce to {str: str} and drop empty mappings.
            row.intent_rules = {
                str(k): str(v)
                for k, v in value.items()
                if k is not None and v is not None and str(v).strip()
            }

    if "confidence_threshold" in updates:
        try:
            threshold = float(updates.get("confidence_threshold"))
            row.confidence_threshold = max(0.0, min(1.0, threshold))
        except (TypeError, ValueError):
            pass

    for flag in ("auto_discovery_enabled", "notify_enabled", "nurture_enabled"):
        if flag in updates:
            setattr(row, flag, bool(updates.get(flag)))

    if "notify_template" in updates:
        template = updates.get("notify_template")
        row.notify_template = str(template).strip() if template else None

    if "notify_destinations" in updates:
        value = updates.get("notify_destinations")
        if isinstance(value, list):
            row.notify_destinations = [
                str(d).strip() for d in value if d is not None and str(d).strip()
            ]

    row.updated_at = datetime.utcnow()
    db.session.flush()
    # Invalidate the TTL cache so the next inbound reads the freshly-written row
    # instead of a stale cached copy. Best-effort.
    _cache_invalidate(ws_id)
    return row.to_dict()
