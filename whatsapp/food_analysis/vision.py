"""
Call OpenAI vision model (GPT-4o / GPT-4o-mini) via AI Gateway to analyze a food image.

Uses the OpenAI client from genai_bridge (same gateway, same API key).
"""

import base64
import logging
from typing import Optional

from .config import FOOD_ANALYSIS_MODEL, FOOD_ANALYSIS_MAX_TOKENS
from .prompts import FOOD_ANALYSIS_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def analyze_food_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    workspace_id: Optional[str] = None,
    custom_prompt: Optional[str] = None,
) -> Optional[str]:
    """Send image to OpenAI vision model and return the nutritional analysis text.

    Returns the analysis string, or None on failure.
    """
    try:
        from core.genai_bridge import _get_openai_client
    except ImportError:
        logger.error("[food_analysis] Cannot import genai_bridge — OpenAI client unavailable")
        return None

    client = _get_openai_client()
    if client is None:
        logger.error("[food_analysis] OpenAI client not initialized (check OPENAI_API_KEY / OPENAI_BASE_URL)")
        return None

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64_image}"

    system_prompt = custom_prompt or FOOD_ANALYSIS_SYSTEM_PROMPT

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": data_uri, "detail": "high"},
                },
                {
                    "type": "text",
                    "text": "Analyze this food image and provide a detailed nutritional assessment.",
                },
            ],
        },
    ]

    extra_headers = {}
    if workspace_id:
        extra_headers["x-workspace-id"] = str(workspace_id)
    extra_headers["x-feature"] = "food_analysis"

    try:
        response = client.chat.completions.create(
            model=FOOD_ANALYSIS_MODEL,
            messages=messages,
            max_tokens=FOOD_ANALYSIS_MAX_TOKENS,
            temperature=0.3,
            extra_headers=extra_headers,
        )
        if response.choices:
            text = response.choices[0].message.content
            logger.info("[food_analysis] Analysis generated (%d chars) for workspace %s", len(text or ""), workspace_id)
            return text
        return None
    except Exception as e:
        logger.exception("[food_analysis] OpenAI vision call failed: %s", e)
        return None
