import os
import uuid
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from threading import Lock

from flask import Blueprint, jsonify, request, redirect
from sqlalchemy import func

from shared_models import db, WhatsAppLinkTracking
from .drip_models import WhatsAppDripEnrollment
from notifications import notification_manager


logger = logging.getLogger(__name__)


_RECENT_CLICK_CACHE = {}
_RECENT_CLICK_CACHE_LOCK = Lock()
_BOT_UA_TOKENS = (
    "bot",
    "crawler",
    "spider",
    "preview",
    "facebookexternalhit",
    "linkedinbot",
    "slackbot",
    "discordbot",
    "twitterbot",
    "telegrambot",
)


def _is_noise_tracking_request() -> bool:
    path = (request.path or "").lower()
    if "favicon.ico" in path:
        return True

    ua = (request.headers.get("User-Agent") or "").lower()
    if "favicon" in ua:
        return True
    if any(token in ua for token in _BOT_UA_TOKENS):
        return True

    purpose = (request.headers.get("Purpose") or request.headers.get("Sec-Purpose") or "").lower()
    if "prefetch" in purpose or "prerender" in purpose:
        return True

    sec_fetch_dest = (request.headers.get("Sec-Fetch-Dest") or "").lower()
    sec_fetch_mode = (request.headers.get("Sec-Fetch-Mode") or "").lower()
    if sec_fetch_dest in {"image", "empty"} and sec_fetch_mode in {"no-cors", "cors"}:
        return True

    return False


def _request_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "") or "unknown"


def _client_fingerprint() -> str:
    ua = (request.headers.get("User-Agent") or "").strip().lower()[:220]
    accept_lang = (request.headers.get("Accept-Language") or "").strip().lower()[:80]
    return f"{_request_ip()}|{ua}|{accept_lang}"


def _dedupe_window_seconds() -> int:
    raw = (os.getenv("WHATSAPP_TRACKING_DEDUP_WINDOW_SECONDS") or "").strip()
    try:
        value = int(raw) if raw else 2
    except ValueError:
        value = 2
    return max(0, min(value, 30))


def _is_recent_duplicate_click(tracking_id: str, now: datetime) -> bool:
    window_seconds = _dedupe_window_seconds()
    if window_seconds <= 0:
        return False

    key = (tracking_id, _client_fingerprint())
    with _RECENT_CLICK_CACHE_LOCK:
        stale_cutoff = now.timestamp() - (window_seconds * 5)
        stale_keys = [cache_key for cache_key, ts in _RECENT_CLICK_CACHE.items() if ts < stale_cutoff]
        for stale_key in stale_keys:
            _RECENT_CLICK_CACHE.pop(stale_key, None)

        last_seen = _RECENT_CLICK_CACHE.get(key)
        _RECENT_CLICK_CACHE[key] = now.timestamp()
        if last_seen is None:
            return False
        return (now.timestamp() - last_seen) <= window_seconds


def _as_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


tracking_bp = Blueprint("whatsapp_tracking", __name__, url_prefix="/api/v1/tracking")
tracking_redirect_bp = Blueprint("whatsapp_tracking_redirect", __name__)


def _workspace_id_from_request() -> str:
    workspace_id = request.headers.get("X-Workspace-ID") or request.args.get("workspace_id")
    if workspace_id:
        return str(workspace_id).strip()

    payload = request.get_json(silent=True) or {}
    fallback = payload.get("workspace_id")
    return str(fallback).strip() if fallback else ""


def _normalize_phone(phone_value: str | None) -> str:
    if not phone_value:
        return ""
    return "".join(ch for ch in str(phone_value) if ch.isdigit())


def _normalize_source(source_value: str | None) -> str:
    normalized = (source_value or "").strip().lower()
    # Accept all custom sources, don't force normalization
    # Valid sources: inbox, bulk, website, qr, ads, organic, direct, etc.
    if normalized:
        return normalized
    return "bulk"  # Only fallback to bulk if completely empty


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_valid_target_url(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _app_base_url() -> str:
    candidates = [
        (request.host_url or "").strip(),
        (os.getenv("APP_BASE_URL") or "").strip(),
        (os.getenv("PUBLIC_APP_BASE_URL") or "").strip(),
    ]
    for candidate in candidates:
        if candidate.lower().startswith(("http://", "https://")):
            return candidate.rstrip("/")
    return "https://sociovia.in"


@tracking_bp.route("/click-to-chat", methods=["POST"])
def track_click_to_chat():
    workspace_id = _workspace_id_from_request()
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400

    data = request.get_json(silent=True) or {}

    target_url = str(data.get("target_url") or "").strip()
    if not _is_valid_target_url(target_url):
        return jsonify({"success": False, "error": "Valid target_url is required"}), 400

    now = datetime.now(timezone.utc)
    phone_number = _normalize_phone(data.get("phone_number"))
    if not phone_number:
        return jsonify({"success": False, "error": "phone_number is required"}), 400

    account_id = _safe_int(data.get("account_id"), 0) or None
    source = _normalize_source(data.get("source"))
    source_type = str(data.get("source_type") or "click_to_chat").strip() or "click_to_chat"
    utm_source = str(data.get("source") or "").strip() or None
    utm_campaign = str(data.get("utm_campaign") or "").strip() or None

    # Aggregate repeated clicks for same target/context while preserving first/last click timestamps.
    existing = WhatsAppLinkTracking.query.filter_by(
        workspace_id=workspace_id,
        account_id=account_id,
        source=source,
        source_type=source_type,
        phone_number=phone_number,
        target_url=target_url,
        utm_source=utm_source,
        utm_campaign=utm_campaign,
    ).first()

    if existing:
        existing.click_count = (existing.click_count or 0) + 1
        if not existing.first_clicked_at:
            existing.first_clicked_at = now
        existing.last_clicked_at = now
        db.session.commit()
        return jsonify({
            "success": True,
            "tracking_id": existing.tracking_id,
            "click_count": existing.click_count,
            "created": False,
        })

    tracking_record = WhatsAppLinkTracking(
        workspace_id=workspace_id,
        account_id=account_id,
        source=source,
        source_type=source_type,
        tracking_id=f"clk_{uuid.uuid4().hex[:16]}",
        phone_number=phone_number,
        name=str(data.get("name") or "").strip() or None,
        template_name=str(data.get("template_name") or "").strip() or None,
        campaign_name=str(data.get("campaign_name") or utm_campaign or "").strip() or None,
        target_url=target_url,
        wamid=str(data.get("wamid") or "").strip() or None,
        utm_source=utm_source,
        utm_campaign=utm_campaign,
        click_count=1,
        first_clicked_at=now,
        last_clicked_at=now,
    )
    db.session.add(tracking_record)
    db.session.commit()

    return jsonify({
        "success": True,
        "tracking_id": tracking_record.tracking_id,
        "click_count": tracking_record.click_count,
        "created": True,
    })


@tracking_bp.route("/all", methods=["GET"])
def list_tracking_records():
    workspace_id = _workspace_id_from_request()
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400

    source_filter = (request.args.get("source") or "all").strip().lower()
    if source_filter not in {"all", "inbox", "bulk"}:
        source_filter = "all"

    phone_filter = (request.args.get("phone") or "").strip()
    limit = min(max(_safe_int(request.args.get("limit"), 100), 1), 500)
    offset = max(_safe_int(request.args.get("offset"), 0), 0)

    query = WhatsAppLinkTracking.query.filter_by(workspace_id=workspace_id)
    if source_filter != "all":
        query = query.filter(WhatsAppLinkTracking.source == source_filter)
    if phone_filter:
        query = query.filter(WhatsAppLinkTracking.phone_number.ilike(f"%{phone_filter}%"))

    rows = query.order_by(WhatsAppLinkTracking.created_at.desc()).offset(offset).limit(limit).all()

    summary_counts = db.session.query(
        WhatsAppLinkTracking.source,
        func.count(WhatsAppLinkTracking.id),
    ).filter(
        WhatsAppLinkTracking.workspace_id == workspace_id,
        WhatsAppLinkTracking.source.in_(["inbox", "bulk"]),
    ).group_by(WhatsAppLinkTracking.source).all()

    summary_map = {source: count for source, count in summary_counts}
    inbox_records = int(summary_map.get("inbox", 0))
    bulk_records = int(summary_map.get("bulk", 0))

    return jsonify({
        "success": True,
        "summary": {
            "inbox_records": inbox_records,
            "bulk_records": bulk_records,
            "total": inbox_records + bulk_records,
        },
        "records": [row.to_tracking_row() for row in rows],
        "filter": {
            "source": source_filter,
            "phone": phone_filter or None,
            "limit": limit,
            "offset": offset,
        },
    })


@tracking_bp.route("/debug", methods=["GET"])
def debug_tracking_stats():
    workspace_id = _workspace_id_from_request()
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400

    inbox_total = WhatsAppLinkTracking.query.filter_by(workspace_id=workspace_id, source="inbox").count()
    bulk_total = WhatsAppLinkTracking.query.filter_by(workspace_id=workspace_id, source="bulk").count()

    inbox_clicked = WhatsAppLinkTracking.query.filter(
        WhatsAppLinkTracking.workspace_id == workspace_id,
        WhatsAppLinkTracking.source == "inbox",
        WhatsAppLinkTracking.click_count > 0,
    ).count()
    bulk_clicked = WhatsAppLinkTracking.query.filter(
        WhatsAppLinkTracking.workspace_id == workspace_id,
        WhatsAppLinkTracking.source == "bulk",
        WhatsAppLinkTracking.click_count > 0,
    ).count()

    def ctr(clicked: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return round((clicked / total) * 100.0, 2)

    recent_inbox = WhatsAppLinkTracking.query.filter_by(
        workspace_id=workspace_id,
        source="inbox",
    ).order_by(WhatsAppLinkTracking.created_at.desc()).limit(10).all()

    app_base_url = (os.getenv("APP_BASE_URL") or request.host_url or "").rstrip("/")

    return jsonify({
        "success": True,
        "workspace_id": workspace_id,
        "config": {
            "app_base_url": app_base_url,
            "tracking_enabled": True,
            "environment": (os.getenv("ENV") or "dev").lower(),
        },
        "stats": {
            "inbox": {
                "total": inbox_total,
                "clicked": inbox_clicked,
                "ctr": ctr(inbox_clicked, inbox_total),
            },
            "bulk": {
                "total": bulk_total,
                "clicked": bulk_clicked,
                "ctr": ctr(bulk_clicked, bulk_total),
            },
        },
        "recent_inbox": [
            {
                "tracking_id": row.tracking_id,
                "phone": row.phone_number,
                "template": row.template_name or row.campaign_name or "-",
                "clicks": row.click_count,
                "created": row.created_at.isoformat() if row.created_at else None,
            }
            for row in recent_inbox
        ],
    })


@tracking_bp.route("/<tracking_id>", methods=["GET"])
def redirect_tracked_link(tracking_id: str):
    """
    🔥 CORE TRACKING ENDPOINT (Production Architecture)
    
    This endpoint intercepts ALL link clicks (QR, manual share, website button, etc.)
    - Logs the click in database
    - Redirects to WhatsApp link
    
    Usage: https://yourdomain.com/api/v1/tracking/{tracking_id}
    QR Code → hits this endpoint → tracked → redirects to wa.me
    """
    if not tracking_id or len(tracking_id.strip()) == 0:
        return jsonify({"error": "tracking_id required"}), 400

    tracking_id = tracking_id.strip()
    logger.info("[tracking] redirect hit tracking_id=%s", tracking_id)
    
    try:
        # Find the tracking record
        record = WhatsAppLinkTracking.query.filter_by(tracking_id=tracking_id).first()
        
        if not record:
            return jsonify({"error": "Link not found or expired"}), 404

        if _is_noise_tracking_request():
            logger.info(
                "[tracking] ignored noise request tracking_id=%s ua=%s path=%s",
                tracking_id,
                (request.headers.get("User-Agent") or "")[:180],
                request.path,
            )
            return redirect(record.target_url, code=302)

        now = datetime.now(timezone.utc)
        if _is_recent_duplicate_click(tracking_id, now):
            logger.info(
                "[tracking] ignored duplicate request tracking_id=%s ip=%s",
                tracking_id,
                _request_ip(),
            )
            return redirect(record.target_url, code=302)
        
        # ✅ TRACK THE CLICK (This is the magic)
        record.click_count = (record.click_count or 0) + 1
        if not record.first_clicked_at:
            record.first_clicked_at = now
        record.last_clicked_at = now

        enrollment = WhatsAppDripEnrollment.query.filter_by(tracking_id=tracking_id).first()
        if enrollment:
            enrollment.clicked = True
            enrollment.click_count = int(enrollment.click_count or 0) + 1
            clicked_at_utc = _as_aware_utc(enrollment.clicked_at)
            if clicked_at_utc is None or now > clicked_at_utc:
                enrollment.clicked_at = now

        db.session.commit()
        logger.info(
            "[tracking] click recorded tracking_id=%s workspace_id=%s phone=%s click_count=%s",
            record.tracking_id,
            record.workspace_id,
            record.phone_number,
            record.click_count,
        )

        try:
            notification_manager.broadcast(
                "whatsapp_link_clicked",
                {
                    "workspace_id": str(record.workspace_id),
                    "account_id": record.account_id,
                    "campaign_name": record.campaign_name,
                    "tracking_id": record.tracking_id,
                    "phone_number": record.phone_number,
                    "click_count": record.click_count,
                    "last_clicked_at": record.last_clicked_at.isoformat() if record.last_clicked_at else None,
                },
            )
        except Exception:
            pass
        
        # ✅ REDIRECT TO WHATSAPP
        return redirect(record.target_url, code=302)
        
    except Exception as e:
        from flask import current_app
        current_app.logger.exception(f"Tracking redirect error for {tracking_id}: {e}")
        return jsonify({"error": "Tracking error"}), 500


@tracking_redirect_bp.route("/t/<tracking_id>", methods=["GET"])
def redirect_tracked_link_short(tracking_id: str):
    return redirect_tracked_link(tracking_id)


@tracking_redirect_bp.route("/favicon.ico", methods=["GET"])
def favicon_noop():
    return "", 204


@tracking_bp.route("/generate", methods=["POST"])
def generate_tracking_link():
    """
    🔥 GENERATE TRACKING LINK ENDPOINT
    
    Frontend calls this to create a tracking record and get a short URL.
    Returns short URL like: https://yourdomain.com/api/v1/tracking/{tracking_id}
    
    This URL should be used for:
    - QR codes
    - Website buttons
    - Email links
    - Ads links
    - Any external sharing
    
    Usage:
    POST /api/v1/tracking/generate
    {
      "phone_number": "919876543210",
      "target_url": "https://wa.me/919876543210?text=Hello",
      "source": "website",
      "utm_campaign": "landing"
    }
    """
    workspace_id = _workspace_id_from_request()
    if not workspace_id:
        return jsonify({"success": False, "error": "Workspace ID required"}), 400

    data = request.get_json(silent=True) or {}

    target_url = str(data.get("target_url") or "").strip()
    if not _is_valid_target_url(target_url):
        return jsonify({"success": False, "error": "Valid target_url is required"}), 400

    phone_number = _normalize_phone(data.get("phone_number"))
    if not phone_number:
        return jsonify({"success": False, "error": "phone_number is required"}), 400

    now = datetime.now(timezone.utc)
    account_id = _safe_int(data.get("account_id"), 0) or None
    source = _normalize_source(data.get("source"))
    source_type = str(data.get("source_type") or "click_to_chat").strip() or "click_to_chat"
    utm_source = str(data.get("source") or "").strip() or None
    utm_campaign = str(data.get("utm_campaign") or "").strip() or None

    # Check if tracking record already exists
    existing = WhatsAppLinkTracking.query.filter_by(
        workspace_id=workspace_id,
        account_id=account_id,
        source=source,
        source_type=source_type,
        phone_number=phone_number,
        target_url=target_url,
        utm_source=utm_source,
        utm_campaign=utm_campaign,
    ).first()

    if existing:
        # Return existing tracking ID
        app_base_url = _app_base_url()
        short_url = f"{app_base_url}/t/{existing.tracking_id}"
        return jsonify({
            "success": True,
            "tracking_id": existing.tracking_id,
            "short_url": short_url,
            "target_url": existing.target_url,
            "created": False,
        })

    # Create new tracking record
    tracking_record = WhatsAppLinkTracking(
        workspace_id=workspace_id,
        account_id=account_id,
        source=source,
        source_type=source_type,
        tracking_id=f"clk_{uuid.uuid4().hex[:16]}",
        phone_number=phone_number,
        name=str(data.get("name") or "").strip() or None,
        template_name=str(data.get("template_name") or "").strip() or None,
        campaign_name=str(data.get("campaign_name") or utm_campaign or "").strip() or None,
        target_url=target_url,
        wamid=str(data.get("wamid") or "").strip() or None,
        utm_source=utm_source,
        utm_campaign=utm_campaign,
        click_count=0,  # ← Start at 0 (not incremented yet)
        first_clicked_at=None,  # ← Will be set on first redirect
        last_clicked_at=None,
    )
    db.session.add(tracking_record)
    db.session.commit()

    # Generate short URL
    app_base_url = _app_base_url()
    short_url = f"{app_base_url}/t/{tracking_record.tracking_id}"

    return jsonify({
        "success": True,
        "tracking_id": tracking_record.tracking_id,
        "short_url": short_url,
        "target_url": tracking_record.target_url,
        "created": True,
    })
