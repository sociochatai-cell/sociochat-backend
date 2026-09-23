"""
Food Image Analysis Module
===========================

Separate, self-contained module for WhatsApp food photo → nutritional analysis.
Uses OpenAI vision models (GPT-4o / GPT-4o-mini) via the AI Gateway.

Entry point:
    from whatsapp.food_analysis import handle_food_image

This module plugs into fast_router.py via a single hook.
All food-analysis logic lives here — easy to find, edit, enable/disable.
"""

from .handler import handle_food_image
from .config import is_food_analysis_enabled

__all__ = ["handle_food_image", "is_food_analysis_enabled"]
