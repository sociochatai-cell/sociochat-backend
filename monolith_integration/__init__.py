"""
Monolith-side integration for WhatsApp capability projection + usage events.

Designed to run inside the Sociovia backend process (shared DB + HTTP to WhatsApp API).
See ``README.md`` in this package for env vars and the usage-consumer CLI.
"""

from .accounting import default_message_sent_billing_handler
from .capability_client import WhatsappCapabilitiesClient
from .metrics import integration_metrics
from .trigger import (
    schedule_capabilities_resync_for_accounts,
    schedule_capabilities_resync_for_user,
    schedule_capabilities_resync_for_workspace,
)
from .usage_consumer import WhatsappUsageEventConsumer

__all__ = [
    "WhatsappCapabilitiesClient",
    "WhatsappUsageEventConsumer",
    "default_message_sent_billing_handler",
    "integration_metrics",
    "schedule_capabilities_resync_for_user",
    "schedule_capabilities_resync_for_workspace",
    "schedule_capabilities_resync_for_accounts",
]
