"""
Optional Meta Graph poll for phone number fields (advisory enrichment for reputation snapshots).
Fails open on any error — never blocks snapshot persistence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WhatsAppAccount

logger = logging.getLogger(__name__)


def fetch_phone_graph_profile(account: "WhatsAppAccount") -> Optional[Dict[str, Any]]:
    if os.getenv("WH_TRUST_GRAPH_POLL", "").lower() not in ("1", "true", "yes"):
        return None
    token = account.get_access_token()
    if not token or not account.phone_number_id:
        return None
    api_version = os.getenv("WHATSAPP_API_VERSION", os.getenv("FB_API_VERSION", "v22.0"))
    try:
        import requests

        url = f"https://graph.facebook.com/{api_version}/{account.phone_number_id}"
        params = {
            "fields": "id,display_phone_number,verified_name,quality_rating,name_status,status",
        }
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=12)
        data = r.json()
        if r.status_code != 200 or "error" in data:
            logger.debug("trust_graph_poll error status=%s body=%s", r.status_code, data)
            return None
        return data
    except Exception as e:
        logger.debug("trust_graph_poll exception: %s", e)
        return None
