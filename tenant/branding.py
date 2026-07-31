"""
Tenant branding defaults and helpers.
========================================

Centralizes the default (SocioChat / T0000) branding so that every tenant —
including the internal SocioChat tenant — uses the SAME branding pipeline.
There is NO special-case code for T0000: it simply has the default values.

Branding is "saved" only. Draft branding lives client-side in the wizard and is
used purely for live preview; the database is written only on Create / Save.
"""

# Reserved tenant code for the internal SocioChat tenant.
INTERNAL_TENANT_CODE = "T0000"
INTERNAL_TENANT_NAME = "SocioChat Internal"

# The canonical default brand (SocioChat). Any field a tenant leaves blank
# falls back to the matching value here, so the UI always has a full theme.
DEFAULT_BRANDING = {
    "company_name": "SocioChat",
    "short_name": "SocioChat",
    "name_suffix": ".ai",
    "tagline": "All-in-one WhatsApp Business Platform",
    "logo_url": "/sociochat_logo.png",
    "logo_dark_url": "/sociochat_logo.png",
    "favicon_url": "/sociochat_logo.png",
    "primary_color": "#25D366",
    "secondary_color": "#128C7E",
    "accent_color": "#0a6847",
    "font_family": "Inter",
    "button_style": "rounded",   # rounded | pill | square
    "theme": "light",            # light | dark | system
    # UI chrome colors + typography + shape. Default "" so the frontend supplies
    # the visible fallback; a tenant override takes precedence when set.
    "background_color": "",
    "surface_color": "",
    "text_color": "",
    "border_color": "",
    "heading_font_family": "",   # "" = same as body font
    "corner_radius": "",         # "" | none | small | medium | large | xl
    "support_email": "support@sociochat.ai",
    "login_background": "linear-gradient(135deg, #0a6847 0%, #128C7E 50%, #25D366 100%)",
    # Landing-page hero. `landing_video_url` is DEPRECATED (the landing page is now
    # fully image-based) and kept only so old saved values don't error; it is not
    # rendered. Defaults below ship the shared SocioChat/Sociovia content — a tenant
    # (super-admin only) override takes precedence per field.
    "landing_video_url": "",
    "landing_image_url": "/landing/hero.png",
    "landing_headline": "Automate your WhatsApp. Multiply your sales.",
    "landing_subheadline": "Stop drowning in manual replies. Automate conversations, send personalized broadcasts, and turn every chat into revenue — all on the official WhatsApp Business API.",
    "landing_cta_text": "Ready to grow on WhatsApp?",
    # Landing feature sections (7). Each has a title / description / image. Blank =
    # fall back to these shared defaults (merge_branding handles per-field fallback).
    "landing_feature1_title": "WhatsApp Broadcast Messaging",
    "landing_feature1_desc": "Send one personalized message to thousands at once — no group chats, delivered privately to every recipient at 90%+ open rates.",
    "landing_feature1_image": "/landing/feature-1-broadcast.png",
    "landing_feature2_title": "WhatsApp Lead Alerts",
    "landing_feature2_desc": "Get an instant WhatsApp alert the moment a new lead arrives — name, phone, source and interest included — so your team replies within minutes.",
    "landing_feature2_image": "/landing/feature-2-lead-alerts.png",
    "landing_feature3_title": "CRM with Kanban View",
    "landing_feature3_desc": "See your whole sales pipeline on a drag-and-drop board. Move deals from New Lead to Closed Won and spot bottlenecks in seconds.",
    "landing_feature3_image": "/landing/feature-3-crm-kanban.png",
    "landing_feature4_title": "WhatsApp Drip Messaging",
    "landing_feature4_desc": "Automated message sequences that nurture leads, recover carts and close sales 24/7 — running on autopilot at a 98% open rate.",
    "landing_feature4_image": "/landing/feature-4-drip.png",
    "landing_feature5_title": "WhatsApp API Triggers",
    "landing_feature5_desc": "Fire the right message on every event — order placed, payment failed, appointment due — automatically, in under three seconds.",
    "landing_feature5_image": "/landing/feature-5-api-triggers.png",
    "landing_feature6_title": "Interactive Flows",
    "landing_feature6_desc": "Guided, multi-step conversations with buttons and menus right inside the chat — instant replies that qualify leads and resolve queries 24/7.",
    "landing_feature6_image": "/landing/feature-6-interactive-flows.png",
    "landing_feature7_title": "WhatsApp AI Chatbot",
    "landing_feature7_desc": "An AI chatbot trained on your products and brand voice — handles hundreds of conversations at once and escalates to a human only when needed.",
    "landing_feature7_image": "/landing/feature-7-ai-chatbot.png",
}

# Keys that are part of the public branding payload the frontend themes from.
BRANDING_KEYS = list(DEFAULT_BRANDING.keys())


def merge_branding(overrides: dict | None) -> dict:
    """Return a full branding dict: tenant overrides on top of defaults.

    Empty / None values fall back to the default so the theme is never partial.
    """
    out = dict(DEFAULT_BRANDING)
    if overrides:
        for key in BRANDING_KEYS:
            val = overrides.get(key)
            if val is not None and val != "":
                out[key] = val
    return out
