# whatsapp/commerce_pay/orders.py
"""
Shared order engine for in-chat payments (SocioChat-only, removable).

One place that creates a CommerceOrder, builds the PayU link, and sends it into
the chat — reused by BOTH the manual "Request Payment" endpoint and the
auto-on-order-received automation. Also resolves the effective auto mode
(per-chat override → workspace default).
"""

import logging
import uuid

from models import db
from . import payu_link
from .models import CommerceOrder, WorkspacePaymentConfig, CommerceChatOverride

logger = logging.getLogger(__name__)


def resolve_auto_mode(cfg_row: "WorkspacePaymentConfig", conversation_id) -> bool:
    """Effective auto-send decision: per-chat override wins, else workspace default."""
    if conversation_id:
        ov = CommerceChatOverride.query.filter_by(conversation_id=int(conversation_id)).first()
        if ov:
            return ov.mode == "on"
    return bool(cfg_row and cfg_row.auto_request_payment)


def already_charged_for_message(source_message_id) -> bool:
    """Dedup guard for auto mode — one order per inbound WhatsApp order message."""
    if not source_message_id:
        return False
    return db.session.query(
        CommerceOrder.query.filter_by(source_message_id=str(source_message_id)).exists()
    ).scalar()


def create_order_and_send(
    *, workspace_id, cfg_row, phone, amount, productinfo, host_url,
    conversation_id=None, customer_name=None, customer_email=None,
    source_message_id=None, origin="manual",
):
    """Create a pending order, send the PayU link into the chat.

    Returns ``(order, pay_link, sent_bool)``. Raises on DB failure.
    """
    txnid = f"SC{workspace_id}{uuid.uuid4().hex[:12]}"
    order = CommerceOrder(
        workspace_id=int(workspace_id),
        txnid=txnid,
        customer_phone=phone,
        customer_name=(customer_name or None),
        customer_email=(customer_email or None),
        conversation_id=int(conversation_id) if conversation_id else None,
        amount=round(float(amount), 2),
        currency="INR",
        productinfo=(productinfo or "Order")[:160],
        status="pending",
        mode=cfg_row.mode,
        source_message_id=str(source_message_id) if source_message_id else None,
        origin=origin,
    )
    db.session.add(order)
    db.session.commit()

    link = payu_link.pay_link(host_url, txnid)
    msg = (
        f"🧾 *Payment request*\n"
        f"{order.productinfo} — ₹{order.amount_str}\n\n"
        f"Pay securely here:\n{link}"
    )
    sent = payu_link.send_chat_text(workspace_id, phone, msg, conversation_id=order.conversation_id)
    return order, link, sent


def orders_summary(workspace_id, status=None, limit=200):
    """List orders (newest first) + a rollup summary for the dashboard."""
    q = CommerceOrder.query.filter_by(workspace_id=int(workspace_id))
    if status in ("pending", "paid", "failed"):
        q = q.filter_by(status=status)
    rows = q.order_by(CommerceOrder.created_at.desc()).limit(int(limit)).all()

    all_rows = CommerceOrder.query.filter_by(workspace_id=int(workspace_id)).all()
    total_collected = sum(float(r.amount or 0) for r in all_rows if r.status == "paid")
    summary = {
        "total_collected": round(total_collected, 2),
        "currency": "INR",
        "paid_count": sum(1 for r in all_rows if r.status == "paid"),
        "pending_count": sum(1 for r in all_rows if r.status == "pending"),
        "failed_count": sum(1 for r in all_rows if r.status == "failed"),
        "total_count": len(all_rows),
    }
    return [r.serialize() for r in rows], summary
