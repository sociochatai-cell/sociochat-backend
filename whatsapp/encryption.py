"""
WhatsApp Token Encryption
=========================
Encrypt/decrypt access tokens before storing in database.
Uses Fernet (symmetric encryption) from cryptography library.
"""

import os
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from flask import current_app

# Generate or load encryption key from environment
def get_encryption_key() -> bytes:
    """
    Get encryption key from environment variable.
    If not set, generate one (for development only - should be set in production).
    """
    key_str = os.getenv("WHATSAPP_ENCRYPTION_KEY")
    
    if not key_str:
        # Development fallback - generate from SECRET_KEY
        # WARNING: In production, set WHATSAPP_ENCRYPTION_KEY explicitly
        secret = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'sociovia_wa_salt',  # Fixed salt for consistency
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(secret.encode()))
        return key
    
    # Use provided key (should be base64-encoded)
    try:
        return base64.urlsafe_b64decode(key_str.encode())
    except Exception:
        # If not base64, treat as raw and encode it
        return base64.urlsafe_b64encode(key_str.encode()[:32].ljust(32, b'0'))


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
        raise ValueError(f"Failed to encrypt token: {e}")


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt an access token from database.
    
    Args:
        encrypted_token: Encrypted token (base64 string)
        
    Returns:
        Plain text access token
    """
    if not encrypted_token:
        return ""
    
    keys_to_try = [get_encryption_key()]
    legacy_secret = os.getenv("LEGACY_SECRET_KEY") or os.getenv("PRODUCTION_SECRET_KEY")
    if legacy_secret:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b'sociovia_wa_salt',
            iterations=100000,
        )
        keys_to_try.append(base64.urlsafe_b64encode(kdf.derive(legacy_secret.encode())))
    
    last_error = None
    for key in keys_to_try:
        try:
            fernet = Fernet(key)
            decrypted = fernet.decrypt(encrypted_token.encode())
            return decrypted.decode()
        except Exception as e:
            last_error = e
            continue
    
    raise ValueError(f"Failed to decrypt token: {last_error}")

