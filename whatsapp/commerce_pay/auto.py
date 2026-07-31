# whatsapp/commerce_pay/auto.py
"""
Auto-send a payment link when a catalog order arrives (SocioChat-only, removable).

Called from the inbound-order webhook path via ONE guarded line. If the
workspace (or this chat) has auto-payment enabled and PayU connected, it creates
the order and sends the PayU link immediately — no owner click. Fully defensive:
any error is swallowed so it can never break order processing.
"""

import logging

logger = logging.getLogger(__name__)


def _cart_total(order_content) -> tuple:
    items = []
    if isinstance(order_content, dict):
        items = order_content.get("product_items") or order_content.get("items") or []
    total = 0.0
    for it in items:
        try:
            total += float(it.get("item_price") or 0) * int(it.get("quantity") or 1)
        except (TypeError, ValueError):
            continue
    return round(total, 2), len(items)


def maybe_auto_request_payment(*, account, conversation, message_id, order_content, host_url=""):
    """Best-effort: auto-create + send a payment link for a received order."""
    try:
        from .models import WorkspacePaymentConfig
        from . import orders as orders_engine

        if not account or getattr(account, "workspace_id", None) is None:
            return
        try:
            ws_id = int(account.workspace_id)
        except (TypeError, ValueError):
            return

        cfg_row = WorkspacePaymentConfig.query.filter_by(workspace_id=ws_id).first()
        if not cfg_row or not cfg_row.is_connected:
            return

        conv_id = getattr(conversation, "id", None)
        if not orders_engine.resolve_auto_mode(cfg_row, conv_id):
            return

        # Dedup — one auto order per inbound WhatsApp order message.
        if orders_engine.already_charged_for_message(message_id):
            return

        total, n_items = _cart_total(order_content)
        if total <= 0:
            logger.info("auto-pay skipped: cart total is 0 (msg=%s)", message_id)
            return

        productinfo = f"Order — {n_items} item(s)" if n_items else "Order"
        phone = getattr(conversation, "user_phone", None)
        if not phone:
            return

        order, link, sent = orders_engine.create_order_and_send(
            workspace_id=ws_id, cfg_row=cfg_row, phone=phone, amount=total,
            productinfo=productinfo, host_url=host_url, conversation_id=conv_id,
            customer_name=getattr(conversation, "user_name", None),
            source_message_id=message_id, origin="auto",
        )
        logger.info("auto-pay link sent=%s txnid=%s total=%s link=%s msg=%s",
                    sent, order.txnid, total, link, message_id)
    except Exception:
        logger.exception("maybe_auto_request_payment failed (msg=%s)", message_id)
