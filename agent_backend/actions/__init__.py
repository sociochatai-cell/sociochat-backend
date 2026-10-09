"""
Agent Action Modules — auto-discovery.
=======================================

Importing this package automatically loads all action modules
so they register themselves with the ActionRegistry.
"""

from . import (
    account_actions,
    template_actions,
    drip_actions,
    bulk_actions,
    automation_actions,
    messaging_actions,
    analytics_actions,
    navigation_actions,
    crm_actions,
    dataset_actions,
    booking_actions,
    faq_actions,
    knowledge_actions,
    contact_actions,
    order_actions,
    flow_actions,
    payment_actions,
    summary_actions,
)

__all__ = [
    "account_actions",
    "template_actions",
    "drip_actions",
    "bulk_actions",
    "automation_actions",
    "messaging_actions",
    "analytics_actions",
    "navigation_actions",
    "crm_actions",
    "dataset_actions",
    "booking_actions",
    "faq_actions",
    "knowledge_actions",
    "contact_actions",
    "order_actions",
    "flow_actions",
    "payment_actions",
    "summary_actions",
]
