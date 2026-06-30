"""
Meta Tech Provider / Tech Partner post-Embedded Signup helpers.
https://developers.facebook.com/docs/whatsapp/embedded-signup/onboarding-customers-as-a-tech-provider
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .provisioning_types import ProvisioningResult

META_WA_MANAGER_URL = "https://business.facebook.com/wa/manage/home/"
META_PAYMENT_METHOD_HELP_URL = "https://www.facebook.com/business/help/488291839463771"


def coexistence_next_steps() -> List[Dict[str, str]]:
    return [
        {
            "id": "keep_wa_business_app_open",
            "title": "Keep WhatsApp Business app open",
            "description": (
                "Contacts and chat history sync can take several minutes. "
                "Keep the WhatsApp Business app open on the primary device while sync runs."
            ),
        },
        {
            "id": "sync_within_24h",
            "title": "Sync completes within 24 hours",
            "description": (
                "Meta requires contacts and history sync within 24 hours of onboarding. "
                "Sociovia starts sync automatically after connect."
            ),
        },
    ]


def payment_method_next_step() -> Dict[str, str]:
    return {
        "id": "add_payment_method",
        "title": "Add payment method in Meta",
        "description": (
            "WhatsApp messaging is billed by Meta directly to your Business Manager — "
            "not through Sociovia. Add a payment method in WhatsApp Manager to complete onboarding."
        ),
        "url": META_WA_MANAGER_URL,
        "help_url": META_PAYMENT_METHOD_HELP_URL,
    }


def merge_connect_hints(
    data: Dict[str, Any],
    ob_session: Any = None,
) -> Dict[str, str]:
    """Merge Embedded Signup asset hints from request body and onboarding session."""
    hints: Dict[str, str] = {}
    for key in ("business_id", "waba_id", "phone_number_id"):
        raw = data.get(key)
        if raw:
            hints[key] = str(raw).strip()
    bm = data.get("business_manager_id") or data.get("meta_business_id")
    if bm and "business_id" not in hints:
        hints["business_id"] = str(bm).strip()

    if ob_session is not None:
        if getattr(ob_session, "business_manager_id", None):
            hints.setdefault("business_id", str(ob_session.business_manager_id).strip())
        if getattr(ob_session, "waba_id", None):
            hints.setdefault("waba_id", str(ob_session.waba_id).strip())
        if getattr(ob_session, "phone_number_id", None):
            hints.setdefault("phone_number_id", str(ob_session.phone_number_id).strip())

    return {k: v for k, v in hints.items() if v}


def apply_hint_overrides(binding: Dict[str, Any], hints: Dict[str, str]) -> Dict[str, Any]:
    """Prefer session-logged business_id when Graph discovery returns null."""
    out = dict(binding)
    if hints.get("business_id") and not out.get("meta_business_id"):
        out["meta_business_id"] = hints["business_id"]
    return out


def tech_provider_next_steps(
    provisioning_result: Optional["ProvisioningResult"] = None,
    *,
    include_payment: bool = True,
) -> List[Dict[str, str]]:
    steps: List[Dict[str, str]] = []
    if provisioning_result and getattr(provisioning_result, "action_required", None):
        steps.append(
            {
                "id": "fix_provisioning",
                "title": "Complete Meta setup",
                "description": str(provisioning_result.action_required),
            }
        )
    if include_payment:
        steps.append(payment_method_next_step())
    return steps
