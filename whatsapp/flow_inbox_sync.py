"""
Merge WhatsApp Flow encrypted-endpoint COMPLETE payloads into inbox messages.

When a flow uses a Meta data endpoint, form answers often arrive only in the
encrypted COMPLETE request — not in the `messages` webhook `response_json`
(which may only contain `{"flow_token":"unused"}`).

We attach that payload to the most recent incoming `nfm_reply` for the same
account within a short window (and optionally matching `flow_token`).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified

from shared_models import db

from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage

logger = logging.getLogger(__name__)

_MERGE_WINDOW = timedelta(minutes=20)

_META_ROOT = frozenset(
    {"version", "action", "flow_token", "screen", "error", "errors", "data"}
)
_INNER_WRAPPERS = frozenset(
    {"response", "fields", "answers", "form", "form_response", "submission", "payload", "values"}
)


def _flatten_flow_submission(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize Meta COMPLETE `data` into a flat dict suitable for merging into
    `response_json` (nested objects become dotted keys).
    """
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, Any] = {}
    skip = frozenset({"version", "action", "flow_token", "screen", "error", "errors"})
    for key, val in raw.items():
        if key in skip or key.startswith("__"):
            continue
        if isinstance(val, dict):
            for sk, sv in val.items():
                if sv in (None, "", [], {}):
                    continue
                nk = f"{key}.{sk}" if key else str(sk)
                out[nk] = sv
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, dict):
                    for sk, sv in item.items():
                        if sv in (None, "", [], {}):
                            continue
                        nk = f"{key}.{i}.{sk}"
                        out[nk] = sv
                elif item not in (None, "", [], {}):
                    out[f"{key}.{i}"] = item
        elif val not in (None, "", [], {}):
            out[key] = val
    return out


def _collect_complete_submission(decrypted_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a flat field map from a decrypted Flow COMPLETE body.

    Meta usually puts answers in ``data``, but we also unwrap common inner keys
    and fall back to non-metadata keys on the root when ``data`` is empty.
    """
    out: Dict[str, Any] = {}
    raw = decrypted_data.get("data")

    if isinstance(raw, dict):
        out.update(_flatten_flow_submission(raw))
        for wrap in _INNER_WRAPPERS:
            inner = raw.get(wrap)
            if isinstance(inner, dict):
                out.update(_flatten_flow_submission(inner))
            elif isinstance(inner, list):
                out.update(_flatten_flow_submission({wrap: inner}))

    if isinstance(raw, list):
        out.update(_flatten_flow_submission({"items": raw}))

    if out:
        return out

    for key, val in decrypted_data.items():
        if key in _META_ROOT or str(key).startswith("__"):
            continue
        if isinstance(val, dict):
            out.update(_flatten_flow_submission(val))
        elif isinstance(val, list):
            out.update(_flatten_flow_submission({str(key): val}))
        elif val not in (None, "", [], {}):
            out[str(key)] = val

    return out


def merge_flow_endpoint_completion_into_latest_message(
    account: WhatsAppAccount,
    decrypted_data: Dict[str, Any],
) -> bool:
    """
    Merge `data` from a Flow COMPLETE decrypted payload into the latest
    matching interactive (nfm_reply) message for this account.

    Returns True if a row was updated.
    """
    action = str(decrypted_data.get("action") or "").strip().upper()
    if action != "COMPLETE":
        return False

    submission = _collect_complete_submission(decrypted_data)
    if not submission:
        raw_dbg = decrypted_data.get("data")
        logger.info(
            "[flow_inbox_sync] COMPLETE: empty submission after extraction; decrypted_keys=%s data_type=%s",
            list(decrypted_data.keys()),
            type(raw_dbg).__name__,
        )
        return False

    flow_token = str(decrypted_data.get("flow_token") or "").strip()
    cutoff = datetime.now(timezone.utc) - _MERGE_WINDOW

    # Match conversations on any active account under this WABA (webhook account row
    # may differ from the row resolved for flow keys / endpoint).
    waba_id = (account.waba_id or "").strip()
    if not waba_id:
        logger.warning("[flow_inbox_sync] COMPLETE: account id=%s has no waba_id", account.id)
        return False

    candidates = (
        WhatsAppMessage.query.join(
            WhatsAppConversation,
            WhatsAppMessage.conversation_id == WhatsAppConversation.id,
        )
        .join(WhatsAppAccount, WhatsAppConversation.account_id == WhatsAppAccount.id)
        .filter(
            WhatsAppAccount.waba_id == waba_id,
            WhatsAppAccount.is_active == True,  # noqa: E712
            WhatsAppMessage.direction == "incoming",
            WhatsAppMessage.type == "interactive",
            WhatsAppMessage.created_at >= cutoff,
        )
        .order_by(WhatsAppMessage.created_at.desc())
        .limit(40)
        .all()
    )

    for msg in candidates:
        content = msg.content if isinstance(msg.content, dict) else {}
        if content.get("interactive_type") != "nfm_reply":
            continue
        if content.get("endpoint_flow_data_merged"):
            continue

        existing_token = content.get("flow_token")
        if not existing_token and isinstance(content.get("response_json"), dict):
            existing_token = content["response_json"].get("flow_token")

        if (
            flow_token
            and flow_token != "unused"
            and existing_token
            and existing_token != "unused"
            and existing_token != flow_token
        ):
            continue

        merged_response = dict(content.get("response_json") or {})
        for key, val in submission.items():
            if val is None or val == "":
                continue
            merged_response[key] = val

        content["response_json"] = merged_response
        content["endpoint_flow_data_merged"] = True
        content["endpoint_flow_completed_at"] = datetime.now(timezone.utc).isoformat()

        try:
            from .flow_field_labels import attach_flow_field_labels_to_nfm_content

            conv_aid = None
            try:
                conv_aid = msg.conversation.account_id if msg.conversation else None
            except Exception:
                conv_aid = None
            attach_flow_field_labels_to_nfm_content(content, int(conv_aid or account.id))
        except Exception:
            logger.exception("[flow_inbox_sync] attach_flow_field_labels_to_nfm_content failed")

        msg.content = content
        flag_modified(msg, "content")
        db.session.commit()

        # Trigger email notification for Flow Form Submission
        try:
            from .human_escalation import send_flow_submission_notification_email_bg
            from .background_processor import bg_processor
            bg_processor.submit(
                send_flow_submission_notification_email_bg,
                account_id=account.id,
                message_id=msg.id,
                submission=submission,
            )
            logger.info(f"Scheduled flow submission email notification for message {msg.id}")
        except Exception as email_err:
            logger.exception(f"Failed to submit background task to send flow submission email: {email_err}")

        conv_aid = None
        try:
            conv_aid = msg.conversation.account_id if msg.conversation else None
        except Exception:
            pass
        logger.info(
            "[flow_inbox_sync] Merged Flow COMPLETE into message id=%s conv=%s conv_account_id=%s keys=%s",
            msg.id,
            msg.conversation_id,
            conv_aid,
            list(submission.keys()),
        )
        broadcast_updated_message(account, msg)
        return True

    logger.warning(
        "[flow_inbox_sync] No matching nfm_reply to merge for waba_id=%s flow_token=%r (candidates=%s)",
        waba_id,
        flow_token,
        len(candidates),
    )
    return False


def broadcast_updated_message(account: WhatsAppAccount, message: WhatsAppMessage) -> None:
    """Notify inbox SSE clients that message content changed."""
    try:
        from notifications import notification_manager

        workspace_id = account.workspace_id if account else None
        if workspace_id is not None:
            workspace_id = str(workspace_id)
        notification_manager.broadcast(
            "whatsapp_message_received",
            {
                "message": message.to_dict(),
                "conversation_id": message.conversation_id,
                "account_id": account.id,
                "workspace_id": workspace_id,
            },
        )
    except Exception as exc:
        logger.warning("[flow_inbox_sync] broadcast skipped: %s", exc)
