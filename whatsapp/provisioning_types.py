"""
WhatsApp Provisioning Types
============================
Data structures for the canonical provisioning pipeline.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Provisioning state machine
PROV_OAUTH_RECEIVED = "oauth_received"
PROV_TOKEN_EXCHANGED = "token_exchanged"
PROV_PHONE_REGISTERED = "phone_registered"
PROV_WEBHOOK_SUBSCRIBED = "webhook_subscribed"
PROV_TEMPLATES_SYNCED = "templates_synced"
PROV_WARMUP_STARTED = "warmup_started"
PROV_HEALTH_VERIFIED = "health_verified"
PROV_READY = "ready"

PROVISIONING_STATES = [
    PROV_OAUTH_RECEIVED,
    PROV_TOKEN_EXCHANGED,
    PROV_PHONE_REGISTERED,
    PROV_WEBHOOK_SUBSCRIBED,
    PROV_TEMPLATES_SYNCED,
    PROV_WARMUP_STARTED,
    PROV_HEALTH_VERIFIED,
    PROV_READY,
]

# Operational lifecycle (Connected vs Operational distinction)
OP_CONNECTED = "connected"
OP_PROVISIONED = "provisioned"
OP_OPERATIONAL = "operational"
OP_TRUSTED = "trusted"
OP_DEGRADED = "degraded"
OP_RESTRICTED = "restricted"

# Capability keys
CAP_TOKEN_VALID = "token_valid"
CAP_TOKEN_SCOPES = "token_scopes_valid"
CAP_TOKEN_APP_OWNERSHIP = "token_belongs_to_app"
CAP_PHONE_REGISTERED = "phone_registered"
CAP_WEBHOOK_SUBSCRIBED = "webhook_subscribed"
CAP_WEBHOOK_FIELDS_COMPLETE = "webhook_fields_complete"
CAP_CAN_SEND = "can_send_messages"
CAP_DISPLAY_NAME_APPROVED = "display_name_approved"
CAP_BUSINESS_PROFILE = "business_profile_complete"
CAP_QUALITY_HEALTHY = "quality_healthy"
CAP_NOT_RESTRICTED = "account_not_restricted"
CAP_TEMPLATES_SYNCED = "templates_synced"
CAP_MESSAGING_TIER = "messaging_tier_valid"
CAP_WARMUP_ACKNOWLEDGED = "warmup_acknowledged"

# Required scopes
REQUIRED_SCOPES = {"whatsapp_business_messaging", "whatsapp_business_management"}
ADVISORY_SCOPES = {"business_management"}

# Webhook fields that MUST be subscribed
REQUIRED_WEBHOOK_FIELDS = [
    "messages",
    "account_update",
    "message_template_status_update",
    "message_template_quality_update",
    "template_category_update",
    "message_echoes",
    "smb_message_echoes",
]

# Meta coexistence / WhatsApp Business app onboarding (solution partner extras)
COEXISTENCE_WEBHOOK_FIELDS = [
    "history",
    "smb_app_state_sync",
]

FULL_WEBHOOK_FIELDS = REQUIRED_WEBHOOK_FIELDS + COEXISTENCE_WEBHOOK_FIELDS

# Scoring weights
CRITICAL_CAPABILITIES = {
    CAP_TOKEN_VALID, CAP_TOKEN_SCOPES, CAP_PHONE_REGISTERED,
    CAP_WEBHOOK_SUBSCRIBED, CAP_CAN_SEND,
}
IMPORTANT_CAPABILITIES = {
    CAP_DISPLAY_NAME_APPROVED, CAP_BUSINESS_PROFILE,
    CAP_QUALITY_HEALTHY, CAP_NOT_RESTRICTED,
}
ADVISORY_CAPABILITIES = {
    CAP_TEMPLATES_SYNCED, CAP_MESSAGING_TIER,
    CAP_WARMUP_ACKNOWLEDGED, CAP_TOKEN_APP_OWNERSHIP,
    CAP_WEBHOOK_FIELDS_COMPLETE,
}
WEIGHT_CRITICAL = 10   # 5 caps × 10 = 50
WEIGHT_IMPORTANT = 8   # 4 caps × 8  = 32
WEIGHT_ADVISORY = 4    # 5 caps × 4  = 20  (total max ~102, normalised to 100)


@dataclass
class CapabilityStatus:
    enabled: bool
    reason: Optional[str] = None
    auto_fixable: bool = False
    fixed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "reason": self.reason,
            "auto_fixable": self.auto_fixable,
            "fixed": self.fixed,
        }


@dataclass
class ProvisioningCheck:
    name: str
    status: str  # healthy | warning | error | critical
    message: str
    auto_fix_available: bool = False
    fixed: bool = False
    fix_message: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "auto_fix_available": self.auto_fix_available,
            "fixed": self.fixed,
        }
        if self.fix_message:
            d["fix_message"] = self.fix_message
        if self.details:
            d["details"] = self.details
        return d


@dataclass
class ProvisioningResult:
    success: bool
    readiness_score: int = 0
    provisioning_state: str = PROV_OAUTH_RECEIVED
    operational_lifecycle: str = OP_CONNECTED
    capability_matrix: Dict[str, CapabilityStatus] = field(default_factory=dict)
    checks: List[ProvisioningCheck] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    token_debug: Dict[str, Any] = field(default_factory=dict)
    warmup_state: Dict[str, Any] = field(default_factory=dict)
    action_required: Optional[str] = None
    source: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "readiness_score": self.readiness_score,
            "provisioning_state": self.provisioning_state,
            "operational_lifecycle": self.operational_lifecycle,
            "capability_matrix": {
                k: v.to_dict() for k, v in self.capability_matrix.items()
            },
            "checks": [c.to_dict() for c in self.checks],
            "warnings": self.warnings,
            "errors": self.errors,
            "token_debug": self.token_debug,
            "warmup_state": self.warmup_state,
            "action_required": self.action_required,
            "source": self.source,
        }


def compute_readiness_score(caps: Dict[str, CapabilityStatus]) -> int:
    raw = 0.0
    max_raw = 0.0
    for key, status in caps.items():
        if key in CRITICAL_CAPABILITIES:
            w = WEIGHT_CRITICAL
        elif key in IMPORTANT_CAPABILITIES:
            w = WEIGHT_IMPORTANT
        elif key in ADVISORY_CAPABILITIES:
            w = WEIGHT_ADVISORY
        else:
            w = WEIGHT_ADVISORY
        max_raw += w
        if status.enabled:
            raw += w
    if max_raw <= 0:
        return 0
    return min(100, int(round(raw / max_raw * 100)))


def determine_operational_lifecycle(
    caps: Dict[str, CapabilityStatus],
    score: int,
    in_warmup: bool,
    restriction_state: str,
) -> str:
    if restriction_state and restriction_state not in ("none", ""):
        return OP_RESTRICTED
    critical_ok = all(
        caps.get(k, CapabilityStatus(enabled=False)).enabled
        for k in CRITICAL_CAPABILITIES
    )
    if not critical_ok:
        return OP_DEGRADED
    if in_warmup:
        return OP_PROVISIONED
    if score >= 80:
        return OP_OPERATIONAL if score < 95 else OP_TRUSTED
    return OP_PROVISIONED
