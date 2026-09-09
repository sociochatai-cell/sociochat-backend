"""Service to extract and persist AI conversation insights.

Called AFTER the AI agent responds — never in the critical message path.
All errors are swallowed so the chat flow is never affected."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Friendly labels for tool actions shown to the workspace owner.
_ACTION_LABELS = {
    "capture_lead": "Captured lead",
    "book_appointment": "Booked appointment",
    "request_payment": "Sent payment link",
    "escalate_to_human": "Escalated to human",
    "send_template": "Sent template",
    "send_products": "Sent product cards",
    "search_knowledge_base": "Searched knowledge base",
    "send_buttons": "Sent reply buttons",
    "list_products": "Listed products",
    "list_templates": "Listed templates",
    "get_business_info": "Looked up business info",
}


def record_insights(
    *,
    workspace_id: int,
    conversation_id: int,
    customer_phone: Optional[str] = None,
    tool_log: Optional[List[Dict[str, Any]]] = None,
    ai_reply_text: Optional[str] = None,
) -> None:
    """Extract insights from the agent's tool log and persist them.

    This is fire-and-forget — any exception is logged and suppressed so
    the main chat pipeline is never affected.
    """
    try:
        _record_insights_inner(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            customer_phone=customer_phone,
            tool_log=tool_log or [],
            ai_reply_text=ai_reply_text,
        )
    except Exception as exc:
        logger.warning("[insights] failed to record for conv=%s: %s", conversation_id, exc)


def _record_insights_inner(
    *,
    workspace_id: int,
    conversation_id: int,
    customer_phone: Optional[str],
    tool_log: List[Dict[str, Any]],
    ai_reply_text: Optional[str],
) -> None:
    from app import db
    from .conversation_insights_models import ConversationInsight

    # Build structured data from tool calls
    actions_taken = []
    extracted_name = None
    extracted_email = None
    extracted_company = None
    extracted_interests = []
    key_topics = []

    for entry in tool_log:
        tool_name = entry.get("name", "")
        args = entry.get("args") or {}
        result = entry.get("result") or {}
        ok = result.get("ok", result.get("success", False))

        action = {
            "tool": tool_name,
            "label": _ACTION_LABELS.get(tool_name, tool_name),
            "ok": bool(ok),
            "timestamp": entry.get("timestamp"),
        }

        if tool_name == "capture_lead":
            extracted_name = args.get("name") or extracted_name
            extracted_email = args.get("email") or extracted_email
            extracted_company = args.get("company") or extracted_company
            interest = args.get("interest")
            if interest:
                extracted_interests.append(interest)
                action["detail"] = interest
        elif tool_name == "book_appointment":
            detail = f"{args.get('date', '?')} at {args.get('time', '?')}"
            svc = args.get("service_type")
            if svc:
                detail += f" — {svc}"
            action["detail"] = detail
            key_topics.append(f"Appointment: {detail}")
        elif tool_name == "request_payment":
            amt = args.get("amount")
            desc = args.get("description")
            action["detail"] = f"₹{amt}" if amt else (desc or "payment")
            key_topics.append(f"Payment: {action['detail']}")
        elif tool_name == "escalate_to_human":
            action["detail"] = args.get("reason", "")
            key_topics.append(f"Escalated: {action['detail']}")
        elif tool_name == "send_template":
            action["detail"] = args.get("template_name", "")
        elif tool_name == "send_products":
            product_ids = args.get("product_ids") or args.get("product_retailer_ids") or []
            if product_ids:
                action["detail"] = f"{len(product_ids)} product(s)"
                key_topics.append("Product inquiry")
        elif tool_name == "search_knowledge_base":
            q = args.get("query", "")
            if q:
                key_topics.append(q)

        actions_taken.append(action)

    # Build summary from the collected data
    summary_parts = []
    if extracted_name:
        summary_parts.append(f"Customer: {extracted_name}")
    if extracted_interests:
        summary_parts.append(f"Interested in: {', '.join(extracted_interests)}")
    if any(a["tool"] == "book_appointment" and a["ok"] for a in actions_taken):
        summary_parts.append("Appointment booked")
    if any(a["tool"] == "capture_lead" and a["ok"] for a in actions_taken):
        summary_parts.append("Lead captured")
    if any(a["tool"] == "request_payment" and a["ok"] for a in actions_taken):
        summary_parts.append("Payment requested")
    if any(a["tool"] == "escalate_to_human" for a in actions_taken):
        summary_parts.append("Escalated to human agent")

    summary = ". ".join(summary_parts) if summary_parts else None

    # Deduplicate key_topics
    seen = set()
    unique_topics = []
    for t in key_topics:
        low = t.lower().strip()
        if low and low not in seen:
            seen.add(low)
            unique_topics.append(t)

    # Upsert — update existing row for this conversation or create new
    existing = ConversationInsight.query.filter_by(
        conversation_id=conversation_id, workspace_id=workspace_id
    ).first()

    if existing:
        if extracted_name:
            existing.customer_name = extracted_name
        if extracted_email:
            existing.customer_email = extracted_email
        if extracted_company:
            existing.customer_company = extracted_company
        if customer_phone:
            existing.customer_phone = customer_phone
        if extracted_interests:
            old = existing.interests or ""
            new_interests = ", ".join(extracted_interests)
            existing.interests = f"{old}, {new_interests}".strip(", ") if old else new_interests
        if summary:
            existing.summary = summary
        if actions_taken:
            old_actions = existing.actions_taken or []
            existing.actions_taken = old_actions + actions_taken
        if tool_log:
            old_log = existing.tool_calls or []
            existing.tool_calls = old_log + [
                {"name": e.get("name"), "args": e.get("args"), "ok": (e.get("result") or {}).get("ok", False)}
                for e in tool_log
            ]
        if unique_topics:
            old_topics = existing.key_topics or ""
            new_topics = "; ".join(unique_topics)
            existing.key_topics = f"{old_topics}; {new_topics}".strip("; ") if old_topics else new_topics
        existing.interaction_count = (existing.interaction_count or 0) + 1
        existing.updated_at = datetime.now(timezone.utc)
    else:
        insight = ConversationInsight(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            customer_name=extracted_name,
            customer_email=extracted_email,
            customer_phone=customer_phone,
            customer_company=extracted_company,
            interests=", ".join(extracted_interests) if extracted_interests else None,
            summary=summary,
            actions_taken=actions_taken if actions_taken else None,
            tool_calls=[
                {"name": e.get("name"), "args": e.get("args"), "ok": (e.get("result") or {}).get("ok", False)}
                for e in tool_log
            ] if tool_log else None,
            key_topics="; ".join(unique_topics) if unique_topics else None,
            interaction_count=1,
        )
        db.session.add(insight)

    db.session.commit()
    logger.info("[insights] recorded for conv=%s ws=%s actions=%s", conversation_id, workspace_id, len(actions_taken))
