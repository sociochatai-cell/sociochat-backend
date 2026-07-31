# whatsapp/commerce_pay/routes.py
"""
Owner-facing PayU payment-config endpoints (per workspace/business).

GATED to the internal SocioChat tenant (T0000) for now, and to the OWNER
(not agents). Each business connects its own PayU merchant credentials here.
"""

import logging
import uuid
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, current_app, Response

from models import db, Workspace
from tenant.context import get_current_user, resolve_owned_workspace
from tenant.branding import INTERNAL_TENANT_CODE
from .models import WorkspacePaymentConfig, CommerceOrder, CommerceChatOverride
from . import payu_link, orders as orders_engine

logger = logging.getLogger(__name__)

commerce_pay_bp = Blueprint("commerce_pay", __name__, url_prefix="/api/whatsapp/commerce")


def _is_internal_tenant(user) -> bool:
    """True only for users on the internal SocioChat tenant (T0000).

    Feature is intentionally scoped to SocioChat for now. A user with no tenant
    is treated as internal (platform default); any real white-label tenant
    (different tenant_code) is excluded."""
    if not user:
        return False
    if getattr(user, "tenant_id", None) is None:
        return True
    try:
        from tenant.models import Tenant
        t = db.session.get(Tenant, int(user.tenant_id))
        return bool(t and t.tenant_code == INTERNAL_TENANT_CODE)
    except Exception:
        return False


def _reject_agents():
    """Payment config is owner-only — block agent sub-logins outright."""
    from auth_core import authenticated_agent_id
    if authenticated_agent_id() is not None:
        return jsonify({"success": False, "error": "owner_only",
                        "message": "Agents cannot manage payment settings."}), 403
    return None


def _owned_workspace():
    """(workspace, err_response). Resolves + verifies ownership of the requested
    workspace (X-Workspace-ID / ?workspace_id / body)."""
    user = get_current_user()
    if not user:
        return None, None, (jsonify({"success": False, "error": "authentication_required"}), 401)
    wid = (request.headers.get("X-Workspace-ID") or request.args.get("workspace_id")
           or (request.get_json(silent=True) or {}).get("workspace_id"))
    ws, err = resolve_owned_workspace(user, wid)
    if err:
        return user, None, err
    return user, ws, None


@commerce_pay_bp.route("/payment-config", methods=["GET"])
def get_payment_config():
    agent_block = _reject_agents()
    if agent_block:
        return agent_block
    user, ws, err = _owned_workspace()
    if err:
        return err
    available = _is_internal_tenant(user)
    if not available:
        # Feature not offered to this tenant — tell the UI so it hides the tab.
        return jsonify({"success": True, "available": False})
    cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
    return jsonify({
        "success": True,
        "available": True,
        "config": cfg.serialize() if cfg else {"provider": "payu", "mode": "test", "connected": False},
    })


@commerce_pay_bp.route("/payment-config", methods=["POST"])
def save_payment_config():
    agent_block = _reject_agents()
    if agent_block:
        return agent_block
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": False, "error": "not_available",
                        "message": "Payments are not enabled for this account."}), 403

    data = request.get_json(silent=True) or {}
    merchant_key = (data.get("merchant_key") or "").strip()
    merchant_salt = (data.get("merchant_salt") or "").strip()
    mode = (data.get("mode") or "test").strip().lower()

    errors = []
    if not merchant_key:
        errors.append("PayU Merchant Key is required")
    if not merchant_salt:
        errors.append("PayU Merchant Salt is required")
    if mode not in ("test", "live"):
        errors.append("Mode must be 'test' or 'live'")
    if errors:
        return jsonify({"success": False, "error": "validation_error",
                        "message": "; ".join(errors), "errors": errors}), 400

    try:
        cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
        if not cfg:
            cfg = WorkspacePaymentConfig(workspace_id=ws.id, provider="payu")
            db.session.add(cfg)
        cfg.provider = "payu"
        cfg.merchant_key = merchant_key
        cfg.set_salt(merchant_salt)
        cfg.mode = mode
        cfg.is_active = True
        db.session.commit()
        return jsonify({"success": True, "config": cfg.serialize(),
                        "message": "PayU credentials saved"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("save_payment_config error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@commerce_pay_bp.route("/payment-config", methods=["DELETE"])
def delete_payment_config():
    agent_block = _reject_agents()
    if agent_block:
        return agent_block
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": False, "error": "not_available"}), 403
    try:
        cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
        if cfg:
            db.session.delete(cfg)
            db.session.commit()
        return jsonify({"success": True, "message": "Payment config removed"})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("delete_payment_config error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


# ============================================================================
# Payment flow: Request Payment -> PayU link -> checkout -> callback
# ============================================================================

def _normalize_phone(p: str) -> str:
    return "".join(ch for ch in str(p or "") if ch.isdigit())


@commerce_pay_bp.route("/request-payment", methods=["POST"])
def request_payment():
    """Owner/agent action from the inbox: create an order + send a PayU link
    into the customer's chat. Amount is confirmed by the sender (dialog)."""
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": False, "error": "not_available",
                        "message": "Payments are not enabled for this account."}), 403

    cfg_row = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
    cfg = payu_link.cfg_from_row(cfg_row)
    if not cfg:
        return jsonify({"success": False, "error": "not_connected",
                        "message": "Connect PayU in Settings → Payments first."}), 400

    data = request.get_json(silent=True) or {}
    phone = _normalize_phone(data.get("phone"))
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        amount = 0.0
    productinfo = (data.get("description") or data.get("productinfo") or "Order").strip()[:160]
    customer_name = (data.get("customer_name") or "").strip()[:120] or None
    customer_email = (data.get("customer_email") or "").strip()[:160] or None
    conversation_id = data.get("conversation_id")

    if not phone:
        return jsonify({"success": False, "error": "validation_error",
                        "message": "Customer phone is required."}), 400
    if amount <= 0:
        return jsonify({"success": False, "error": "validation_error",
                        "message": "Amount must be greater than 0."}), 400

    try:
        order, link, sent = orders_engine.create_order_and_send(
            workspace_id=ws.id, cfg_row=cfg_row, phone=phone, amount=amount,
            productinfo=productinfo or "Order", host_url=request.host_url,
            conversation_id=conversation_id, customer_name=customer_name,
            customer_email=customer_email, origin="manual",
        )
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("request_payment create order error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500

    return jsonify({
        "success": True,
        "order": order.serialize(),
        "pay_link": link,
        "message_sent": sent,
        "message": "Payment link sent to the customer." if sent
                   else "Order created, but sending the chat message failed — share the link manually.",
    })


@commerce_pay_bp.route("/pay/<txnid>", methods=["GET"])
def pay_redirect(txnid):
    """PUBLIC. Customer opens this link → auto-submit to PayU checkout."""
    order = CommerceOrder.query.filter_by(txnid=txnid).first()
    if not order:
        return Response(payu_link._thankyou("Link not found",
                        "This payment link is invalid or has expired.", False),
                        mimetype="text/html", status=404)
    if order.status == "paid":
        return Response(payu_link._thankyou("Already paid",
                        "This order has already been paid. Thank you!", True),
                        mimetype="text/html")

    cfg_row = WorkspacePaymentConfig.query.filter_by(workspace_id=order.workspace_id).first()
    cfg = payu_link.cfg_from_row(cfg_row)
    if not cfg:
        return Response(payu_link._thankyou("Payment unavailable",
                        "The seller's payment account is not configured.", False),
                        mimetype="text/html", status=400)

    try:
        checkout = payu_link.build_checkout(cfg, order, request.host_url)
        return Response(payu_link.render_autosubmit(checkout), mimetype="text/html")
    except Exception as e:
        current_app.logger.exception("pay_redirect error for %s: %s", txnid, e)
        return Response(payu_link._thankyou("Something went wrong",
                        "We couldn't start the payment. Please try again.", False),
                        mimetype="text/html", status=500)


@commerce_pay_bp.route("/payu/callback", methods=["POST", "GET"])
def payu_callback():
    """PUBLIC. PayU posts the customer's browser here (surl/furl). Verify the
    reverse hash, mark the order, and confirm in chat."""
    posted = request.form.to_dict() if request.form else request.args.to_dict()
    txnid = posted.get("txnid", "")
    order = CommerceOrder.query.filter_by(txnid=txnid).first()
    if not order:
        return Response(payu_link._thankyou("Unknown payment",
                        "We could not match this payment to an order.", False),
                        mimetype="text/html", status=404)

    cfg_row = WorkspacePaymentConfig.query.filter_by(workspace_id=order.workspace_id).first()
    cfg = payu_link.cfg_from_row(cfg_row)
    if not cfg:
        return Response(payu_link._thankyou("Payment error",
                        "Seller payment configuration is missing.", False),
                        mimetype="text/html", status=400)

    from payments import payu as payu_core
    if not payu_core.verify_response_hash(cfg, posted):
        current_app.logger.warning("PayU callback hash mismatch for txnid=%s", txnid)
        return Response(payu_link._thankyou("Verification failed",
                        "We could not verify this payment. If money was debited, contact the seller.", False),
                        mimetype="text/html", status=400)

    status = (posted.get("status") or "").strip().lower()
    already_paid = order.status == "paid"

    if status == "success":
        if not already_paid:
            order.status = "paid"
            order.payu_mihpayid = posted.get("mihpayid") or posted.get("payuMoneyId")
            order.paid_at = datetime.now(timezone.utc)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                current_app.logger.exception("payu_callback commit failed for %s", txnid)
            payu_link.send_chat_text(
                order.workspace_id, order.customer_phone,
                f"✅ *Payment received* — ₹{order.amount_str} for {order.productinfo}. Thank you!",
                conversation_id=order.conversation_id,
            )
        return Response(payu_link._thankyou("Payment successful",
                        f"₹{order.amount_str} paid for {order.productinfo}.", True),
                        mimetype="text/html")

    # failure / cancelled
    if not already_paid:
        order.status = "failed"
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return Response(payu_link._thankyou("Payment not completed",
                    "The payment was not completed. You can try the link again.", False),
                    mimetype="text/html")


# ============================================================================
# Preferences (auto-send + notification emails), Orders list, per-chat override
# ============================================================================

def _valid_emails(raw):
    """Normalize a list/str of emails → cleaned comma-joined string (or None)."""
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or "").replace(";", ",").split(",")
    out = []
    for e in items:
        e = (e or "").strip()
        if e and "@" in e and "." in e.split("@")[-1] and e not in out:
            out.append(e[:160])
    return ", ".join(out) if out else None


@commerce_pay_bp.route("/payment-config/preferences", methods=["PUT"])
def save_preferences():
    """Update auto-send + notification emails WITHOUT re-entering the salt."""
    agent_block = _reject_agents()
    if agent_block:
        return agent_block
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": False, "error": "not_available"}), 403

    cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
    if not cfg:
        return jsonify({"success": False, "error": "not_connected",
                        "message": "Connect PayU first, then set preferences."}), 400

    data = request.get_json(silent=True) or {}
    if "auto_request_payment" in data:
        cfg.auto_request_payment = bool(data.get("auto_request_payment"))
    if "notify_emails" in data:
        cfg.notify_emails = _valid_emails(data.get("notify_emails"))
    try:
        db.session.commit()
        return jsonify({"success": True, "config": cfg.serialize()})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("save_preferences error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500


@commerce_pay_bp.route("/orders", methods=["GET"])
def list_orders():
    """Orders/payment statement for the dashboard (owner-only)."""
    agent_block = _reject_agents()
    if agent_block:
        return agent_block
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": True, "available": False, "orders": [], "summary": {}})
    status = request.args.get("status")
    try:
        limit = min(int(request.args.get("limit", 200)), 500)
    except (TypeError, ValueError):
        limit = 200
    rows, summary = orders_engine.orders_summary(ws.id, status=status, limit=limit)
    return jsonify({"success": True, "available": True, "orders": rows, "summary": summary})


@commerce_pay_bp.route("/chat-auto/<int:conversation_id>", methods=["GET"])
def get_chat_auto(conversation_id):
    """Effective auto mode for one chat: 'default' | 'on' | 'off' + resolved bool."""
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": True, "available": False})
    cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
    ov = CommerceChatOverride.query.filter_by(conversation_id=conversation_id).first()
    mode = ov.mode if ov else "default"
    effective = orders_engine.resolve_auto_mode(cfg, conversation_id)
    return jsonify({
        "success": True, "available": True,
        "mode": mode, "effective": effective,
        "workspace_default": bool(cfg and cfg.auto_request_payment),
        "connected": bool(cfg and cfg.is_connected),
    })


@commerce_pay_bp.route("/chat-auto/<int:conversation_id>", methods=["PUT"])
def set_chat_auto(conversation_id):
    """Set per-chat override: mode in {'default','on','off'} (default removes the row)."""
    user, ws, err = _owned_workspace()
    if err:
        return err
    if not _is_internal_tenant(user):
        return jsonify({"success": False, "error": "not_available"}), 403
    mode = (request.get_json(silent=True) or {}).get("mode", "default")
    if mode not in ("default", "on", "off"):
        return jsonify({"success": False, "error": "validation_error",
                        "message": "mode must be default|on|off"}), 400
    try:
        ov = CommerceChatOverride.query.filter_by(conversation_id=conversation_id).first()
        if mode == "default":
            if ov:
                db.session.delete(ov)
        else:
            if not ov:
                ov = CommerceChatOverride(workspace_id=ws.id, conversation_id=conversation_id)
                db.session.add(ov)
            ov.mode = mode
        db.session.commit()
        cfg = WorkspacePaymentConfig.query.filter_by(workspace_id=ws.id).first()
        return jsonify({"success": True, "mode": mode,
                        "effective": orders_engine.resolve_auto_mode(cfg, conversation_id)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception("set_chat_auto error: %s", e)
        return jsonify({"success": False, "error": "server_error"}), 500
