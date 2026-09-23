"""
Per-workspace food analysis configuration.

Enable/disable per workspace by adding workspace_id to FOOD_ANALYSIS_WORKSPACE_IDS env var
(comma-separated), or set FOOD_ANALYSIS_ENABLED=true to enable for ALL workspaces.
"""

import os
import logging

logger = logging.getLogger(__name__)

# Vision model to use via OpenAI gateway
FOOD_ANALYSIS_MODEL = os.environ.get("FOOD_ANALYSIS_MODEL", "gpt-4o-mini")

# Max tokens for the analysis response
FOOD_ANALYSIS_MAX_TOKENS = int(os.environ.get("FOOD_ANALYSIS_MAX_TOKENS", "1024"))

# Timeout for media download from Meta (seconds)
MEDIA_DOWNLOAD_TIMEOUT = int(os.environ.get("FOOD_ANALYSIS_MEDIA_TIMEOUT", "15"))


def _enabled_workspace_ids() -> set:
    raw = os.environ.get("FOOD_ANALYSIS_WORKSPACE_IDS", "")
    if not raw.strip():
        return set()
    return {ws.strip() for ws in raw.split(",") if ws.strip()}


def is_food_analysis_enabled(workspace_id) -> bool:
    if os.environ.get("FOOD_ANALYSIS_ENABLED", "").strip().lower() in ("1", "true", "yes"):
        return True
    ws_ids = _enabled_workspace_ids()
    return str(workspace_id) in ws_ids
