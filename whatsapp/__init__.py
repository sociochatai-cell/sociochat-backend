"""
WhatsApp Module - Phase 1
=========================

Clean, isolated WhatsApp backend functionality.
All WhatsApp-related code lives here to keep the main project clean.

Structure:
    - models.py          → SQLAlchemy database models
    - services.py        → WhatsApp Cloud API messaging functions
    - routes.py          → Flask Blueprint with API endpoints
    - validators.py      → Request validation helpers
    - utils.py           → Utility functions
    - webhook.py         → Webhook processing logic
    - template_builder.py → Template payload generator
    - health_check.py    → Account health monitoring & auto-setup
    - automation_models.py    → Automation database models
    - automation_engine.py    → Automation rule processing
    - automation_routes.py    → Automation API endpoints
    - ai_chatbot.py           → AI-powered chatbot (Gemini)
    - ai_routes.py            → AI configuration endpoints

Usage:
    from whatsapp import whatsapp_bp, automation_bp, ai_bp
    app.register_blueprint(whatsapp_bp, url_prefix="/api/whatsapp")
    app.register_blueprint(automation_bp, url_prefix="/api/whatsapp")
    app.register_blueprint(ai_bp)  # Already has url_prefix
    app.register_blueprint(faq_bp)  # Already has url_prefix
"""

from .routes import whatsapp_bp
from .automation_routes import automation_bp
from .ai_routes import ai_bp
from .faq_routes import faq_bp
from .knowledge_routes import knowledge_bp
from .trigger_routes import trigger_bp
from .production_trigger_routes import production_trigger_bp
from .drip_routes import drip_bp
from .template_routes import template_bp
from .interactive_automation_routes import interactive_automation_bp
from .verification_routes import verification_bp
from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppWebhookLog,
    WhatsAppTemplate,
)
from .automation_models import (
    WhatsAppAutomationRule,
    WhatsAppAutomationLog,
    WhatsAppBusinessHours,
)
from .template_builder import (
    TemplateBuilder,
    build_simple_template,
    build_template_with_body_params,
    build_template_with_image_header,
)
from .health_check import (
    perform_health_check,
    perform_workspace_health_check,
    subscribe_waba_to_webhooks,
    setup_account_after_connection,
)

from .bulk_routes import bulk_bp
from .coexistence_routes import coexistence_bp
from .catalog_routes import catalog_bp
from .tracking_routes import tracking_bp, tracking_redirect_bp

__all__ = [
    "whatsapp_bp",
    "automation_bp",
    "ai_bp",
    "faq_bp",
    "knowledge_bp",
    "template_bp",
    "trigger_bp",
    "drip_bp",
    "interactive_automation_bp",
    "bulk_bp",
    "production_trigger_bp",
    "coexistence_bp",
    "catalog_bp",
    "tracking_bp",
    "tracking_redirect_bp",
    "verification_bp",
    "WhatsAppAccount",
    "WhatsAppConversation",
    "WhatsAppMessage",
    "WhatsAppWebhookLog",
    "WhatsAppTemplate",
    "WhatsAppAutomationRule",
    "WhatsAppAutomationLog",
    "WhatsAppBusinessHours",
    "TemplateBuilder",
    "build_simple_template",
    "build_template_with_body_params",
    "build_template_with_image_header",
    "perform_health_check",
    "perform_workspace_health_check",
    "subscribe_waba_to_webhooks",
    "setup_account_after_connection",
]

__version__ = "1.1.0"
