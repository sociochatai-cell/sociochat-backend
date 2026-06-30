# ctwa/attribution.py
# CTWA Attribution Parser
# =======================

"""
Attribution parser for Click-to-WhatsApp ads.

Extracts ad/campaign information from WhatsApp webhook referral data
and enriches it with Meta Marketing API data.
"""

import os
import json
import logging
from typing import Optional, Dict, Any
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class CTWAAttribution:
    """
    Attribution data extracted from a CTWA ad click.
    """
    # From webhook referral
    ad_id: str
    ctwa_clid: str
    source_type: str = "ad"
    headline: Optional[str] = None
    body: Optional[str] = None
    media_type: Optional[str] = None
    image_url: Optional[str] = None
    video_url: Optional[str] = None
    source_url: Optional[str] = None
    
    # Enriched from Meta API (populated later)
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    adset_id: Optional[str] = None
    adset_name: Optional[str] = None
    ad_name: Optional[str] = None
    creative_id: Optional[str] = None
    
    # Metadata
    attributed_at: Optional[str] = None
    enriched: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage."""
        return asdict(self)


def parse_referral(message: Dict[str, Any]) -> Optional[CTWAAttribution]:
    """
    Parse referral data from incoming WhatsApp message.
    
    Args:
        message: The message object from WhatsApp webhook payload
        
    Returns:
        CTWAAttribution if referral exists and is from an ad, None otherwise
    """
    referral = message.get("referral")
    
    if not referral:
        return None
    
    source_type = referral.get("source_type")
    source_id = referral.get("source_id")
    ctwa_clid = referral.get("ctwa_clid")
    
    # Only process ad referrals
    if source_type != "ad":
        logger.debug(f"Ignoring non-ad referral: source_type={source_type}")
        return None
    
    # Handle missing required fields
    if not source_id and not ctwa_clid:
        logger.warning("Referral missing both source_id and ctwa_clid")
        return None
    
    # Create attribution object
    attribution = CTWAAttribution(
        ad_id=source_id or "unknown",
        ctwa_clid=ctwa_clid or f"missing_{source_id}_{datetime.now(timezone.utc).timestamp()}",
        source_type=source_type,
        headline=referral.get("headline"),
        body=referral.get("body"),
        media_type=referral.get("media_type"),
        image_url=referral.get("image_url"),
        video_url=referral.get("video_url"),
        source_url=referral.get("source_url"),
        attributed_at=datetime.now(timezone.utc).isoformat(),
    )
    
    logger.info(f"Parsed CTWA attribution: ad_id={attribution.ad_id}, ctwa_clid={attribution.ctwa_clid}")
    
    return attribution


def enrich_attribution(
    attribution: CTWAAttribution,
    access_token: str,
    cache: Optional[Dict] = None
) -> CTWAAttribution:
    """
    Fetch campaign/adset details from Meta API using ad_id.
    """
    import requests
    
    if attribution.ad_id == "unknown":
        return attribution
    
    cache = cache or {}
    cache_key = f"ad_metadata:{attribution.ad_id}"
    
    # Check cache first
    if cache_key in cache:
        cached = cache[cache_key]
        attribution.ad_name = cached.get("name")
        attribution.campaign_id = cached.get("campaign_id")
        attribution.campaign_name = cached.get("campaign_name")
        attribution.adset_id = cached.get("adset_id")
        attribution.adset_name = cached.get("adset_name")
        attribution.creative_id = cached.get("creative_id")
        attribution.enriched = True
        return attribution
    
    api_version = os.getenv("FB_API_VERSION", "v22.0")
    
    try:
        response = requests.get(
            f"https://graph.facebook.com/{api_version}/{attribution.ad_id}",
            params={
                "fields": "name,campaign{id,name},adset{id,name},creative{id}",
                "access_token": access_token,
            },
            timeout=10,
        )
        
        if response.status_code != 200:
            logger.warning(f"Failed to fetch ad metadata: {response.status_code}")
            return attribution
        
        data = response.json()
        
        attribution.ad_name = data.get("name")
        attribution.campaign_id = data.get("campaign", {}).get("id")
        attribution.campaign_name = data.get("campaign", {}).get("name")
        attribution.adset_id = data.get("adset", {}).get("id")
        attribution.adset_name = data.get("adset", {}).get("name")
        attribution.creative_id = data.get("creative", {}).get("id")
        attribution.enriched = True
        
        # Store in cache
        cache[cache_key] = {
            "name": attribution.ad_name,
            "campaign_id": attribution.campaign_id,
            "campaign_name": attribution.campaign_name,
            "adset_id": attribution.adset_id,
            "adset_name": attribution.adset_name,
            "creative_id": attribution.creative_id,
        }
        
        logger.info(f"Enriched attribution: campaign={attribution.campaign_name}, ad={attribution.ad_name}")
        
    except Exception as e:
        logger.exception(f"Error enriching attribution: {e}")
    
    return attribution


def apply_attribution_to_conversation(
    conversation,
    attribution: CTWAAttribution,
    db_session
) -> None:
    """
    Apply attribution data to a WhatsApp conversation.
    """
    conversation.entry_source = "ctwa"
    conversation.ctwa_clid = attribution.ctwa_clid
    conversation.ad_id = attribution.ad_id
    conversation.campaign_id = attribution.campaign_id
    conversation.adset_id = attribution.adset_id
    conversation.attribution_data = attribution.to_dict()
    conversation.attributed_at = datetime.now(timezone.utc)
    
    db_session.commit()
    
    logger.info(
        f"Applied attribution to conversation {conversation.id}: "
        f"ad_id={attribution.ad_id}, campaign={attribution.campaign_name}"
    )
