"""
WhatsApp Product Catalog Management Routes
==========================================

Manages Meta Commerce product catalogs connected to a WhatsApp Business Account.

Endpoints (all under /api/whatsapp/):
    GET    /catalogs                  - List catalogs connected to WABA
    GET    /catalogs/available        - List all business-owned catalogs (for connecting)
    GET    /catalogs/health           - Run comprehensive catalog health diagnostics
    POST   /catalogs/connect          - Connect an existing catalog to WABA
    POST   /catalogs/create           - Create a new catalog under the Business Manager
    DELETE /catalogs/<catalog_id>     - Disconnect a catalog from WABA
"""

import os
import logging
import requests as http_requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger(__name__)

catalog_bp = Blueprint("catalog_bp", __name__)

GRAPH_API_BASE = "https://graph.facebook.com"


def _get_api_version() -> str:
    return os.getenv("WHATSAPP_API_VERSION", "v22.0")


def _get_account_and_token():
    """
    Look up WhatsAppAccount by account_id or workspace_id from query params or JSON body.
    Falls back to the first active account if neither is supplied.
    Returns (account, access_token).
    Raises ValueError with a descriptive message on failure.
    """
    from .models import WhatsAppAccount

    data = (request.get_json(silent=True) or {}) if request.is_json else {}
    account_id = data.get("account_id") or request.args.get("account_id")
    workspace_id = data.get("workspace_id") or request.args.get("workspace_id")

    if account_id:
        account = WhatsAppAccount.query.filter_by(id=account_id, is_active=True).first()
    elif workspace_id:
        account = WhatsAppAccount.query.filter_by(
            workspace_id=str(workspace_id),
            is_active=True,
        ).first()
    else:
        account = WhatsAppAccount.query.filter_by(is_active=True).first()

    if not account:
        raise ValueError("No active WhatsApp account found")

    token = account.get_access_token()
    if not token:
        raise ValueError("No access token available for this account")

    return account, token


# ── List connected catalogs ──────────────────────────────────────────────────

@catalog_bp.route("/catalogs", methods=["GET"])
def list_connected_catalogs():
    """Return the list of product catalogs currently connected to the WABA."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{account.waba_id}/product_catalogs"

    try:
        resp = http_requests.get(
            url,
            params={"fields": "id,name,vertical,product_count"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog list request failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = data.get("error", {})
        logger.warning("Meta catalog list error: %s", data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Meta API error",
            "meta_error": err,
        }), resp.status_code

    return jsonify({
        "success": True,
        "catalogs": data.get("data", []),
        "waba_id": account.waba_id,
        "account_id": account.id,
    })


# ── List available (business-owned) catalogs ─────────────────────────────────

@catalog_bp.route("/catalogs/available", methods=["GET"])
def list_available_catalogs():
    """Return all catalogs owned by the Meta Business Manager for this account."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    business_id = account.meta_business_id
    if not business_id:
        return jsonify({
            "success": False,
            "error": "No Meta Business Manager ID linked to this account. Add it in WhatsApp Settings.",
            "needs_business_id": True,
        }), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{business_id}/owned_product_catalogs"

    try:
        resp = http_requests.get(
            url,
            params={"fields": "id,name,vertical,product_count"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Available catalog list failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = data.get("error", {})
        return jsonify({
            "success": False,
            "error": err.get("message") or "Meta API error",
            "meta_error": err,
        }), resp.status_code

    return jsonify({
        "success": True,
        "catalogs": data.get("data", []),
        "business_id": business_id,
    })


# ── Connect existing catalog ──────────────────────────────────────────────────

@catalog_bp.route("/catalogs/connect", methods=["POST"])
def connect_catalog():
    """Connect an existing catalog (by ID) to the WABA."""
    body = request.get_json(silent=True) or {}
    catalog_id = str(body.get("catalog_id", "")).strip()

    if not catalog_id:
        return jsonify({"success": False, "error": "catalog_id is required"}), 422

    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{account.waba_id}/product_catalogs"

    try:
        resp = http_requests.post(
            url,
            json={"catalog_id": catalog_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog connect request failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to connect catalog",
            "meta_error": err,
        }), resp.status_code

    logger.info("Catalog %s connected to WABA %s", catalog_id, account.waba_id)
    return jsonify({
        "success": True,
        "message": "Catalog connected successfully",
        "catalog_id": catalog_id,
    })


# ── Create new catalog ────────────────────────────────────────────────────────

@catalog_bp.route("/catalogs/create", methods=["POST"])
def create_catalog():
    """Create a new product catalog under the Meta Business Manager."""
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()

    if not name:
        return jsonify({"success": False, "error": "Catalog name is required"}), 422

    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    business_id = account.meta_business_id
    if not business_id:
        return jsonify({
            "success": False,
            "error": "No Meta Business Manager ID linked to this account.",
            "needs_business_id": True,
        }), 400

    api_version = _get_api_version()

    # Fetch existing catalogs to check for duplicate names
    check_url = f"{GRAPH_API_BASE}/{api_version}/{business_id}/owned_product_catalogs"
    try:
        check_resp = http_requests.get(
            check_url,
            params={"fields": "id,name"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if check_resp.ok:
            existing_data = check_resp.json().get("data", []) or []
            for cat in existing_data:
                if cat.get("name", "").strip().lower() == name.lower():
                    return jsonify({
                        "success": False,
                        "error": f"A catalog named '{name}' already exists. Please choose a unique name."
                    }), 400
    except Exception as exc:
        logger.warning("Failed to verify catalog name uniqueness: %s", exc)

    url = f"{GRAPH_API_BASE}/{api_version}/{business_id}/owned_product_catalogs"

    payload: dict = {"name": name, "vertical": "commerce"}
    if description:
        payload["description"] = description

    try:
        resp = http_requests.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog create request failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to create catalog",
            "meta_error": err,
        }), resp.status_code

    new_catalog_id = resp_data.get("id")
    logger.info("New catalog created: %s for business %s", new_catalog_id, business_id)
    return jsonify({
        "success": True,
        "message": "Catalog created successfully",
        "catalog_id": new_catalog_id,
        "catalog": resp_data,
    }), 201


# ── Disconnect catalog ────────────────────────────────────────────────────────

@catalog_bp.route("/catalogs/<catalog_id>", methods=["DELETE"])
def disconnect_catalog(catalog_id: str):
    """Disconnect a catalog from the WABA (does not delete the catalog from Meta)."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{account.waba_id}/product_catalogs"

    try:
        resp = http_requests.delete(
            url,
            json={"catalog_id": catalog_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog disconnect request failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to disconnect catalog",
            "meta_error": err,
        }), resp.status_code

    logger.info("Catalog %s disconnected from WABA %s", catalog_id, account.waba_id)
    return jsonify({
        "success": True,
        "message": "Catalog disconnected successfully",
    })


# ── Delete catalog permanently ────────────────────────────────────────────────

@catalog_bp.route("/catalogs/<catalog_id>/permanent", methods=["DELETE"])
def delete_catalog_permanent(catalog_id: str):
    """Disconnect a catalog from WABA if connected, and permanently delete it from Meta."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()

    # Step 1: Disconnect the catalog from WABA first (best effort)
    disconnect_url = f"{GRAPH_API_BASE}/{api_version}/{account.waba_id}/product_catalogs"
    try:
        http_requests.delete(
            disconnect_url,
            json={"catalog_id": catalog_id},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Catalog disconnect during permanent delete failed: %s", exc)

    # Step 2: Delete catalog from Meta Business Manager
    delete_url = f"{GRAPH_API_BASE}/{api_version}/{catalog_id}"
    try:
        resp = http_requests.delete(
            delete_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog permanent delete request failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        logger.warning("Meta catalog delete error: %s", resp_data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to delete catalog from Meta",
            "meta_error": err,
        }), resp.status_code

    logger.info("Catalog %s permanently deleted from Meta by WABA %s", catalog_id, account.waba_id)
    return jsonify({
        "success": True,
        "message": "Catalog permanently deleted from Meta and disconnected from WhatsApp",
    })


# ── Commerce Settings & Product List ─────────────────────────────────────────

@catalog_bp.route("/catalogs/commerce-settings", methods=["GET"])
def get_commerce_settings():
    """Retrieve commerce settings (is_cart_enabled, is_catalog_visible) for the phone number."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{account.phone_number_id}/whatsapp_commerce_settings"

    try:
        resp = http_requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Commerce settings get failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = data.get("error", {})
        logger.warning("Meta commerce settings get error: %s", data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Meta API error",
            "meta_error": err,
        }), resp.status_code

    # Meta returns data as a list of dicts, or a direct object
    settings_data = data
    if isinstance(data.get("data"), list) and len(data["data"]) > 0:
        settings_data = data["data"][0]

    return jsonify({
        "success": True,
        "is_cart_enabled": settings_data.get("is_cart_enabled", True),
        "is_catalog_visible": settings_data.get("is_catalog_visible", False),
    })


@catalog_bp.route("/catalogs/commerce-settings", methods=["POST"])
def update_commerce_settings():
    """Update commerce settings (is_cart_enabled, is_catalog_visible) for the phone number."""
    body = request.get_json(silent=True) or {}
    is_cart_enabled = body.get("is_cart_enabled")
    is_catalog_visible = body.get("is_catalog_visible")

    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{account.phone_number_id}/whatsapp_commerce_settings"

    params = {}
    if is_cart_enabled is not None:
        params["is_cart_enabled"] = "true" if is_cart_enabled else "false"
    if is_catalog_visible is not None:
        params["is_catalog_visible"] = "true" if is_catalog_visible else "false"

    if not params:
        return jsonify({"success": False, "error": "No settings to update provided"}), 400

    try:
        resp = http_requests.post(
            url,
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Commerce settings update failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = data.get("error", {})
        logger.warning("Meta commerce settings update error: %s", data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to update commerce settings",
            "meta_error": err,
        }), resp.status_code

    return jsonify({
        "success": True,
        "message": "Commerce settings updated successfully",
    })


@catalog_bp.route("/catalogs/product-image", methods=["GET"])
def proxy_catalog_product_image():
    """
    Proxy catalog product images for inbox rendering (CORS / Google Drive / Meta CDN safe).
    GET /api/whatsapp/catalogs/product-image?url=...&product_id=...&workspace_id=...
    """
    from flask import Response
    from .order_enrichment import (
        _fetch_product_by_id,
        _sanitize_catalog_image_url,
        fetch_catalog_image_bytes,
    )

    image_url = (request.args.get("url") or "").strip()
    product_id = (request.args.get("product_id") or "").strip()

    try:
        account, token = _get_account_and_token()
    except ValueError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    if not image_url and product_id:
        meta = _fetch_product_by_id(product_id, token)
        image_url = (meta.get("image_url") or "").strip()

    if not image_url:
        return jsonify({"success": False, "error": "url or product_id is required"}), 400

    image_url = _sanitize_catalog_image_url(image_url)
    image_bytes, content_type = fetch_catalog_image_bytes(image_url, access_token=token)
    if not image_bytes:
        return jsonify({"success": False, "error": "Unable to load product image"}), 404

    return Response(
        image_bytes,
        mimetype=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@catalog_bp.route("/catalogs/<catalog_id>/products", methods=["GET"])
def list_catalog_products(catalog_id: str):
    """Retrieve products in the specified Meta product catalog."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{catalog_id}/products"
    
    # We want product fields: id, name, description, price, currency, image_url, availability
    fields = "id,name,description,price,currency,image_url,availability,retailer_id"
    
    try:
        resp = http_requests.get(
            url,
            params={"fields": fields, "limit": 50},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Catalog products fetch failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = data.get("error", {})
        logger.warning("Meta catalog products fetch error: %s", data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Meta API error",
            "meta_error": err,
        }), resp.status_code

    return jsonify({
        "success": True,
        "products": data.get("data", []),
    })


@catalog_bp.route("/catalogs/<catalog_id>/products", methods=["POST"])
def create_catalog_product(catalog_id: str):
    """Add a new product to the specified product catalog on Meta."""
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    description = body.get("description")
    price = body.get("price")  # Decimal string or float, e.g., 99.99
    currency = body.get("currency", "USD")
    image_url = body.get("image_url")
    url = body.get("url")
    brand = body.get("brand")
    retailer_id = body.get("retailer_id")
    availability = body.get("availability", "in stock")
    condition = body.get("condition", "new")

    if not all([name, price, currency, image_url, url, brand, retailer_id]):
        return jsonify({
            "success": False,
            "error": "Missing required fields. Name, Price, Currency, Image URL, Web URL, Brand, and Retailer ID (SKU) are all required."
        }), 422

    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    endpoint = f"{GRAPH_API_BASE}/{api_version}/{catalog_id}/products"

    try:
        price_cents = int(float(price) * 100)
    except Exception:
        return jsonify({"success": False, "error": "Invalid price format. Must be a number."}), 422

    payload = {
        "name": name,
        "description": description or name,
        "price": price_cents,
        "currency": currency,
        "image_url": image_url,
        "url": url,
        "brand": brand,
        "retailer_id": retailer_id,
        "availability": availability,
        "condition": condition
    }

    try:
        resp = http_requests.post(
            endpoint,
            json=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            timeout=15
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Product create failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        logger.warning("Meta product create error: %s", resp_data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to add product to catalog",
            "meta_error": err
        }), resp.status_code

    return jsonify({
        "success": True,
        "message": "Product added successfully",
        "product_id": resp_data.get("id")
    }), 201


@catalog_bp.route("/catalogs/products/<product_id>", methods=["DELETE"])
def delete_catalog_product(product_id: str):
    """Delete a product item from a Meta catalog."""
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    api_version = _get_api_version()
    url = f"{GRAPH_API_BASE}/{api_version}/{product_id}"

    try:
        resp = http_requests.delete(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        resp_data = resp.json() if resp.content else {}
    except Exception as exc:
        logger.error("Product delete failed: %s", exc)
        return jsonify({"success": False, "error": "Failed to reach Meta API"}), 502

    if not resp.ok:
        err = resp_data.get("error", {})
        logger.warning("Meta product delete error: %s", resp_data)
        return jsonify({
            "success": False,
            "error": err.get("message") or "Failed to delete product",
            "meta_error": err
        }), resp.status_code

    return jsonify({
        "success": True,
        "message": "Product deleted successfully from catalog"
    })


# ── Catalog Health Diagnostics ────────────────────────────────────────────────

@catalog_bp.route("/catalogs/health", methods=["GET"])
def catalog_health_check():
    """Run comprehensive diagnostic checks on the catalog setup and return a readiness score."""
    checks = []

    def _add(check_id, label, status, detail, fix=None):
        entry = {"id": check_id, "label": label, "status": status, "detail": detail}
        if fix:
            entry["fix"] = fix
        checks.append(entry)

    # ── Check 1: WABA Connected ──────────────────────────────────────────
    try:
        account, token = _get_account_and_token()
    except ValueError as e:
        _add("waba_connected", "WhatsApp Account Connected", "fail",
             str(e), "Connect a WhatsApp Business Account in WhatsApp Settings.")
        # Cannot proceed without account
        return jsonify({
            "success": True, "score": 0, "max_score": 100,
            "checks": checks, "overall_status": "setup_required",
        })

    _add("waba_connected", "WhatsApp Account Connected", "pass",
         f"WABA {account.waba_id} is active (Phone: {account.display_phone_number or 'N/A'})")

    api_version = _get_api_version()

    # ── Check 2: Business Manager ID ─────────────────────────────────────
    business_id = account.meta_business_id
    if not business_id:
        _add("business_id", "Meta Business Manager ID", "fail",
             "No Meta Business Manager ID is configured for this account.",
             "Go to WhatsApp Settings and add your Meta Business Manager ID.")
    else:
        _add("business_id", "Meta Business Manager ID", "pass",
             f"Business ID: {business_id}")

    # ── Check 3: Catalog Exists (business-owned) ─────────────────────────
    owned_catalogs = []
    if business_id:
        try:
            url = f"{GRAPH_API_BASE}/{api_version}/{business_id}/owned_product_catalogs"
            resp = http_requests.get(
                url, params={"fields": "id,name,product_count"},
                headers={"Authorization": f"Bearer {token}"}, timeout=15,
            )
            if resp.ok:
                owned_catalogs = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("Health check: owned catalogs fetch failed: %s", exc)

    if not business_id:
        _add("catalog_exists", "Catalog Created", "fail",
             "Cannot check catalogs without a Business Manager ID.",
             "Configure your Business Manager ID first.")
    elif len(owned_catalogs) == 0:
        _add("catalog_exists", "Catalog Created", "fail",
             "No catalogs found under your Business Manager.",
             "Create a new catalog on the 'New Catalog' tab.")
    else:
        _add("catalog_exists", "Catalog Created", "pass",
             f"{len(owned_catalogs)} catalog(s) owned by Business Manager.")

    # ── Check 4: Catalog Linked to WABA ──────────────────────────────────
    connected_catalogs = []
    try:
        url = f"{GRAPH_API_BASE}/{api_version}/{account.waba_id}/product_catalogs"
        resp = http_requests.get(
            url, params={"fields": "id,name,product_count"},
            headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        if resp.ok:
            connected_catalogs = resp.json().get("data", [])
    except Exception as exc:
        logger.warning("Health check: connected catalogs fetch failed: %s", exc)

    if len(connected_catalogs) == 0:
        _add("catalog_linked", "Catalog Linked to WABA", "fail",
             "No catalog is connected to your WhatsApp Business Account.",
             "Link a catalog on the 'Link Catalog' tab.")
    else:
        _add("catalog_linked", "Catalog Linked to WABA", "pass",
             f"{len(connected_catalogs)} catalog(s) connected to WABA.")

    # ── Check 5 & 6: Commerce Settings ───────────────────────────────────
    cart_enabled = False
    catalog_visible = False
    try:
        url = f"{GRAPH_API_BASE}/{api_version}/{account.phone_number_id}/whatsapp_commerce_settings"
        resp = http_requests.get(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=15,
        )
        if resp.ok:
            sdata = resp.json()
            if isinstance(sdata.get("data"), list) and len(sdata["data"]) > 0:
                sdata = sdata["data"][0]
            cart_enabled = sdata.get("is_cart_enabled", False)
            catalog_visible = sdata.get("is_catalog_visible", False)
    except Exception as exc:
        logger.warning("Health check: commerce settings fetch failed: %s", exc)

    if cart_enabled:
        _add("cart_enabled", "Shopping Cart Enabled", "pass", "Shopping cart is enabled for customers.")
    else:
        _add("cart_enabled", "Shopping Cart Enabled", "warning",
             "Shopping cart is disabled. Customers cannot add items to cart.",
             "Toggle 'Shopping Cart' ON in Storefront Settings.")

    if catalog_visible:
        _add("catalog_visible", "Storefront Visible on Profile", "pass",
             "The catalog storefront icon is visible on your WhatsApp profile.")
    else:
        _add("catalog_visible", "Storefront Visible on Profile", "fail",
             "The catalog storefront icon is NOT visible on your WhatsApp business profile.",
             "Toggle 'Catalog Storefront Icon' ON in Storefront Settings. It may take 5-10 minutes to appear.")

    # ── Check 7 & 8: Products ────────────────────────────────────────────
    primary_catalog = connected_catalogs[0] if connected_catalogs else None
    products_list = []
    if primary_catalog:
        try:
            url = f"{GRAPH_API_BASE}/{api_version}/{primary_catalog['id']}/products"
            fields = "id,name,description,price,currency,image_url,availability,retailer_id,url"
            resp = http_requests.get(
                url, params={"fields": fields, "limit": 100},
                headers={"Authorization": f"Bearer {token}"}, timeout=15,
            )
            if resp.ok:
                products_list = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("Health check: products fetch failed: %s", exc)

    if not primary_catalog:
        _add("products_exist", "Products in Catalog", "fail",
             "No linked catalog to check products.",
             "Link a catalog first, then add products.")
        _add("product_compliance", "Product Data Quality", "fail",
             "Cannot check product quality without a linked catalog.",
             "Link a catalog and add products.")
    elif len(products_list) == 0:
        _add("products_exist", "Products in Catalog", "fail",
             f"Catalog '{primary_catalog.get('name', primary_catalog['id'])}' has no products.",
             "Add at least one product using the 'Add Product' button in Live Inventory.")
        _add("product_compliance", "Product Data Quality", "fail",
             "No products to verify.", "Add products first.")
    else:
        _add("products_exist", "Products in Catalog", "pass",
             f"{len(products_list)} product(s) found in '{primary_catalog.get('name', primary_catalog['id'])}'.")

        # Compliance check
        required_fields = ["name", "price", "image_url"]
        issues = []
        for p in products_list:
            missing = [f for f in required_fields if not p.get(f)]
            if missing:
                issues.append(f"{p.get('name', p['id'])}: missing {', '.join(missing)}")

        if issues:
            _add("product_compliance", "Product Data Quality", "warning",
                 f"{len(issues)} product(s) have incomplete data: {'; '.join(issues[:3])}{'...' if len(issues) > 3 else ''}",
                 "Ensure all products have name, price, and image_url for Meta approval.")
        else:
            _add("product_compliance", "Product Data Quality", "pass",
                 "All products have required fields (name, price, image).")

    # ── Compute Score ────────────────────────────────────────────────────
    weights = {
        "waba_connected": 20, "business_id": 10, "catalog_exists": 10,
        "catalog_linked": 20, "cart_enabled": 5, "catalog_visible": 15,
        "products_exist": 10, "product_compliance": 10,
    }
    total = 0
    for c in checks:
        w = weights.get(c["id"], 0)
        if c["status"] == "pass":
            total += w
        elif c["status"] == "warning":
            total += w * 0.5

    if total >= 80:
        overall = "ready"
    elif total >= 50:
        overall = "needs_attention"
    else:
        overall = "setup_required"

    return jsonify({
        "success": True, 
        "score": round(total, 1), 
        "max_score": 100,
        "checks": checks, 
        "overall_status": overall,
        "waba_id": account.waba_id,
        "display_phone_number": account.display_phone_number,
        "verified_name": account.verified_name or account.custom_name or "WhatsApp Business Account",
        "meta_business_id": account.meta_business_id,
        "account_id": account.id
    })
