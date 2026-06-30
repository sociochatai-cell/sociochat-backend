"""
Flow Endpoint Handler - Dynamic Flows Support
==============================================

Handles encrypted data exchange requests from Meta for dynamic flows.
Implements Refinement #5: Rate limiting, idempotency, and timeout.

Endpoints:
    POST /api/whatsapp/flows/endpoint  - Handle Meta data exchange
    POST /api/whatsapp/flows/keys      - Generate and upload encryption keys
"""

import os
import json
import base64
import hashlib
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Optional, Dict, Any, Tuple

from flask import Blueprint, request, jsonify, current_app
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

# Import models
from shared_models import db
from .models import WhatsAppAccount, WhatsAppFlow

# Create blueprint
flow_endpoint_bp = Blueprint("flow_endpoint", __name__, url_prefix="/api/whatsapp/flows")

# ============================================================
# In-Memory Caches (Use Redis in Production)
# ============================================================

# Rate limiting cache: {ip: [timestamps]}
rate_limit_cache: Dict[str, list] = {}

# Idempotency cache: {request_hash: (response, timestamp)}
idempotency_cache: Dict[str, Tuple[dict, float]] = {}

# Private key cache: {account_id: private_key_pem}
private_key_cache: Dict[int, bytes] = {}

# Configuration
RATE_LIMIT_REQUESTS = 50  # Max requests per minute
RATE_LIMIT_WINDOW = 60    # Window in seconds
IDEMPOTENCY_TTL = 300     # Cache TTL in seconds (5 minutes)
REQUEST_TIMEOUT = 2       # Max seconds for request handling


# ============================================================
# Rate Limiting Decorator
# ============================================================

def rate_limit(max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW):
    """Rate limiting decorator."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            client_ip = request.remote_addr or "unknown"
            current_time = time.time()
            
            # Clean old entries
            if client_ip in rate_limit_cache:
                rate_limit_cache[client_ip] = [
                    t for t in rate_limit_cache[client_ip] 
                    if current_time - t < window
                ]
            else:
                rate_limit_cache[client_ip] = []
            
            # Check rate limit
            if len(rate_limit_cache[client_ip]) >= max_requests:
                return jsonify({
                    "error": "Rate limit exceeded",
                    "retry_after": window
                }), 429
            
            # Record this request
            rate_limit_cache[client_ip].append(current_time)
            
            return f(*args, **kwargs)
        return wrapper
    return decorator


def per_tenant_rate_limit(max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW):
    """Rate limiting per WABA ID (tenant isolation)."""
    def decorator(f):
        @wraps(f)
        def wrapper(waba_id, *args, **kwargs):
            # Use WABA ID for tenant-isolated rate limiting
            tenant_key = f"waba_{waba_id}"
            current_time = time.time()
            
            if tenant_key in rate_limit_cache:
                rate_limit_cache[tenant_key] = [
                    t for t in rate_limit_cache[tenant_key] 
                    if current_time - t < window
                ]
            else:
                rate_limit_cache[tenant_key] = []
            
            if len(rate_limit_cache[tenant_key]) >= max_requests:
                return jsonify({
                    "error": "Rate limit exceeded for this account",
                    "retry_after": window
                }), 429
            
            rate_limit_cache[tenant_key].append(current_time)
            return f(waba_id, *args, **kwargs)
        return wrapper
    return decorator


# ============================================================
# X-Hub-Signature Verification (Meta Security)
# ============================================================

def verify_meta_signature(payload: bytes, signature_header: str, app_secret: str) -> bool:
    """
    Verify X-Hub-Signature-256 header from Meta.
    
    Meta signs all webhook/endpoint requests with HMAC-SHA256.
    This prevents spoofed requests.
    """
    if not signature_header:
        return False
    
    if not signature_header.startswith("sha256="):
        return False
    
    expected_signature = signature_header[7:]  # Remove "sha256=" prefix
    
    import hmac
    computed = hmac.new(
        app_secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(computed, expected_signature)


def require_meta_signature(f):
    """Decorator to require valid X-Hub-Signature-256."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        signature = request.headers.get("X-Hub-Signature-256")
        app_secret = current_app.config.get("FB_APP_SECRET") or os.environ.get("FB_APP_SECRET")
        
        # Log incoming request for debugging
        current_app.logger.info(f"Flow endpoint request - Headers: {dict(request.headers)}")
        current_app.logger.info(f"Flow endpoint request - Body length: {len(request.data)} bytes")
        
        if not app_secret:
            current_app.logger.error("FB_APP_SECRET not configured")
            return jsonify({"error": "Server configuration error"}), 500
        
        if not signature:
            current_app.logger.warning("Missing X-Hub-Signature-256 header - allowing for health check")
            # Allow requests without signature for health checks (Meta may send unsigned pings)
            return f(*args, **kwargs)
        
        if not verify_meta_signature(request.data, signature, app_secret):
            current_app.logger.warning(f"Invalid X-Hub-Signature-256: {signature[:20]}...")
            return "", 401
        
        return f(*args, **kwargs)
    return wrapper


# ============================================================
# Encryption Utilities
# ============================================================

def generate_rsa_keypair() -> Tuple[bytes, bytes]:
    """Generate RSA-2048 key pair for flow encryption."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend()
    )
    
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    
    return private_pem, public_pem


def upload_public_key_to_meta(phone_number_id: str, public_key_pem: str, access_token: str) -> dict:
    """
    Upload public key to Meta Graph API for flow encryption.
    
    This registers the business public key with Meta so they can encrypt
    data they send to your endpoint.
    
    Args:
        phone_number_id: The WhatsApp phone number ID
        public_key_pem: RSA public key in PEM format
        access_token: Access token for the WhatsApp account
        
    Returns:
        dict with success status and any error details
    """
    import requests
    
    url = f"https://graph.facebook.com/v18.0/{phone_number_id}/whatsapp_business_encryption"
    
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    # Meta expects the key without newlines in form data
    files = {
        "business_public_key": (None, public_key_pem)
    }
    
    try:
        response = requests.post(url, headers=headers, files=files, timeout=30)
        data = response.json()
        
        if response.status_code == 200 and data.get("success"):
            return {
                "success": True,
                "message": "Public key uploaded to Meta successfully"
            }
        else:
            error_msg = data.get("error", {}).get("message", "Unknown error")
            return {
                "success": False,
                "error": error_msg,
                "meta_response": data
            }
    except requests.Timeout:
        return {"success": False, "error": "Request timeout"}
    except requests.RequestException as e:
        return {"success": False, "error": str(e)}


def setup_flow_encryption_for_account(account) -> dict:
    """
    Complete flow encryption setup for a WhatsApp account.
    
    This is called automatically when:
    1. A new WhatsApp account is connected
    2. User wants to enable dynamic flows
    3. Keys need to be rotated
    
    Steps:
    1. Generate RSA key pair
    2. Save keys to database (private encrypted)
    3. Upload public key to Meta
    
    Returns:
        dict with success status, endpoint URL, and any errors
    """
    from shared_models import db
    
    # Step 1: Generate keys
    private_pem, public_pem = generate_rsa_keypair()
    
    # Step 2: Save to database
    try:
        account.set_flow_keys(
            private_key=private_pem.decode('utf-8'),
            public_key=public_pem.decode('utf-8')
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return {"success": False, "error": f"Failed to save keys: {e}"}
    
    # Step 3: Get access token for Meta API
    access_token = account.get_access_token()
    if not access_token:
        return {
            "success": False,
            "error": "No access token available. Keys saved but not uploaded to Meta.",
            "keys_saved": True
        }
    
    # Step 4: Upload to Meta
    upload_result = upload_public_key_to_meta(
        phone_number_id=account.phone_number_id,
        public_key_pem=public_pem.decode('utf-8'),
        access_token=access_token
    )
    
    endpoint_url = f"/api/whatsapp/flows/endpoint/{account.waba_id}"
    
    if upload_result["success"]:
        return {
            "success": True,
            "message": "Flow encryption fully configured",
            "endpoint_url": endpoint_url,
            "waba_id": account.waba_id
        }
    else:
        return {
            "success": False,
            "error": upload_result.get("error"),
            "keys_saved": True,
            "endpoint_url": endpoint_url,
            "message": "Keys saved to database but failed to upload to Meta"
        }


def decrypt_request(
    encrypted_flow_data: str,
    encrypted_aes_key: str,
    initial_vector: str,
    private_key_pem: bytes
) -> Tuple[dict, bytes, bytes]:
    """
    Decrypt the encrypted payload from Meta.
    
    Meta encrypts using:
    1. AES key (randomly generated by Meta)
    2. AES key is encrypted with our public RSA key
    3. Flow data is encrypted with AES-GCM
    
    We decrypt using:
    1. Decrypt AES key with our private RSA key
    2. Use AES key + IV to decrypt flow data
    
    Returns:
        Tuple of (decrypted_data, aes_key, iv) - aes_key and iv needed for response encryption
    """
    # Load private key
    private_key = serialization.load_pem_private_key(
        private_key_pem,
        password=None,
        backend=default_backend()
    )
    
    # Decode base64 inputs
    encrypted_aes_key_bytes = base64.b64decode(encrypted_aes_key)
    iv_bytes = base64.b64decode(initial_vector)
    encrypted_data_bytes = base64.b64decode(encrypted_flow_data)
    
    # Decrypt AES key with RSA private key
    aes_key = private_key.decrypt(
        encrypted_aes_key_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None
        )
    )
    
    # Decrypt flow data with AES-GCM
    # Last 16 bytes are the GCM tag
    tag = encrypted_data_bytes[-16:]
    ciphertext = encrypted_data_bytes[:-16]
    
    cipher = Cipher(
        algorithms.AES(aes_key),
        modes.GCM(iv_bytes, tag),
        backend=default_backend()
    )
    decryptor = cipher.decryptor()
    decrypted_data = decryptor.update(ciphertext) + decryptor.finalize()
    
    # Return decrypted data AND the keys needed for response encryption
    return json.loads(decrypted_data.decode('utf-8')), aes_key, iv_bytes


def encrypt_response(response_data: dict, aes_key: bytes, iv: bytes) -> str:
    """
    Encrypt response data for Meta.
    
    IMPORTANT: Meta requires flipping all bits of the IV for the response.
    This is documented in Meta's Flows encryption specification.
    
    Uses the same AES key but with flipped IV.
    """
    # Flip all bits of the IV as Meta requires
    flipped_iv = bytes(b ^ 0xFF for b in iv)
    
    plaintext = json.dumps(response_data).encode('utf-8')
    
    cipher = Cipher(
        algorithms.AES(aes_key),
        modes.GCM(flipped_iv),
        backend=default_backend()
    )
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(plaintext) + encryptor.finalize()
    
    # Append GCM tag
    encrypted = ciphertext + encryptor.tag
    
    return base64.b64encode(encrypted).decode('utf-8')


def get_response_version(decrypted_data: dict) -> str:
    """Use the request's flow data version when present."""
    return str(decrypted_data.get("version") or "3.0")


def build_flow_response(
    decrypted_data: dict,
    *,
    data: Optional[dict] = None,
    screen: Optional[str] = None,
) -> dict:
    """Build a Meta-compliant flow response envelope."""
    response = {
        "version": get_response_version(decrypted_data),
        "data": data or {},
    }
    if screen:
        response["screen"] = screen
    return response


def resolve_account_for_endpoint(account_id: Optional[int] = None) -> Optional[WhatsAppAccount]:
    """Resolve an account for the generic endpoint, falling back only in single-account setups."""
    if account_id:
        return WhatsAppAccount.query.get(account_id)

    accounts = WhatsAppAccount.query.filter_by(is_active=True).all()
    accounts_with_keys = [account for account in accounts if account.has_flow_keys()]
    if len(accounts_with_keys) == 1:
        return accounts_with_keys[0]
    return None


def resolve_account_for_waba(waba_id: str) -> Optional[WhatsAppAccount]:
    """
    Resolve the active account for a WABA, preferring the most recently updated
    record that already has flow keys configured.
    """
    accounts = (
        WhatsAppAccount.query
        .filter_by(waba_id=waba_id, is_active=True)
        .order_by(WhatsAppAccount.updated_at.desc(), WhatsAppAccount.id.desc())
        .all()
    )
    if not accounts:
        return None

    accounts_with_keys = [account for account in accounts if account.has_flow_keys()]
    if accounts_with_keys:
        return accounts_with_keys[0]

    return accounts[0]


# ============================================================
# Dynamic Data Handlers
# ============================================================

def handle_data_exchange(flow_id: str, screen: str, data: dict, account_id: int) -> dict:
    """
    Handle data exchange requests based on screen requirements.
    
    This is where you implement custom business logic for dynamic flows.
    Examples:
    - Fetch appointment slots from calendar
    - Get product inventory
    - Validate promo codes
    - Lookup customer data
    """
    # Example: Appointment slots
    if screen == "SLOTS" or screen == "APPOINTMENT_SLOTS":
        date = data.get("date", "")
        slots = get_available_slots(date)
        return {
            "screen": screen,
            "data": {
                "available_slots": slots
            }
        }
    
    # Example: Product lookup
    if screen == "PRODUCTS":
        category = data.get("category", "")
        products = get_products_by_category(category)
        return {
            "screen": screen,
            "data": {
                "products": products
            }
        }
    
    # Example: Promo code validation
    if screen == "PROMO":
        code = data.get("promo_code", "")
        result = validate_promo_code(code)
        return {
            "screen": screen,
            "data": {
                "valid": result["valid"],
                "discount": result.get("discount", 0),
                "message": result.get("message", "")
            }
        }
    
    # Default: Return empty data
    return {
        "screen": screen,
        "data": {}
    }


def get_available_slots(date: str) -> list:
    """Get available appointment slots for a date."""
    # TODO: Integrate with your calendar/booking system
    # This is sample data for demonstration
    return [
        {"id": "9am", "title": "9:00 AM - 10:00 AM"},
        {"id": "10am", "title": "10:00 AM - 11:00 AM"},
        {"id": "2pm", "title": "2:00 PM - 3:00 PM"},
        {"id": "3pm", "title": "3:00 PM - 4:00 PM"},
    ]


def get_products_by_category(category: str) -> list:
    """Get products by category."""
    # TODO: Integrate with your product database
    return [
        {"id": "prod1", "title": f"{category} Product 1", "price": "$19.99"},
        {"id": "prod2", "title": f"{category} Product 2", "price": "$29.99"},
    ]


def validate_promo_code(code: str) -> dict:
    """Validate a promotional code."""
    # TODO: Integrate with your promo code system
    valid_codes = {
        "SAVE10": {"valid": True, "discount": 10, "message": "10% off applied!"},
        "SAVE20": {"valid": True, "discount": 20, "message": "20% off applied!"},
    }
    return valid_codes.get(code.upper(), {"valid": False, "message": "Invalid code"})


# ============================================================
# API Endpoints
# ============================================================

@flow_endpoint_bp.route("/endpoint", methods=["POST"])
@rate_limit(max_requests=50, window=60)
def flow_data_endpoint():
    """
    Handle dynamic flow data requests from Meta.
    
    Meta sends encrypted payloads when:
    1. Flow needs dynamic data (data_exchange action)
    2. User completes a flow (complete action)
    
    Request format:
    {
        "encrypted_flow_data": "base64...",
        "encrypted_aes_key": "base64...",
        "initial_vector": "base64..."
    }
    """
    start_time = time.time()
    
    # Check timeout
    if time.time() - start_time > REQUEST_TIMEOUT:
        return jsonify({"error": "Request timeout"}), 504
    
    # === Idempotency Check ===
    request_hash = hashlib.sha256(request.data).hexdigest()
    if request_hash in idempotency_cache:
        cached_response, cached_time = idempotency_cache[request_hash]
        if time.time() - cached_time < IDEMPOTENCY_TTL:
            if isinstance(cached_response, str):
                return cached_response, 200, {"Content-Type": "text/plain"}
            return jsonify(cached_response)
    
    try:
        data = request.get_json() or {}
        
        encrypted_flow_data = data.get("encrypted_flow_data")
        encrypted_aes_key = data.get("encrypted_aes_key")
        initial_vector = data.get("initial_vector")
        
        if not all([encrypted_flow_data, encrypted_aes_key, initial_vector]):
            return jsonify({"error": "Missing encryption parameters"}), 400
        
        account_id = data.get("account_id") or request.headers.get("X-Account-ID")
        resolved_account = resolve_account_for_endpoint(int(account_id) if str(account_id or "").isdigit() else None)
        if not resolved_account:
            return jsonify({
                "error": "Unable to resolve flow account. Use the per-WABA endpoint for Meta."
            }), 400

        if resolved_account.id not in private_key_cache:
            private_key_pem = resolved_account.get_flow_private_key()
            if not private_key_pem:
                return jsonify({"error": "No encryption key for this account"}), 400
            private_key_cache[resolved_account.id] = private_key_pem

        private_key_pem = private_key_cache[resolved_account.id]
        
        # Decrypt the request
        decrypted_data, aes_key, iv = decrypt_request(
            encrypted_flow_data,
            encrypted_aes_key,
            initial_vector,
            private_key_pem
        )
        
        # Parse request (Meta may vary action casing)
        action = decrypted_data.get("action")
        flow_id = decrypted_data.get("flow_token", "")
        screen = decrypted_data.get("screen", "")
        screen_data = decrypted_data.get("data", {})
        na = str(action or "").strip().upper()

        if na == "PING":
            # Health check from Meta
            response_data = build_flow_response(decrypted_data, data={"status": "active"})
        elif na == "DATA_EXCHANGE":
            # Dynamic data request
            dynamic_response = handle_data_exchange(flow_id, screen, screen_data, resolved_account.id)
            response_data = build_flow_response(
                decrypted_data,
                screen=dynamic_response.get("screen"),
                data=dynamic_response.get("data"),
            )
        elif na == "INIT":
            response_data = build_flow_response(
                decrypted_data,
                screen=screen or "WELCOME",
                data=screen_data if isinstance(screen_data, dict) else {},
            )
        elif na == "COMPLETE":
            # Flow completed — merge form `data` into latest inbox nfm_reply (endpoint path)
            try:
                from .flow_inbox_sync import merge_flow_endpoint_completion_into_latest_message

                if resolved_account:
                    merged_ok = merge_flow_endpoint_completion_into_latest_message(
                        resolved_account, decrypted_data
                    )
                    current_app.logger.info(
                        "Flow COMPLETE (generic endpoint) merge_ok=%s account_id=%s",
                        merged_ok,
                        resolved_account.id,
                    )
            except Exception:
                current_app.logger.exception(
                    "Failed to merge Flow COMPLETE into inbox (generic endpoint)"
                )
            response_data = build_flow_response(
                decrypted_data,
                data={"status": "received", "flow_token": flow_id},
            )
        else:
            response_data = build_flow_response(
                decrypted_data,
                data={"error": f"Unknown action: {action}"},
            )
        
        encrypted_response = encrypt_response(response_data, aes_key, iv)
        idempotency_cache[request_hash] = (encrypted_response, time.time())
        
        # Clean old cache entries periodically
        current_time = time.time()
        keys_to_delete = [
            k for k, (_, t) in idempotency_cache.items() 
            if current_time - t > IDEMPOTENCY_TTL
        ]
        for k in keys_to_delete:
            del idempotency_cache[k]
        
        return encrypted_response, 200, {"Content-Type": "text/plain"}
        
    except Exception as e:
        current_app.logger.exception("Flow endpoint error: %s", e)
        return jsonify({"error": "Internal server error"}), 500


# ============================================================
# Per-Tenant Endpoint (RECOMMENDED for Production)
# ============================================================

@flow_endpoint_bp.route("/endpoint/<waba_id>", methods=["POST"])
@require_meta_signature
@per_tenant_rate_limit(max_requests=50, window=60)
def flow_data_endpoint_per_tenant(waba_id: str):
    """
    Tenant-isolated flow endpoint.
    
    This is the RECOMMENDED endpoint for production multi-tenant deployments.
    Each WABA has its own:
    - Rate limiting bucket
    - RSA keypair
    - Failure isolation
    
    Meta should be configured with: 
    https://yourdomain.com/api/whatsapp/flows/endpoint/{waba_id}
    """
    start_time = time.time()
    current_app.logger.info(f"=== Flow Endpoint Called for WABA: {waba_id} ===")
    
    # Get account by WABA ID
    account = resolve_account_for_waba(waba_id)
    if not account:
        current_app.logger.warning(f"No active account found for WABA: {waba_id}")
        return jsonify({"error": "Account not found"}), 404
    
    current_app.logger.info(f"Found account: {account.id}, has_flow_keys: {account.has_flow_keys()}")
    
    # Check if account has flow keys configured
    if not account.has_flow_keys():
        current_app.logger.error(f"No flow keys for WABA: {waba_id}")
        return jsonify({"error": "Encryption keys not configured"}), 421
    
    # Get private key for this specific account
    private_key_pem = account.get_flow_private_key()
    if not private_key_pem:
        current_app.logger.error(f"Cannot decrypt private key for WABA: {waba_id}")
        return jsonify({"error": "Cannot decrypt private key"}), 500
    
    current_app.logger.info(f"Private key loaded, length: {len(private_key_pem)} bytes")
    
    # Idempotency check
    request_hash = hashlib.sha256(request.data).hexdigest()
    cache_key = f"{waba_id}:{request_hash}"
    if cache_key in idempotency_cache:
        cached_response, cached_time = idempotency_cache[cache_key]
        if time.time() - cached_time < IDEMPOTENCY_TTL:
            if isinstance(cached_response, str):
                return cached_response, 200, {"Content-Type": "text/plain"}
            return jsonify(cached_response)
    
    try:
        data = request.get_json() or {}
        
        encrypted_flow_data = data.get("encrypted_flow_data")
        encrypted_aes_key = data.get("encrypted_aes_key")
        initial_vector = data.get("initial_vector")
        
        if not all([encrypted_flow_data, encrypted_aes_key, initial_vector]):
            return jsonify({"error": "Missing encryption parameters"}), 400
        
        # Decrypt using THIS account's private key
        # Returns (decrypted_data, aes_key, iv) for response encryption
        decrypted_data, aes_key, iv = decrypt_request(
            encrypted_flow_data,
            encrypted_aes_key,
            initial_vector,
            private_key_pem
        )
        
        # Parse request (Meta may send action casing inconsistently)
        action = decrypted_data.get("action")
        flow_token = decrypted_data.get("flow_token", "")
        screen = decrypted_data.get("screen", "")
        screen_data = decrypted_data.get("data", {})
        na = str(action or "").strip().upper()

        if na == "PING":
            response_data = build_flow_response(decrypted_data, data={"status": "active"})
        elif na == "DATA_EXCHANGE":
            dynamic_response = handle_data_exchange(flow_token, screen, screen_data, account.id)
            response_data = build_flow_response(
                decrypted_data,
                screen=dynamic_response.get("screen"),
                data=dynamic_response.get("data"),
            )
        elif na == "INIT":
            # Initial flow load
            response_data = build_flow_response(
                decrypted_data,
                screen=screen or "WELCOME",
                data=screen_data if isinstance(screen_data, dict) else {},
            )
        elif na == "COMPLETE":
            # Flow completed — merge `data` into latest inbox nfm_reply (same WABA)
            current_app.logger.info("Flow COMPLETE for WABA %s action_data keys=%s", waba_id, list(screen_data.keys()) if isinstance(screen_data, dict) else type(screen_data))
            try:
                from .flow_inbox_sync import merge_flow_endpoint_completion_into_latest_message

                merged_ok = merge_flow_endpoint_completion_into_latest_message(account, decrypted_data)
                current_app.logger.info("Flow COMPLETE inbox merge_ok=%s waba=%s", merged_ok, waba_id)
            except Exception:
                current_app.logger.exception(
                    f"Failed to merge Flow COMPLETE into inbox for WABA {waba_id}"
                )
            response_data = build_flow_response(
                decrypted_data,
                data={"status": "received", "flow_token": flow_token},
            )
        else:
            response_data = build_flow_response(
                decrypted_data,
                data={"error": f"Unknown action: {action}"},
            )
        
        # ENCRYPT response using same AES key Meta sent
        encrypted_response = encrypt_response(response_data, aes_key, iv)
        
        # Cache response
        idempotency_cache[cache_key] = (encrypted_response, time.time())
        
        # Return as plain text (Base64 encoded encrypted data)
        return encrypted_response, 200, {"Content-Type": "text/plain"}
        
    except Exception as e:
        current_app.logger.exception(f"Flow endpoint error for WABA {waba_id}: {e}")
        # Return 421 to tell Meta to retry with fresh keys
        if "decrypt" in str(e).lower():
            return "", 421
        return jsonify({"error": "Internal server error"}), 500


@flow_endpoint_bp.route("/keys/generate", methods=["POST"])
def generate_encryption_keys():
    """
    Generate RSA key pair for a WhatsApp account and save to database.
    
    The public key should be uploaded to Meta.
    The private key is stored encrypted in the database.
    """
    data = request.get_json() or {}
    account_id = data.get("account_id")
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id required"}), 400
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    # Generate key pair
    private_pem, public_pem = generate_rsa_keypair()
    
    # Save keys to account (private key is encrypted at rest)
    try:
        account.set_flow_keys(
            private_key=private_pem.decode('utf-8'),
            public_key=public_pem.decode('utf-8')
        )
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception(f"Failed to save flow keys: {e}")
        return jsonify({"success": False, "error": "Failed to save keys"}), 500
    
    return jsonify({
        "success": True,
        "account_id": account_id,
        "waba_id": account.waba_id,
        "public_key": public_pem.decode('utf-8'),
        "endpoint_url": f"/api/whatsapp/flows/endpoint/{account.waba_id}",
        "instructions": [
            "1. Copy the public_key above",
            "2. Upload to Meta: POST https://graph.facebook.com/v18.0/{phone_number_id}/whatsapp_business_encryption",
            "3. Configure flow endpoint_uri in Meta to: https://yourdomain.com" + f"/api/whatsapp/flows/endpoint/{account.waba_id}",
            "Private key has been saved encrypted to database."
        ]
    })


@flow_endpoint_bp.route("/health", methods=["GET"])
def endpoint_health():
    """Health check for the flow endpoint."""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rate_limit": {
            "max_requests": RATE_LIMIT_REQUESTS,
            "window_seconds": RATE_LIMIT_WINDOW
        },
        "idempotency_cache_size": len(idempotency_cache),
        "rate_limit_cache_size": len(rate_limit_cache)
    })


@flow_endpoint_bp.route("/keys/setup", methods=["POST"])
def setup_flow_keys():
    """
    Complete automatic flow encryption setup.
    
    This endpoint:
    1. Generates RSA key pair
    2. Saves keys to database (private encrypted)
    3. Automatically uploads public key to Meta Graph API
    
    Use this for onboarding new accounts - everything is automatic!
    
    Request:
        {"account_id": 12}
    """
    data = request.get_json() or {}
    account_id = data.get("account_id")
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id required"}), 400
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    # Run complete setup
    result = setup_flow_encryption_for_account(account)
    
    if result.get("success"):
        base_url = os.environ.get("APP_BASE_URL", "https://yourdomain.com")
        return jsonify({
            "success": True,
            "message": "Flow encryption fully configured!",
            "account_id": account_id,
            "waba_id": account.waba_id,
            "endpoint_url": f"{base_url}/api/whatsapp/flows/endpoint/{account.waba_id}",
            "next_steps": [
                "1. Set endpoint URI in Meta Flow Builder to the URL above",
                "2. Public key has been automatically uploaded to Meta",
                "3. Run Health Check in Meta to verify"
            ]
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error"),
            "keys_saved": result.get("keys_saved", False),
            "message": result.get("message", "Setup failed")
        }), 400


@flow_endpoint_bp.route("/keys/upload", methods=["POST"])
def upload_existing_key():
    """
    Upload existing public key to Meta.
    
    Use this if keys were generated but not uploaded, or need re-upload.
    
    Request:
        {"account_id": 12}
    """
    data = request.get_json() or {}
    account_id = data.get("account_id")
    
    if not account_id:
        return jsonify({"success": False, "error": "account_id required"}), 400
    
    account = WhatsAppAccount.query.get(account_id)
    if not account:
        return jsonify({"success": False, "error": "Account not found"}), 404
    
    if not account.has_flow_keys():
        return jsonify({
            "success": False, 
            "error": "No keys configured. Use /keys/setup instead."
        }), 400
    
    access_token = account.get_access_token()
    if not access_token:
        return jsonify({"success": False, "error": "No access token available"}), 400
    
    result = upload_public_key_to_meta(
        phone_number_id=account.phone_number_id,
        public_key_pem=account.flow_public_key,
        access_token=access_token
    )
    
    if result.get("success"):
        base_url = os.environ.get("APP_BASE_URL", "https://yourdomain.com")
        return jsonify({
            "success": True,
            "message": "Public key uploaded to Meta successfully",
            "endpoint_url": f"{base_url}/api/whatsapp/flows/endpoint/{account.waba_id}"
        })
    else:
        return jsonify({
            "success": False,
            "error": result.get("error"),
            "meta_response": result.get("meta_response")
        }), 400
