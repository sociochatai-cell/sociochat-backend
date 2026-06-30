"""
WhatsApp Utils - Phase 1
========================

Utility functions for WhatsApp module.
"""

import os
import hmac
import hashlib
import logging
import requests
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


# ============================================================
# Phone Number Utilities
# ============================================================

def normalize_phone(phone: str) -> str:
    """
    Normalize a phone number to canonical E.164 format (digits only, with country code).

    Strategy (industry standard):
    1. Strip all non-digit characters (+, spaces, dashes, parens).
    2. If the result is a 10-digit Indian mobile number (starts with 6-9),
       prepend the country code '91'.
    3. If it has a leading '0' (local dialling), remove it and treat as above.
    4. Otherwise return digits only (already has country code).

    This ensures "9999320932", "919999320932", "+91-9999320932", and
    "09999320932" are ALL stored as "919999320932", preventing duplicate
    conversations for the same person.

    Examples:
        normalize_phone("+91 99993 20932")  -> "919999320932"
        normalize_phone("9999320932")       -> "919999320932"
        normalize_phone("919999320932")     -> "919999320932"
        normalize_phone("+1 650 555 1234")  -> "16505551234"
    """
    if not phone:
        return ""
    # Step 1: strip everything that isn't a digit
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return ""
    # Step 2: remove leading zeros (common in local dialling)
    digits = digits.lstrip("0") or "0"
    # Step 3: Indian mobile numbers — 10 digits starting with 6/7/8/9
    if len(digits) == 10 and digits[0] in "6789":
        digits = "91" + digits
    return digits


def _digits_only(value) -> str:
    """All digits in a value, as a string."""
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _region_for_country_code(cc_digits: str):
    """Map a numeric dialing/country code (e.g. '52', '54', '1') to an ISO region (e.g. 'MX',
    'AR', 'US') using phonenumbers. Returns None if unavailable/ambiguous-empty."""
    if not cc_digits:
        return None
    try:
        import phonenumbers
        region = phonenumbers.region_code_for_country_code(int(cc_digits))
        if region and region != "ZZ":
            return region
    except Exception:
        pass
    return None


def normalize_phone_robust(raw, country_code=None, default_region: str = "IN") -> Optional[str]:
    """
    Production-grade phone normalizer using Google's phonenumbers library. Returns digits-only
    E.164 (no '+'), or None if no valid number is found.

    `country_code` (optional): the row's numeric dialing code (e.g. "52", "+54", 91). When the
    phone value is a bare NATIONAL number, this picks the correct region so the number is NOT
    blindly assumed Indian (+91), and so country-specific quirks (Mexico's +52, Argentina's
    mobile +549, etc.) are handled by phonenumbers rather than naive concatenation.

    Resolution order, per candidate number:
      1. Value already in international form ("+…") → trust it.
      2. country_code given → parse the national number under that region (correct MX/AR),
         and also try international "+<cc><national>".
      3. Looks like it already embeds a country code (long, no '+') → try international "+<digits>".
      4. Fall back to the default region (India) — mainly for bare 10-digit local numbers.
    """
    import re

    if raw is None:
        return None
    raw_str = str(raw).strip()
    if not raw_str:
        return None

    # Excel scientific notation (e.g. 9.19E+11)
    if "E+" in raw_str.upper() or "E-" in raw_str.upper():
        try:
            raw_str = format(float(raw_str), ".0f")
        except (ValueError, OverflowError):
            pass
    # Float with decimal (e.g. "9390000000.0")
    if isinstance(raw, float):
        raw_str = format(raw, ".0f")
    elif "." in raw_str:
        try:
            raw_str = format(float(raw_str), ".0f")
        except (ValueError, OverflowError):
            pass

    # Split embedded '+' signs into separate candidates, keeping the '+' prefix.
    if raw_str.count("+") > 1:
        plus_parts = ["+" + seg.strip() for seg in raw_str.split("+") if seg.strip()]
        if plus_parts:
            raw_str = ",".join(plus_parts)

    parts = re.split(r"[,;/\|&\n\t]+|\bor\b|\band\b", raw_str)

    cc_digits = _digits_only(country_code).lstrip("0") if country_code is not None else ""

    try:
        import phonenumbers
    except ImportError:
        phonenumbers = None
        logger.warning("phonenumbers library not installed, falling back to basic normalization")

    if phonenumbers is None:
        # Best-effort fallback without the library.
        for part in parts:
            d = _digits_only(part).lstrip("0")
            if not d:
                continue
            if cc_digits and len(d) <= 10 and not d.startswith(cc_digits):
                d = cc_digits + d            # use the row's country code, not a hardcoded +91
            elif not cc_digits and len(d) == 10 and d[0] in "6789":
                d = "91" + d                 # legacy India default only when no cc is known
            if 10 <= len(d) <= 15:
                return d
        return None

    preferred_region = _region_for_country_code(cc_digits)

    def _accept(num) -> Optional[str]:
        try:
            if phonenumbers.is_valid_number(num) or phonenumbers.is_possible_number(num):
                return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164).replace("+", "")
        except Exception:
            return None
        return None

    def _try(value: str, region):
        try:
            return _accept(phonenumbers.parse(value, region))
        except Exception:
            return None

    for part in parts:
        part = part.strip()
        if not part:
            continue
        d = _digits_only(part)
        if not d:
            continue

        # Build attempts in priority order; first valid wins.
        attempts = []
        if part.lstrip().startswith("+"):
            attempts.append((part, None))                       # 1. explicit international
        if preferred_region:
            if cc_digits and d.startswith(cc_digits) and len(d) > 10:
                # Already carries the cc: try the national remainder under its true region first
                # (lets phonenumbers apply mobile rules, e.g. Argentina's +549), then trust the
                # embedded code as-is.
                attempts.append((d[len(cc_digits):], preferred_region))
                attempts.append(("+" + d, None))                # 2a. national already carries the cc
            attempts.append((d, preferred_region))              # 2b. national under its true region (MX/AR)
            attempts.append(("+" + cc_digits + d.lstrip("0"), None))  # 2c. prefix the cc
        if len(d) > 10:
            attempts.append(("+" + d, None))                    # 3. cc likely embedded
        attempts.append((d, default_region))                    # 4. legacy default (India) for bare local
        attempts.append(("+" + d, None))                        # 5. last-ditch international

        seen = set()
        for value, region in attempts:
            key = (value, region)
            if key in seen:
                continue
            seen.add(key)
            res = _try(value, region)
            if res and 8 <= len(res) <= 15:
                return res

    logger.warning(f"Invalid phone — no valid number found in: {repr(raw)} (cc={country_code!r})")
    return None


def extract_all_phones(raw) -> list:
    """
    Extract ALL valid phone numbers from a multi-number field.

    Useful for CRM cleaning, deduplication, and future multi-recipient features.

    Args:
        raw: Raw phone value (str, float, int, or None)

    Returns:
        List of valid E.164 phone numbers (digits only, no +)
    """
    import re

    DEFAULT_COUNTRY = "IN"

    if raw is None:
        return []

    raw_str = str(raw).strip()
    if not raw_str:
        return []

    # Handle scientific notation
    if "E+" in raw_str.upper() or "E-" in raw_str.upper():
        try:
            raw_str = format(float(raw_str), ".0f")
        except (ValueError, OverflowError):
            pass

    if isinstance(raw, float):
        raw_str = format(raw, ".0f")

    # Split embedded + signs
    if raw_str.count("+") > 1:
        plus_parts = []
        for segment in raw_str.split("+"):
            segment = segment.strip()
            if segment:
                plus_parts.append("+" + segment)
        if plus_parts:
            raw_str = ",".join(plus_parts)

    parts = re.split(r"[,;/\|&\n\t]+|\bor\b|\band\b", raw_str)
    phones = []

    try:
        import phonenumbers
        _has_phonenumbers = True
    except ImportError:
        _has_phonenumbers = False

    for part in parts:
        part = part.strip()
        if not part:
            continue

        if _has_phonenumbers:
            try:
                num = phonenumbers.parse(part, DEFAULT_COUNTRY)
                if phonenumbers.is_possible_number(num):
                    e164 = phonenumbers.format_number(
                        num, phonenumbers.PhoneNumberFormat.E164
                    ).replace("+", "")
                    if e164 not in phones:
                        phones.append(e164)
            except Exception:
                continue
        else:
            digits = "".join(ch for ch in part if ch.isdigit())
            if not digits:
                continue
            digits = digits.lstrip("0") or "0"
            if len(digits) == 10 and digits[0] in "6789":
                digits = "91" + digits
            if 10 <= len(digits) <= 15 and digits not in phones:
                phones.append(digits)

    return phones


def format_phone_display(phone: str) -> str:
    """
    Format phone number for display.
    
    Args:
        phone: Normalized phone number
        
    Returns:
        Formatted phone number with country code prefix
    """
    if not phone:
        return ""
    
    # Add + prefix if not present
    if not phone.startswith("+"):
        phone = f"+{phone}"
    
    return phone


def is_valid_phone(phone: str) -> bool:
    """
    Check if phone number is valid (basic validation).
    
    Args:
        phone: Phone number to validate
        
    Returns:
        True if valid
    """
    normalized = normalize_phone(phone)
    return len(normalized) >= 10 and len(normalized) <= 15 and normalized.isdigit()


# ============================================================
# Signature Verification
# ============================================================

def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """
    Verify webhook signature from Meta.
    
    Args:
        payload: Raw request body
        signature: X-Hub-Signature-256 header value
        secret: App secret from environment
        
    Returns:
        True if signature is valid
    """
    if not signature or not secret:
        return False
    
    # Signature format: sha256=xxxxx
    if not signature.startswith("sha256="):
        return False
    
    expected_signature = signature[7:]  # Remove 'sha256=' prefix
    
    # Calculate HMAC
    computed_signature = hmac.new(
        secret.encode("utf-8"),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    # Use constant-time comparison
    return hmac.compare_digest(expected_signature, computed_signature)


def generate_verify_token() -> str:
    """
    Generate a random verify token for webhook verification.
    
    Returns:
        Random token string
    """
    import secrets
    return secrets.token_urlsafe(32)


# ============================================================
# Environment Helpers
# ============================================================

def get_whatsapp_config() -> Dict[str, Any]:
    """
    Get WhatsApp configuration from environment variables.
    
    Returns:
        Config dict with all WhatsApp settings
    """
    return {
        "access_token": os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("WHATSAPP_TEMP_TOKEN", ""),
        "phone_number_id": os.getenv("WHATSAPP_PHONE_NUMBER_ID", ""),
        "waba_id": os.getenv("WHATSAPP_WABA_ID", ""),
        "verify_token": os.getenv("WHATSAPP_VERIFY_TOKEN", ""),
        "app_secret": os.getenv("WHATSAPP_APP_SECRET", ""),
        "api_version": os.getenv("WHATSAPP_API_VERSION", "v22.0"),
    }


def is_whatsapp_configured() -> bool:
    """
    Check if WhatsApp is properly configured.
    
    Returns:
        True if required config is present
    """
    config = get_whatsapp_config()
    return bool(config["access_token"] and config["phone_number_id"])


# ============================================================
# Message Parsing
# ============================================================

def extract_message_text(message: Dict[str, Any]) -> Optional[str]:
    """
    Extract text content from a webhook message object.
    
    Args:
        message: Message object from webhook
        
    Returns:
        Text content or None
    """
    msg_type = message.get("type", "")
    
    if msg_type == "text":
        return message.get("text", {}).get("body")
    
    elif msg_type == "interactive":
        interactive = message.get("interactive", {})
        int_type = interactive.get("type", "")
        
        if int_type == "button_reply":
            return interactive.get("button_reply", {}).get("title")
        elif int_type == "list_reply":
            return interactive.get("list_reply", {}).get("title")
    
    elif msg_type == "button":
        return message.get("button", {}).get("text")
    
    return None


def get_message_type(message: Dict[str, Any]) -> str:
    """
    Get the type of a message from webhook payload.
    
    Args:
        message: Message object from webhook
        
    Returns:
        Message type string
    """
    return message.get("type", "unknown")


def extract_media_info(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract media information from a message.
    
    Args:
        message: Message object from webhook
        
    Returns:
        Media info dict or None
    """
    msg_type = message.get("type", "")
    
    if msg_type in ("image", "video", "audio", "document", "sticker"):
        media_obj = message.get(msg_type, {})
        return {
            "type": msg_type,
            "id": media_obj.get("id"),
            "mime_type": media_obj.get("mime_type"),
            "sha256": media_obj.get("sha256"),
            "caption": media_obj.get("caption"),
            "filename": media_obj.get("filename"),
        }
    
    return None


# ============================================================
# Timestamp Utilities
# ============================================================

def parse_whatsapp_timestamp(timestamp: str) -> Optional[datetime]:
    """
    Parse a WhatsApp timestamp (Unix epoch seconds).
    
    Args:
        timestamp: Unix timestamp string
        
    Returns:
        datetime object or None
    """
    try:
        ts = int(timestamp)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError):
        return None


def format_timestamp(dt: datetime) -> str:
    """
    Format datetime for API response.
    
    Args:
        dt: datetime object
        
    Returns:
        ISO format string
    """
    if not dt:
        return ""
    return dt.isoformat()


def utcnow() -> datetime:
    """Get current UTC datetime."""
    return datetime.now(timezone.utc)


# ============================================================
# Response Helpers
# ============================================================

def success_response(data: Dict[str, Any] = None, message: str = None) -> Dict[str, Any]:
    """
    Create a success response dict.
    
    Args:
        data: Optional data to include
        message: Optional message
        
    Returns:
        Response dict
    """
    response = {"success": True}
    if data:
        response.update(data)
    if message:
        response["message"] = message
    return response


def error_response(error: str, code: str = None, field: str = None) -> Dict[str, Any]:
    """
    Create an error response dict.
    
    Args:
        error: Error message
        code: Optional error code
        field: Optional field name
        
    Returns:
        Response dict
    """
    response = {
        "success": False,
        "error": error,
    }
    if code:
        response["code"] = code
    if field:
        response["field"] = field
    return response


# ============================================================
# Logging Helpers
# ============================================================

def log_webhook_event(event_type: str, phone_number_id: str, details: Dict[str, Any] = None):
    """
    Log a webhook event for debugging.
    
    Args:
        event_type: Type of event (message, status, etc.)
        phone_number_id: Phone number ID
        details: Additional details to log
    """
    logger.info(
        f"WhatsApp webhook event: {event_type} | phone_id: {phone_number_id} | details: {details}"
    )


def log_api_call(endpoint: str, method: str, status: int, response_time_ms: float):
    """
    Log an API call for monitoring.
    
    Args:
        endpoint: API endpoint called
        method: HTTP method
        status: Response status code
        response_time_ms: Response time in milliseconds
    """
    logger.info(
        f"WhatsApp API call: {method} {endpoint} | status: {status} | time: {response_time_ms:.2f}ms"
    )


# ============================================================
# Deduplication
# ============================================================

_processed_wamids = set()
_max_cache_size = 10000


def is_duplicate_message(wamid: str) -> bool:
    """
    Check if message has already been processed (in-memory cache).
    
    Note: This is a simple in-memory cache. For production,
    use Redis or database-backed deduplication.
    
    Args:
        wamid: WhatsApp message ID
        
    Returns:
        True if duplicate
    """
    global _processed_wamids
    
    if wamid in _processed_wamids:
        return True
    
    # Add to cache
    _processed_wamids.add(wamid)
    
    # Clear cache if too large
    if len(_processed_wamids) > _max_cache_size:
        # Keep only the most recent half
        _processed_wamids = set(list(_processed_wamids)[_max_cache_size // 2:])
    
    return False


def clear_dedup_cache():
    """Clear the deduplication cache (for testing)."""
    global _processed_wamids
    _processed_wamids = set()


# ============================================================
# Webhook Subscription Helpers
# ============================================================

def _webhook_subscribe_tokens(primary_token: str) -> list:
    """Tokens to try for POST /{waba-id}/subscribed_apps (customer token first)."""
    tokens: list = []
    for token in (
        primary_token,
        os.getenv("WHATSAPP_ACCESS_TOKEN"),
        os.getenv("META_SYSTEM_USER_TOKEN"),
    ):
        token = (token or "").strip()
        if token and token not in tokens:
            tokens.append(token)
    app_id = (os.getenv("FB_APP_ID") or os.getenv("META_APP_ID") or "").strip()
    app_secret = (os.getenv("FB_APP_SECRET") or os.getenv("META_APP_SECRET") or "").strip()
    if app_id and app_secret:
        app_token = f"{app_id}|{app_secret}"
        if app_token not in tokens:
            tokens.append(app_token)
    return tokens


def subscribe_waba_to_app(waba_id: str, access_token: str) -> dict:
    """
    Subscribe WABA to Sociovia's Meta App for webhook events.
    This is critical for receiving messages, statuses, echoes, and template updates.
    
    Fields subscribed:
    - messages: Inbound messages from users
    - message_template_status_update: Template approval/rejection/re-approval
    - message_template_quality_update: Template quality score changes
    - template_category_update: Template category shifts by Meta
    - message_echoes: Messages sent from other devices (for coexistence)
    - smb_message_echoes: Messages sent from mobile in coexistence mode (SMB specific)
    
    Args:
        waba_id: WhatsApp Business Account ID
        access_token: Meta access token with business_management scope
        
    Returns:
        Dict with success status and message/error
    """
    api_version = os.getenv("WHATSAPP_API_VERSION", "v24.0")
    meta_graph = f"https://graph.facebook.com/{api_version}"
    
    from .provisioning_types import FULL_WEBHOOK_FIELDS

    subscribed_fields = list(FULL_WEBHOOK_FIELDS)
    
    import time
    
    max_retries = 3
    base_backoff = 2
    last_error = "Max retries exceeded"

    for token in _webhook_subscribe_tokens(access_token):
        for attempt in range(max_retries):
            try:
                logger.info(
                    "Subscribing WABA %s to app fields (token attempt %s, retry %s/%s)",
                    waba_id,
                    "customer" if token == (access_token or "").strip() else "fallback",
                    attempt + 1,
                    max_retries,
                )

                resp = requests.post(
                    f"{meta_graph}/{waba_id}/subscribed_apps",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"subscribed_fields": subscribed_fields},
                    timeout=20,
                )

                result = resp.json()

                if resp.status_code == 200 and result.get("success"):
                    logger.info("WABA %s subscribed to app successfully.", waba_id)
                    return {"success": True, "message": "WABA subscribed to app successfully"}

                error_msg = result.get("error", {}).get("message") or str(result)
                last_error = error_msg
                logger.warning("WABA %s subscription failed: %s", waba_id, error_msg)
                if attempt == max_retries - 1:
                    break

            except Exception as e:
                last_error = str(e)
                logger.exception("Unexpected error subscribing WABA %s: %s", waba_id, e)
                if attempt == max_retries - 1:
                    break

            time.sleep(base_backoff * (2 ** attempt))

    return {"success": False, "error": last_error}
