"""
WhatsApp Token Encryption
=========================
Encrypt/decrypt access tokens before storing in database.
Uses Fernet (symmetric encryption) from cryptography library.

Decrypt tries multiple key material sources so tokens encrypted before
``WHATSAPP_ENCRYPTION_KEY`` was introduced (SECRET_KEY-derived Fernet) still
decrypt when the service now prefers an explicit vault key.
"""

import os
import base64
from typing import List, Optional

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_PLACEHOLDER_KEYS = frozenset(
    {"your_encryption_key_here", "changeme", "replace_me", ""}
)


def _derive_fernet_key_from_secret(secret: str) -> bytes:
    """Deterministic Fernet key from a shared secret (legacy path)."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"sociovia_wa_salt",
        iterations=100000,
    )
    return base64.urlsafe_b64encode(kdf.derive(secret.encode()))


def _decode_explicit_whatsapp_key(raw: str) -> Optional[bytes]:
    """
    Decode WHATSAPP_ENCRYPTION_KEY / WHATSAPP_TOKEN_KEY value to Fernet key bytes.
    Returns None when the env var should be ignored (placeholder / empty).
    """
    key_str = (raw or "").strip()
    if not key_str or key_str.lower() in _PLACEHOLDER_KEYS:
        return None
    # A Fernet key IS the 44-char urlsafe-base64 string (it decodes to 32 bytes).
    # Fernet() expects that base64 FORM, NOT the decoded raw 32 bytes. So when the
    # env value is already a valid Fernet key, return it AS-IS — decoding it here
    # produced raw bytes that Fernet rejected ("must be 32 url-safe base64-encoded
    # bytes"), which silently broke encrypt_token for every new connect.
    kb = key_str.encode()
    try:
        if len(base64.urlsafe_b64decode(kb)) == 32:
            return kb
    except Exception:
        pass
    # Arbitrary/non-Fernet text: coerce into a valid Fernet key deterministically.
    return base64.urlsafe_b64encode(key_str.encode()[:32].ljust(32, b"0"))


def get_encryption_key() -> bytes:
    """
    Primary Fernet key for new encrypts and first decrypt attempt.
    Prefer explicit WHATSAPP_ENCRYPTION_KEY; else derive from SECRET_KEY.
    """
    raw = (os.getenv("WHATSAPP_ENCRYPTION_KEY") or os.getenv("WHATSAPP_TOKEN_KEY") or "").strip()
    if raw.lower() in _PLACEHOLDER_KEYS:
        raw = ""
    explicit = _decode_explicit_whatsapp_key(raw) if raw else None
    if explicit is not None:
        return explicit
    secret = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
    return _derive_fernet_key_from_secret(secret)


def iter_fernet_decrypt_keys() -> List[bytes]:
    """
    Ordered, de-duplicated Fernet keys to try when decrypting DB ciphertext.

    Covers: explicit vault key, previous keys (rotation), SECRET_KEY/SESSION_SECRET
    KDF (historical), Flask app secret (when in request context), dev default.
    """
    keys: List[bytes] = []
    seen: set[bytes] = set()

    def add(k: Optional[bytes]) -> None:
        if not k:
            return
        if k in seen:
            return
        seen.add(k)
        keys.append(k)

    # 1) Primary (same as encrypt path)
    try:
        add(get_encryption_key())
    except Exception:
        pass

    # 2) Previous explicit keys (comma-separated base64)
    prev_raw = (os.getenv("WHATSAPP_ENCRYPTION_KEY_PREVIOUS") or "").strip()
    if prev_raw:
        for part in prev_raw.split(","):
            part = part.strip()
            if not part:
                continue
            k = _decode_explicit_whatsapp_key(part)
            if k is not None:
                add(k)

    # 3) Alternate env secrets (historical encrypt path before explicit WA key)
    for env_name in ("SECRET_KEY", "SESSION_SECRET"):
        val = (os.getenv(env_name) or "").strip()
        if val and val.lower() not in _PLACEHOLDER_KEYS:
            try:
                add(_derive_fernet_key_from_secret(val))
            except Exception:
                pass

    # 3b) Legacy shared secrets from a SIBLING service (e.g. whatsapp-api) whose
    # SECRET_KEY differs from this app's. Tokens encrypted there use a Fernet key
    # KDF-derived from that secret; without this they are undecryptable here.
    # Comma-separated raw secrets in WHATSAPP_LEGACY_SECRETS.
    legacy = (os.getenv("WHATSAPP_LEGACY_SECRETS") or "").strip()
    if legacy:
        for sec in legacy.split(","):
            sec = sec.strip()
            if sec and sec.lower() not in _PLACEHOLDER_KEYS:
                try:
                    add(_derive_fernet_key_from_secret(sec))
                except Exception:
                    pass

    # 4) Flask app secret (some deployments only set secret_key)
    try:
        from flask import current_app, has_request_context

        if has_request_context():
            sk = getattr(current_app, "secret_key", None)
            if sk is not None:
                if isinstance(sk, bytes):
                    s = sk.decode("utf-8", errors="replace")
                else:
                    s = str(sk)
                s = s.strip()
                if s and s.lower() not in _PLACEHOLDER_KEYS:
                    add(_derive_fernet_key_from_secret(s))
    except Exception:
        pass

    # 5) Dev default KDF (only if nothing collected — matches old get_encryption_key fallback)
    if not keys:
        add(_derive_fernet_key_from_secret("dev-secret-key-change-in-production"))

    return keys


def encrypt_token(token: str) -> str:
    """
    Encrypt an access token before storing in database.

    Args:
        token: Plain text access token

    Returns:
        Encrypted token (base64 string)
    """
    if not token:
        return ""

    try:
        key = get_encryption_key()
        fernet = Fernet(key)
        encrypted = fernet.encrypt(token.encode())
        return encrypted.decode()
    except Exception as e:
        raise ValueError(f"Failed to encrypt token: {e}") from e


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt an access token from database.

    Tries ``iter_fernet_decrypt_keys()`` in order so live rows encrypted under an
    older key policy (e.g. SECRET_KEY-only) still decrypt after introducing
    ``WHATSAPP_ENCRYPTION_KEY``.

    Args:
        encrypted_token: Encrypted token (base64 string)

    Returns:
        Plain text access token
    """
    if not encrypted_token:
        return ""

    raw = encrypted_token.strip()
    last_err: Optional[BaseException] = None
    for key in iter_fernet_decrypt_keys():
        try:
            fernet = Fernet(key)
            return fernet.decrypt(raw.encode()).decode()
        except Exception as e:
            last_err = e
    raise ValueError(f"Failed to decrypt token: {last_err}") from last_err
