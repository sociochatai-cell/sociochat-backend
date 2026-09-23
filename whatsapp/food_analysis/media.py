"""
Download WhatsApp media (images) from Meta's CDN.

Two-step process:
  1. GET graph.facebook.com/{media_id} → returns {url: "https://..."}
  2. GET that url with Bearer token → returns image bytes
"""

import logging
import requests
from typing import Optional, Tuple

from .config import MEDIA_DOWNLOAD_TIMEOUT

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v24.0"


def download_image(media_id: str, access_token: str, api_version: str = DEFAULT_API_VERSION) -> Optional[Tuple[bytes, str]]:
    """Download image bytes from Meta given a media_id.

    Returns (image_bytes, mime_type) or None on failure.
    """
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        info_url = f"{WHATSAPP_API_BASE}/{api_version}/{media_id}"
        info_resp = requests.get(info_url, headers=headers, timeout=MEDIA_DOWNLOAD_TIMEOUT)
        if info_resp.status_code != 200:
            logger.error("[food_analysis] Media info fetch failed (%s): %s", info_resp.status_code, info_resp.text[:200])
            return None

        media_data = info_resp.json()
        download_url = media_data.get("url")
        mime_type = media_data.get("mime_type", "image/jpeg")

        if not download_url:
            logger.error("[food_analysis] No URL in media info response for %s", media_id)
            return None

        img_resp = requests.get(download_url, headers=headers, timeout=MEDIA_DOWNLOAD_TIMEOUT)
        if img_resp.status_code != 200:
            logger.error("[food_analysis] Image download failed (%s)", img_resp.status_code)
            return None

        return (img_resp.content, mime_type)

    except requests.Timeout:
        logger.error("[food_analysis] Timeout downloading media %s", media_id)
        return None
    except Exception as e:
        logger.exception("[food_analysis] Unexpected error downloading media %s: %s", media_id, e)
        return None
