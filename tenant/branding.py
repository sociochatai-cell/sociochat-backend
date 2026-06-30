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
    # Landing-page hero fields. Default "" so the frontend supplies the visible
    # fallback copy; a tenant override (text/URL) takes precedence when set.
    "landing_video_url": "",
    "landing_image_url": "",
    "landing_headline": "",
    "landing_subheadline": "",
    "landing_cta_text": "",
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
