"""
Warmup configuration: env + optional JSON overrides by country / account type.

No I/O except reading process environment at call time (test-friendly).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmupRuleProfile:
    """Per-profile tuning (merged over default)."""

    duration_days: int
    daily_send_cap: int
    block_broadcast: bool = True
    block_drip: bool = True
    block_aggressive_automation: bool = True
    throttle_ai: bool = True
    ai_max_tokens_cap: int = 512
    ai_temperature_max: float = 0.35


DEFAULT_PROFILE = WarmupRuleProfile(
    duration_days=7,
    daily_send_cap=80,
    block_broadcast=True,
    block_drip=True,
    block_aggressive_automation=True,
    throttle_ai=True,
    ai_max_tokens_cap=512,
    ai_temperature_max=0.35,
)


def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def warmup_enabled() -> bool:
    return _env_bool("WH_WARMUP_ENABLED", True)


def default_warmup_days() -> int:
    try:
        return max(1, int(os.getenv("WH_WARMUP_DEFAULT_DAYS", "7")))
    except (TypeError, ValueError):
        return 7


def maturity_skip_days() -> int:
    """Existing accounts older than this never auto-enter warmup on reconnect."""
    try:
        return max(1, int(os.getenv("WH_WARMUP_MATURITY_SKIP_DAYS", "30")))
    except (TypeError, ValueError):
        return 30


def _parse_rules_json(raw: str) -> Dict[str, Any]:
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        logger.warning("WH_WARMUP_RULES_JSON invalid JSON: %s", e)
        return {}


def load_rule_profiles() -> Dict[str, Dict[str, Any]]:
    """
    WH_WARMUP_RULES_JSON example:
    {
      "default": {"duration_days": 7, "daily_send_cap": 80},
      "US": {"daily_send_cap": 120},
      "IN": {"daily_send_cap": 60},
      "coexistence": {"duration_days": 10, "daily_send_cap": 40}
    }
    """
    blob = _parse_rules_json(os.getenv("WH_WARMUP_RULES_JSON", "") or "")
    if not blob:
        return {"default": {}}
    return blob


def resolve_profile_key(country_code: Optional[str], is_coexistence: bool) -> str:
    if is_coexistence:
        return "coexistence"
    cc = (country_code or "").strip().upper()
    if cc and cc != "DEFAULT":
        return cc
    return "default"


def merged_profile(country_code: Optional[str], is_coexistence: bool) -> WarmupRuleProfile:
    profiles = load_rule_profiles()
    base = profiles.get("default") or {}
    key = resolve_profile_key(country_code, is_coexistence)
    overlay = profiles.get(key) or {}
    merged: Dict[str, Any] = {**base, **overlay}

    def _int(name: str, fallback: int) -> int:
        try:
            return int(merged.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    def _float(name: str, fallback: float) -> float:
        try:
            return float(merged.get(name, fallback))
        except (TypeError, ValueError):
            return fallback

    dd = _int("duration_days", default_warmup_days())
    cap = _int("daily_send_cap", DEFAULT_PROFILE.daily_send_cap)
    return WarmupRuleProfile(
        duration_days=max(1, dd),
        daily_send_cap=max(1, cap),
        block_broadcast=bool(merged.get("block_broadcast", DEFAULT_PROFILE.block_broadcast)),
        block_drip=bool(merged.get("block_drip", DEFAULT_PROFILE.block_drip)),
        block_aggressive_automation=bool(
            merged.get("block_aggressive_automation", DEFAULT_PROFILE.block_aggressive_automation)
        ),
        throttle_ai=bool(merged.get("throttle_ai", DEFAULT_PROFILE.throttle_ai)),
        ai_max_tokens_cap=max(128, _int("ai_max_tokens_cap", DEFAULT_PROFILE.ai_max_tokens_cap)),
        ai_temperature_max=min(1.0, max(0.0, _float("ai_temperature_max", DEFAULT_PROFILE.ai_temperature_max))),
    )
