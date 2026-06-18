"""
DigitalOcean Spaces (S3-compatible) helpers for SocioChat uploads.

All objects are stored under SPACE_PREFIX (default: sociochat/) inside the bucket.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple


def _normalize_spaces_endpoint(
    endpoint: str,
    region: Optional[str],
    bucket: Optional[str],
) -> str:
    """
    DigitalOcean Spaces expects a regional endpoint only, e.g.
    https://blr1.digitaloceanspaces.com — not the bucket hostname and not /sociochat paths.
    """
    endpoint = endpoint.rstrip("/")
    if not endpoint and region:
        return f"https://{region}.digitaloceanspaces.com"

    # Strip accidental path suffixes like /sociochat (prefix belongs in object keys, not endpoint).
    if "digitaloceanspaces.com/" in endpoint:
        endpoint = endpoint.split("digitaloceanspaces.com/", 1)[0] + "digitaloceanspaces.com"

    # Convert bucket virtual-host URLs back to regional endpoint for boto3.
    if bucket and region:
        bucket_host = f"https://{bucket}.{region}.digitaloceanspaces.com"
        if endpoint.startswith(bucket_host):
            endpoint = f"https://{region}.digitaloceanspaces.com"

    return endpoint


def get_space_prefix() -> str:
    """Folder prefix inside the bucket, e.g. sociochat."""
    return (os.getenv("SPACE_PREFIX") or "sociochat").strip().strip("/")


def get_spaces_config() -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Returns: bucket, region, access_key_id, secret_access_key, endpoint_url, cdn_base
    """
    bucket = os.getenv("SPACE_NAME") or os.getenv("DO_SPACES_BUCKET")
    region = os.getenv("SPACE_REGION") or os.getenv("DO_SPACES_REGION")
    access_key = os.getenv("ACCESS_KEY") or os.getenv("DO_ACCESS_KEY_ID")
    # Never use Flask SESSION_SECRET / SECRET_KEY for object storage.
    secret_key = (
        os.getenv("DO_SPACES_SECRET_KEY")
        or os.getenv("SPACE_SECRET_KEY")
        or os.getenv("DO_SECRET_ACCESS_KEY")
    )
    endpoint = _normalize_spaces_endpoint(
        (os.getenv("SPACE_ENDPOINT") or "").strip(),
        region,
        bucket,
    )
    cdn = (os.getenv("SPACE_CDN") or "").strip().rstrip("/") or None
    return bucket, region, access_key, secret_key, endpoint, cdn


def is_spaces_configured() -> bool:
    bucket, region, access_key, secret_key, endpoint, _ = get_spaces_config()
    return bool(bucket and region and access_key and secret_key and endpoint)


def build_object_key(*parts: str) -> str:
    """Build a key like sociochat/uploads/chat/image/123_abc.jpg"""
    segments = [get_space_prefix()]
    segments.extend(p.strip("/") for p in parts if p and str(p).strip("/"))
    return "/".join(segments)


def build_public_url(key: str) -> str:
    """Public URL — prefers SPACE_CDN when set."""
    _, _, _, _, _, cdn = get_spaces_config()
    normalized_key = key.lstrip("/")
    if cdn:
        return f"{cdn}/{normalized_key}"
    bucket, region, _, _, _, _ = get_spaces_config()
    return f"https://{bucket}.{region}.digitaloceanspaces.com/{normalized_key}"


def get_s3_client():
    import boto3

    bucket, region, access_key, secret_key, endpoint, _ = get_spaces_config()
    if not all([bucket, region, access_key, secret_key, endpoint]):
        raise RuntimeError(
            "DigitalOcean Spaces not configured. Set SPACE_NAME, SPACE_REGION, "
            "ACCESS_KEY, DO_SPACES_SECRET_KEY, and SPACE_ENDPOINT."
        )
    from botocore.client import Config

    return boto3.client(
        "s3",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        endpoint_url=endpoint,
        region_name=region,
        config=Config(signature_version="s3v4"),
    ), bucket
