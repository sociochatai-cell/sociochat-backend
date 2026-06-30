"""
Tenant Module - Branding Image Uploads
=======================================

Super-admin-only endpoint for uploading tenant branding assets (logo, favicon,
login background). Stores to DigitalOcean Spaces when configured, otherwise
falls back to the local Flask ``static/`` folder.

Registered by the integrator in ``app.py`` as ``upload_bp``.
"""

import os
import logging
import secrets
import mimetypes

from flask import Blueprint, jsonify, request
from werkzeug.utils import secure_filename

from tenant.context import require_super_admin
from core.spaces_storage import (
    is_spaces_configured,
    get_s3_client,
    build_object_key,
    build_public_url,
)

logger = logging.getLogger(__name__)

upload_bp = Blueprint("upload", __name__, url_prefix="/api/superadmin")

# 5 MB cap for branding images; 50 MB for hero videos (per-kind, see below).
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_VIDEO_UPLOAD_BYTES = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico"}
ALLOWED_CONTENT_TYPES = {
    "image/png",
    "image/jpeg",
    "image/jpg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "image/x-icon",
    "image/vnd.microsoft.icon",
    "image/ico",
}

# Video assets for the landing-page hero (kind == "hero_video").
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "webm", "mov", "ogg", "ogv"}
ALLOWED_VIDEO_CONTENT_TYPES = {
    "video/mp4",
    "video/webm",
    "video/quicktime",
    "video/ogg",
}

# Kinds that accept video instead of images.
VIDEO_KINDS = {"hero_video"}
ALLOWED_KINDS = {"logo", "favicon", "background", "hero_image", "hero_video"}

# Extension -> canonical content type, used to repair missing/wrong mimetypes.
_EXT_CONTENT_TYPE = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "ico": "image/x-icon",
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mov": "video/quicktime",
    "ogg": "video/ogg",
    "ogv": "video/ogg",
}


def _extension(filename: str) -> str:
    if not filename or "." not in filename:
        return ""
    return filename.rsplit(".", 1)[1].lower()


@upload_bp.route("/uploads", methods=["POST"])
@require_super_admin
def upload_branding_asset(admin):
    """Accept a multipart image/video and return its public URL.

    Form fields:
      * ``file`` (required) - the image (or video) binary.
      * ``kind`` (optional) - one of logo|favicon|background|hero_image|hero_video.
        ``hero_video`` accepts video files (mp4/webm/mov/ogg, up to 50 MB);
        all other kinds accept images (up to 5 MB).
    """
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"success": False, "error": "no_file"}), 400

    kind = (request.form.get("kind") or "").strip().lower()
    if kind and kind not in ALLOWED_KINDS:
        return jsonify({"success": False, "error": "invalid_kind"}), 400

    is_video = kind in VIDEO_KINDS

    ext = _extension(file.filename)
    content_type = (file.mimetype or "").lower()

    # Validate by extension OR content-type. Video kinds use the video
    # allow-lists and a larger size cap; everything else stays image-only
    # (svg/ico can arrive with quirky mimetypes, so an allowed extension is
    # sufficient).
    if is_video:
        allowed_exts = ALLOWED_VIDEO_EXTENSIONS
        allowed_cts = ALLOWED_VIDEO_CONTENT_TYPES
        max_bytes = MAX_VIDEO_UPLOAD_BYTES
    else:
        allowed_exts = ALLOWED_EXTENSIONS
        allowed_cts = ALLOWED_CONTENT_TYPES
        max_bytes = MAX_UPLOAD_BYTES

    ext_ok = ext in allowed_exts
    ct_ok = content_type in allowed_cts
    if not ext_ok and not ct_ok:
        return jsonify({"success": False, "error": "unsupported_file_type"}), 400

    # Read bytes and enforce the (per-kind) size cap.
    data = file.read()
    if not data:
        return jsonify({"success": False, "error": "empty_file"}), 400
    if len(data) > max_bytes:
        return jsonify({"success": False, "error": "file_too_large"}), 413

    # Normalize a stored filename and resolve a sensible content type.
    safe_filename = secure_filename(file.filename) or "upload"
    if not ext:
        ext = _extension(safe_filename)
    resolved_ct = (
        content_type
        if content_type in allowed_cts
        else _EXT_CONTENT_TYPE.get(ext)
        or mimetypes.guess_type(safe_filename)[0]
        or "application/octet-stream"
    )

    unique_name = f"{secrets.token_hex(8)}_{safe_filename}"

    try:
        if is_spaces_configured():
            client, bucket = get_s3_client()
            key = build_object_key("uploads", "branding", unique_name)
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=data,
                ContentType=resolved_ct,
                ACL="public-read",
                CacheControl="public, max-age=31536000",
            )
            url = build_public_url(key)
        else:
            # Local fallback: write under the Flask app's static folder.
            # __file__ lives in tenant/, so the backend (app) root is two dirs up.
            base_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "static",
                "uploads",
                "branding",
            )
            os.makedirs(base_dir, exist_ok=True)
            dest_path = os.path.join(base_dir, unique_name)
            with open(dest_path, "wb") as fh:
                fh.write(data)
            url = (
                request.host_url.rstrip("/")
                + f"/static/uploads/branding/{unique_name}"
            )
    except Exception as exc:  # noqa: BLE001 - surface storage failures cleanly.
        logger.exception("branding_upload_failed kind=%s", kind or "(none)")
        return jsonify({"success": False, "error": f"upload_failed: {exc}"}), 500

    return jsonify({"success": True, "url": url, "kind": kind or None})
