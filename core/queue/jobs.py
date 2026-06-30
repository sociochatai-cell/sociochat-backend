"""
WhatsApp-Only Job Definitions
==============================

Trimmed from monolith — contains ONLY WhatsApp-related background jobs.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Dict, Mapping


logger = logging.getLogger("wa.queue.jobs")


JobPayload = Mapping[str, Any] | None
JobHandler = Callable[[JobPayload], Mapping[str, Any] | None]


@dataclass(frozen=True)
class JobDefinition:
    name: str
    schedule: str
    path: str
    description: str
    handler: JobHandler


def _run_with_app_context(callback: Callable[[Any], Mapping[str, Any] | None]) -> Mapping[str, Any] | None:
    from runtime import app as runtime_app, prepare_worker_runtime

    try:
        prepare_worker_runtime()
    except Exception as exc:
        logger.debug("Worker runtime DB bootstrap skipped: %s", exc)

    with runtime_app.app_context():
        return callback(runtime_app)


# ── WhatsApp Drip Campaigns ──────────────────────────────────────────

def _job_whatsapp_drip(_: JobPayload) -> Mapping[str, Any] | None:
    def _run(_flask_app):
        from whatsapp.drip_engine import process_drip_campaigns

        process_drip_campaigns()
        return None

    return _run_with_app_context(_run)


def _job_whatsapp_drip_trigger_campaign(payload: JobPayload) -> Mapping[str, Any] | None:
    """Run trigger_campaign_now for one campaign (used after scheduler SKIP LOCKED claim)."""

    def _run(_flask_app):
        from whatsapp.drip_engine import trigger_campaign_now

        payload_dict = dict(payload or {})
        campaign_id = payload_dict.get("campaign_id")
        if not campaign_id:
            logger.warning("[job] whatsapp-drip-trigger-campaign: missing campaign_id")
            return {"error": "missing campaign_id"}
        trigger_campaign_now(int(campaign_id))
        return {"campaign_id": campaign_id}

    return _run_with_app_context(_run)


# ── Send Notification (WhatsApp/Email/SMS) ───────────────────────────

def _job_send_notification(payload: JobPayload) -> Mapping[str, Any] | None:
    """Handle async notification delivery via job queue."""
    def _run(_flask_app):
        from whatsapp.services import WhatsAppService

        payload_dict = dict(payload or {})
        to = payload_dict.get("to", "")
        template_name = payload_dict.get("template_name", "")
        language_code = payload_dict.get("language_code", "en_US")
        body_params = payload_dict.get("body_params")
        components = payload_dict.get("components")
        workspace_id = payload_dict.get("workspace_id")
        campaign_id = payload_dict.get("campaign_id")

        logger.info("[job] send-notification: to=%s template=%s", to, template_name)

        svc = WhatsAppService(workspace_id=workspace_id)
        result = svc.send_template(
            to=to,
            template_name=template_name,
            language_code=language_code,
            components=components,
        )

        logger.info("[job] send-notification result: success=%s", result.get("success"))
        return {"to": to, "template": template_name, "success": result.get("success", False)}

    return _run_with_app_context(_run)


# ── WhatsApp Bulk Campaign Processing ────────────────────────────────

def _job_whatsapp_bulk_send(payload: JobPayload) -> Mapping[str, Any] | None:
    """Process a queued bulk campaign send batch."""
    def _run(_flask_app):
        payload_dict = dict(payload or {})
        campaign_id = payload_dict.get("campaign_id")
        workspace_id = payload_dict.get("workspace_id")

        if not campaign_id:
            logger.warning("[job] whatsapp-bulk-send: missing campaign_id")
            return {"error": "missing campaign_id"}

        logger.info("[job] whatsapp-bulk-send: campaign=%s ws=%s", campaign_id, workspace_id)
        # Bulk send logic is handled within the bulk_routes module
        return {"campaign_id": campaign_id, "status": "processed"}

    return _run_with_app_context(_run)


# ── WhatsApp Webhook Processing ───────────────────────────────────────

def _job_whatsapp_webhook(payload: JobPayload) -> Mapping[str, Any] | None:
    """Process inbound Meta webhook payload off the HTTP request thread."""

    def _run(_flask_app):
        from whatsapp.webhook import WebhookProcessor
        from whatsapp.human_escalation import send_cart_notification_email_bg
        from whatsapp.models import WhatsAppMessage
        from shared_models import db

        payload_dict = dict(payload or {})
        webhook_payload = payload_dict.get("payload")
        if not webhook_payload:
            logger.warning("[job] whatsapp-webhook: missing payload")
            return {"error": "missing payload"}

        processor = WebhookProcessor(db.session)
        success, message = processor.process_webhook(webhook_payload)
        if success:
            logger.info("[job] whatsapp-webhook ok: %s", message)
            # Safety fallback: ensure catalog cart email notification is attempted
            # for inbound order webhooks processed by this job.
            try:
                entries = webhook_payload.get("entry", []) if isinstance(webhook_payload, dict) else []
                for entry in entries:
                    for change in (entry.get("changes") or []):
                        value = change.get("value") or {}
                        for incoming_msg in (value.get("messages") or []):
                            if incoming_msg.get("type") != "order":
                                continue
                            wamid = incoming_msg.get("id")
                            if not wamid:
                                continue

                            db_msg = WhatsAppMessage.query.filter_by(wamid=wamid).first()
                            if not db_msg or db_msg.type != "order":
                                continue

                            content = db_msg.content if isinstance(db_msg.content, dict) else {}
                            if content.get("cart_email_sent") is True:
                                continue

                            conversation = db_msg.conversation
                            if not conversation:
                                continue
                            account = conversation.account
                            if not account:
                                continue

                            logger.info(
                                "[job] whatsapp-webhook cart-email fallback: wamid=%s msg_id=%s conv_id=%s",
                                wamid,
                                db_msg.id,
                                conversation.id,
                            )
                            send_cart_notification_email_bg(
                                account_id=account.id,
                                conversation_id=conversation.id,
                                message_id=db_msg.id,
                                order_data=content,
                            )
            except Exception as fallback_err:
                logger.exception("[job] whatsapp-webhook cart-email fallback failed: %s", fallback_err)
        else:
            logger.error("[job] whatsapp-webhook failed: %s", message)
        return {"success": success, "message": message}

    return _run_with_app_context(_run)


# ── WhatsApp AI Generation (dedicated queue) ──────────────────────────

def _job_whatsapp_ai_generate(payload: JobPayload) -> Mapping[str, Any] | None:
    def _run(_flask_app):
        from whatsapp.ai_job import run_ai_generation

        return run_ai_generation(dict(payload or {}))

    return _run_with_app_context(_run)


# ── Job Registry ─────────────────────────────────────────────────────

JOB_DEFINITIONS: Dict[str, JobDefinition] = {
    "send-notification": JobDefinition(
        name="send-notification",
        schedule="",  # on-demand only
        path="/api/internal/jobs/send-notification",
        description="Send a WhatsApp notification asynchronously.",
        handler=_job_send_notification,
    ),
    "whatsapp-drip": JobDefinition(
        name="whatsapp-drip",
        schedule="* * * * *",
        path="/api/internal/jobs/whatsapp-drip",
        description="Process due WhatsApp drip campaigns.",
        handler=_job_whatsapp_drip,
    ),
    "whatsapp-drip-trigger-campaign": JobDefinition(
        name="whatsapp-drip-trigger-campaign",
        schedule="",
        path="/api/internal/jobs/whatsapp-drip-trigger-campaign",
        description="Trigger one drip/bulk campaign after atomic scheduler claim.",
        handler=_job_whatsapp_drip_trigger_campaign,
    ),
    "whatsapp-bulk-send": JobDefinition(
        name="whatsapp-bulk-send",
        schedule="",  # on-demand only
        path="/api/internal/jobs/whatsapp-bulk-send",
        description="Process a WhatsApp bulk campaign batch.",
        handler=_job_whatsapp_bulk_send,
    ),
    "whatsapp-webhook": JobDefinition(
        name="whatsapp-webhook",
        schedule="",
        path="/api/internal/jobs/whatsapp-webhook",
        description="Process inbound Meta webhook events asynchronously.",
        handler=_job_whatsapp_webhook,
    ),
    "whatsapp-ai-generate": JobDefinition(
        name="whatsapp-ai-generate",
        schedule="",
        path="/api/internal/jobs/whatsapp-ai-generate",
        description="Generate and send AI chatbot responses (RAG + Gemini).",
        handler=_job_whatsapp_ai_generate,
    ),
}


def get_job_definition(job_name: str) -> JobDefinition:
    try:
        return JOB_DEFINITIONS[job_name]
    except KeyError as exc:
        raise ValueError(f"Unknown job '{job_name}'") from exc


def list_job_definitions() -> list[JobDefinition]:
    return [JOB_DEFINITIONS[name] for name in sorted(JOB_DEFINITIONS.keys())]


def execute_job(job_name: str, payload: JobPayload = None) -> Dict[str, Any]:
    definition = get_job_definition(job_name)
    started_at = datetime.now(timezone.utc)
    result = definition.handler(payload or {}) or {}
    elapsed_ms = (datetime.now(timezone.utc) - started_at).total_seconds() * 1000

    return {
        "job": definition.name,
        "schedule": definition.schedule,
        "path": definition.path,
        "elapsed_ms": round(elapsed_ms),
        "timestamp": started_at.isoformat(),
        "result": dict(result),
    }
