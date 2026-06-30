"""
Shared types and constants for the warmup / safe-messaging subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Stored in whatsapp_accounts.onboarding_lifecycle_state (extend-only string contract)
LIFECYCLE_ONBOARDING = "onboarding"
LIFECYCLE_WARMUP = "warmup"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_TRUSTED = "trusted"
LIFECYCLE_RESTRICTED = "restricted"

# Operational mode (advisory-first; no hard bans via this field alone)
OP_NORMAL = "normal"
OP_ADVISORY_WARMUP = "advisory_warmup"


@dataclass
class WarmupOperationalSignals:
    """Read-only inputs for evaluation (no side effects)."""

    account_age_days: float
    quality_rating: Optional[str]
    webhook_health: str
    onboarding_complete: bool
    restriction_state: str
    restriction_reason: Optional[str]
    is_coexistence: bool
    message_health: Optional[Dict[str, Any]] = None


@dataclass
class WarmupGateDenial:
    """Returned by enforcement hooks; mapped to CapabilityResult in capabilities."""

    message: str
    code: str = "warmup_denied"


@dataclass
class WarmupEvaluation:
    """Computed view for APIs + enforcement."""

    lifecycle_effective: str
    in_warmup_window: bool
    advisory_safe_mode: bool
    safe_mode_reasons: List[str] = field(default_factory=list)
    effective_daily_send_cap: Optional[int] = None
    blocks: Dict[str, bool] = field(default_factory=dict)
    block_reasons: Dict[str, str] = field(default_factory=dict)
    countdown_seconds: Optional[int] = None
    rules_profile: str = "default"
    diagnostics: Dict[str, Any] = field(default_factory=dict)
