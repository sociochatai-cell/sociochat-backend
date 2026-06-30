"""
WhatsApp Messaging Service
==========================

Handles all outgoing message operations:
  - send_text
  - send_template / send_template_with_builder
  - send_image / send_video / send_audio / send_document
  - send_interactive_buttons / send_interactive_list
  - send_sticker

Depends on:
  - _send_api_request (core infra, stays in base)
  - _store_outgoing_message (core infra, stays in base)
  - _get_or_create_conversation (account service)
  - _normalize_phone (core infra, stays in base)
"""

import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

import requests

from .models import (
    WhatsAppAccount,
    WhatsAppConversation,
    WhatsAppMessage,
    WhatsAppTemplate,
)
from notifications import notification_manager

logger = logging.getLogger(__name__)

WHATSAPP_API_BASE = "https://graph.facebook.com"


class WhatsAppMessagingService:
    """
    Mixin for WhatsApp messaging operations.

    All methods expect self.phone_number_id, self.access_token,
    self._send_api_request(), self._get_or_create_conversation(),
    self._store_outgoing_message(), and self._normalize_phone() to exist
    (provided by the base WhatsAppService via MRO).
    """

    def _resolve_outbound_conversation(
        self,
        to: str,
        conversation_id: Optional[int] = None,
    ) -> WhatsAppConversation:
        """Use explicit conversation when provided (automation flows), else get/create by phone."""
        if conversation_id:
            conversation = WhatsAppConversation.query.get(conversation_id)
            if conversation:
                return conversation
        return self._get_or_create_conversation(to)

    def _broadcast_outgoing_if_requested(
        self,
        *,
        message: WhatsAppMessage,
        conversation: WhatsAppConversation,
        enabled: bool,
    ) -> None:
        if not enabled:
            return
        try:
            account = WhatsAppAccount.query.get(conversation.account_id)
            notification_manager.broadcast(
                "whatsapp_message_received",
                {
                    "message": message.to_dict(),
                    "conversation_id": conversation.id,
                    "account_id": conversation.account_id,
                    "workspace_id": account.workspace_id if account else None,
                },
            )
        except Exception as e:
            logger.error("Failed to broadcast outgoing message: %s", e)

    # ============================================================
    # Send Text Message
    # ============================================================

    def send_text(
        self,
        to: str,
        text: str,
        waba_id: Optional[str] = None,
        preview_url: bool = False,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a text message.

        Args:
            to: Recipient phone number (E.164 format without +)
            text: Message text
            waba_id: Optional WABA ID override
            preview_url: Whether to show URL preview

        Returns:
            Response dict with success status
        """
        to = self._normalize_phone(to)

        # Get or create conversation first to check if closed
        conversation = self._resolve_outbound_conversation(to, conversation_id)

        # Block sending if conversation is closed by agent
        if conversation.closed_by_agent:
            return {
                "success": False,
                "error": "Cannot send message. This conversation was closed by an agent. Reopen it first or use a template message.",
                "error_code": "CONVERSATION_CLOSED"
            }

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {
                "preview_url": preview_url,
                "body": text,
            },
        }

        # Send via API
        result = self._send_api_request(payload)

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="text",
            content={"text": text, "preview_url": preview_url},
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()

        if result.get("success"):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=broadcast_on_success,
            )

        return result

    # ============================================================
    # Send Template Message
    # ============================================================

    def send_template(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        components: Optional[List[Dict]] = None,
        waba_id: Optional[str] = None,
        campaign_id: Optional[int] = None,
        copy_code_value: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """
        Send a template message.

        Uses TemplateBuilder for ALL code paths to ensure correct Meta API payloads.
        Handles: named/positional body params, image/video/document/text headers,
        CATALOG, FLOW, VOICE_CALL, URL, COPY_CODE, QUICK_REPLY buttons.
        """
        import re as _re
        from .template_builder import TemplateBuilder

        to = self._normalize_phone(to)

        # ── 1. Look up template metadata from DB ─────────────────────────
        template_body_text = None
        template_header_text = None
        template_footer_text = None
        template_category = None
        template_components_schema = None
        try:
            template_record = WhatsAppTemplate.query.filter_by(
                name=template_name,
                language=language_code
            ).first()
            if template_record:
                template_body_text = template_record.body_text
                template_header_text = template_record.header_text
                template_footer_text = template_record.footer_text
                template_category = template_record.category
                template_components_schema = template_record.components
        except Exception as e:
            logger.warning(f"Could not look up template body: {e}")

        # ── 2. Parse input components into structured parts ──────────────
        named_params = None        # dict of {param_name: value}
        positional_body_params = []  # list of positional body parameter dicts
        header_component = None    # header component dict
        button_components = []     # list of pre-built button component dicts
        carousel_component = None  # media-card carousel template component

        if components:
            for comp in components:
                comp_type = str(comp.get("type", "")).lower()

                if comp.get("named_params"):
                    # Named params wrapper: {'type': 'body', 'named_params': {'name': 'John'}}
                    named_params = comp["named_params"]

                elif comp_type == "body" and comp.get("parameters"):
                    positional_body_params = comp["parameters"]

                elif comp_type == "header" and comp.get("parameters"):
                    header_component = comp

                elif comp_type == "button":
                    button_components.append(comp)

                elif comp_type == "carousel" and comp.get("cards"):
                    carousel_component = comp

        # ── 3. Build payload via TemplateBuilder ─────────────────────────
        builder = TemplateBuilder(template_name, language_code)

        # 3a. Body parameters
        if named_params:
            builder.add_named_body_params(named_params)
        elif positional_body_params:
            # Check if they already have parameter_name (named style in parameters list)
            has_param_names = any(p.get("parameter_name") for p in positional_body_params)
            if has_param_names:
                # Named params already in parameters format
                builder._body_component = __import__(
                    'whatsapp.template_builder', fromlist=['TemplateComponent']
                ).TemplateComponent(type="body", parameters=positional_body_params)
            else:
                mapping = {}
                if template_record:
                    try:
                        mapping = template_record.get_variable_mapping() or {}
                    except Exception:
                        mapping = {}
                if mapping and any(str(v).strip() and not str(v).isdigit() for v in mapping.values()):
                    from .template_builder import TemplateComponent

                    params_with_names = []
                    for idx, p in enumerate(positional_body_params):
                        param = {"type": "text", "text": str(p.get("text", ""))}
                        pos_key = str(idx + 1)
                        if pos_key in mapping and mapping[pos_key]:
                            param["parameter_name"] = mapping[pos_key]
                        params_with_names.append(param)
                    builder._body_component = TemplateComponent(
                        type="body", parameters=params_with_names
                    )
                else:
                    texts = [str(p.get("text", "")) for p in positional_body_params]
                    builder.add_body_params(texts)

        # 3b. Header from input components
        if header_component and header_component.get("parameters"):
            p = header_component["parameters"][0]
            p_type = str(p.get("type", "")).lower()
            if p_type == "image":
                link = p.get("image", {}).get("link", "")
                if link:
                    builder.add_header_image(link)
            elif p_type == "video":
                link = p.get("video", {}).get("link", "")
                if link:
                    builder.add_header_video(link)
            elif p_type == "document":
                doc = p.get("document", {})
                if doc.get("link"):
                    builder.add_header_document(doc["link"], filename=doc.get("filename"))
            elif p_type == "text":
                text = p.get("text", "")
                if text:
                    builder.add_header_text(text)
            elif p_type == "location":
                # Location headers: pass through as-is
                from .template_builder import TemplateComponent
                builder._header_component = TemplateComponent(
                    type="header",
                    parameters=[p]
                )

        # 3c. Pre-built button components from input
        for btn_comp in button_components:
            from .template_builder import TemplateComponent
            builder._button_components.append(TemplateComponent(
                type="button",
                sub_type=btn_comp.get("sub_type"),
                index=int(btn_comp.get("index", 0)),
                parameters=btn_comp.get("parameters", [])
            ))

        # ── 4. Auto-inject buttons from template schema ──────────────────
        # Only if no button components were explicitly provided
        if template_components_schema and not button_components:
            for schema_comp in template_components_schema:
                if str(schema_comp.get("type", "")).upper() != "BUTTONS":
                    continue

                for btn_idx, button in enumerate(schema_comp.get("buttons", [])):
                    btn_type = str(button.get("type", "")).upper()

                    if btn_type == "CATALOG":
                        # CATALOG buttons need sub_type "catalog" at send time
                        # Optionally include a thumbnail product retailer ID
                        builder.add_catalog_button(btn_idx)

                    elif btn_type == "FLOW":
                        builder.add_flow_button(btn_idx)

                    elif btn_type == "VOICE_CALL":
                        builder.add_voice_call_button(btn_idx)

                    elif btn_type == "COPY_CODE":
                        example = button.get("example", [])
                        coupon = (
                            copy_code_value
                            or (example if isinstance(example, str)
                                else (example[0] if isinstance(example, list) and example else ""))
                        )
                        coupon = _re.sub(r'[^A-Za-z0-9]', '', str(coupon))
                        builder.add_copy_code_button(btn_idx, coupon)

                    elif btn_type == "URL":
                        url = button.get("url", "")
                        if "{{" in url:
                            # URL with variable — use first body param as suffix
                            suffix = ""
                            if positional_body_params:
                                suffix = str(positional_body_params[0].get("text", ""))
                            elif named_params:
                                suffix = str(list(named_params.values())[0]) if named_params else ""
                            builder.add_url_button(btn_idx, suffix)

                    elif btn_type == "QUICK_REPLY":
                        # Quick reply buttons don't need parameters at send time
                        pass

        # ── 5. Build final payload ───────────────────────────────────────
        payload = builder.build_with_recipient(to)

        if carousel_component:
            tpl_components = payload.setdefault("template", {}).setdefault("components", [])
            tpl_components.append(carousel_component)

        # Print the exact payload being sent for debugging
        print(f"\n{'='*60}")
        print(f"=== TEMPLATE PAYLOAD ===")
        print(f"Template Name: {template_name}")
        print(f"Language Code: {language_code}")
        print(f"Template Schema: {template_components_schema}")
        print(f"Input Components: {components}")
        print(f"Full Payload: {json.dumps(payload, indent=2)}")
        print(f"{'='*60}\n")

        # ── 6. Send and store ────────────────────────────────────────────
        conversation = self._resolve_outbound_conversation(to, conversation_id)

        result = self._send_api_request(payload)

        if result.get("success"):
            logger.info(f"Template '{template_name}' sent successfully, wamid: {result.get('wamid')}")
        else:
            logger.error(f"Template '{template_name}' failed: {result.get('error')}")

        # Extract body_params for frontend preview display
        body_params_for_preview = []
        if named_params:
            body_params_for_preview = list(named_params.values())
        elif positional_body_params:
            body_params_for_preview = [p.get("text", "") for p in positional_body_params]

        template_buttons_for_preview = []
        try:
            from .order_enrichment import template_buttons_from_schema
            template_buttons_for_preview = template_buttons_from_schema(template_components_schema)
        except Exception:
            template_buttons_for_preview = []

        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="template",
            content={
                "template_name": template_name,
                "language": language_code,
                "components": components if components else None,
                "body_params": body_params_for_preview,
                "body": template_body_text,
                "header": template_header_text,
                "footer": template_footer_text,
                "buttons": template_buttons_for_preview or None,
                "payload_sent": payload,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            template_name=template_name,
            template_category=template_category,
            campaign_id=campaign_id,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        result["payload_sent"] = payload

        if result.get("success") and (broadcast_on_success or not defer_post_send):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=True,
            )

        return result

    def send_template_with_builder(
        self,
        to: str,
        template_name: str,
        language_code: str = "en",
        body_params: Optional[List[str]] = None,
        header_image_url: Optional[str] = None,
        header_video_url: Optional[str] = None,
        header_document_url: Optional[str] = None,
        header_text: Optional[str] = None,
        button_payloads: Optional[List[Dict]] = None,
        waba_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a template message using the TemplateBuilder for proper payload generation.

        This method is recommended over send_template() as it properly handles:
        - Templates without variables (no components field)
        - Image/video/document headers
        - Body parameters
        - Button parameters
        """
        from .template_builder import TemplateBuilder

        to = self._normalize_phone(to)

        # Use builder to construct proper payload
        builder = TemplateBuilder(template_name, language_code)

        # Add header if provided
        if header_image_url:
            builder.add_header_image(header_image_url)
        elif header_video_url:
            builder.add_header_video(header_video_url)
        elif header_document_url:
            builder.add_header_document(header_document_url)
        elif header_text:
            builder.add_header_text(header_text)

        # Add body params if provided
        if body_params:
            builder.add_body_params(body_params)

        # Add button params if provided
        if button_payloads:
            for btn in button_payloads:
                btn_index = btn.get("index", 0)
                btn_type = btn.get("type", "url")
                btn_value = btn.get("value", "")

                if btn_type == "url":
                    builder.add_url_button(btn_index, btn_value)
                elif btn_type == "quick_reply":
                    builder.add_quick_reply_button(btn_index, btn_value)
                elif btn_type == "copy_code":
                    builder.add_copy_code_button(btn_index, btn_value)

        # Build payload
        payload = builder.build_with_recipient(to)

        # Log for debugging
        logger.info(f"Sending template '{template_name}' via builder to {to}")
        logger.debug(f"Builder payload: {json.dumps(payload, indent=2)}")

        # Get or create conversation
        conversation = self._get_or_create_conversation(to)

        # Send via API
        result = self._send_api_request(payload)

        template_buttons_for_preview = []
        try:
            from .models import WhatsAppTemplate
            from .order_enrichment import template_buttons_from_schema

            template_record = WhatsAppTemplate.query.filter_by(
                name=template_name,
                language=language_code,
            ).first()
            if template_record:
                template_buttons_for_preview = template_buttons_from_schema(template_record.components)
        except Exception:
            template_buttons_for_preview = []

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="template",
            content={
                "template_name": template_name,
                "language": language_code,
                "body_params": body_params,
                "header_image_url": header_image_url,
                "header_video_url": header_video_url,
                "header_document_url": header_document_url,
                "header_text": header_text,
                "button_payloads": button_payloads,
                "buttons": template_buttons_for_preview or None,
                "payload_sent": payload,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            template_name=template_name,  # Store for analytics tracking
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        result["payload_sent"] = payload

        # Broadcast via SSE for real-time inbox update
        if result.get("success"):
            try:
                account = WhatsAppAccount.query.filter_by(phone_number_id=self.phone_number_id).first()
                workspace_id = account.workspace_id if account else None

                notification_manager.broadcast("whatsapp_message_received", {
                    "message": message.to_dict(),
                    "conversation_id": conversation.id,
                    "account_id": account.id if account else None,
                    "workspace_id": workspace_id
                })
                logger.info(f"Broadcasted template (builder) message: {message.id}")
            except Exception as e:
                logger.error(f"Failed to broadcast template message event: {e}")

        return result

    # ============================================================
    # Send Media Messages
    # ============================================================

    def upload_media_from_url(self, url: str, *, media_kind: str = "image") -> Optional[str]:
        """Download URL and upload to Meta; returns media id (avoids Meta fetching BML links)."""
        if not url or not self.phone_number_id or not self.access_token:
            return None
        try:
            resp = requests.get(url, timeout=45, allow_redirects=True)
            if resp.status_code != 200 or len(resp.content) < 128:
                logger.warning(
                    "[media_upload] download failed status=%s bytes=%s url=%s",
                    resp.status_code,
                    len(resp.content),
                    url[:120],
                )
                return None
            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip().lower()
            if media_kind == "image" and not content_type.startswith("image/"):
                logger.warning("[media_upload] not image content_type=%s url=%s", content_type, url[:120])
                return None
            upload_url = f"{WHATSAPP_API_BASE}/v22.0/{self.phone_number_id}/media"
            ext = content_type.split("/")[-1] or "jpg"
            files = {"file": (f"media.{ext}", resp.content, content_type)}
            data = {"messaging_product": "whatsapp", "type": content_type}
            up = requests.post(
                upload_url,
                headers={"Authorization": f"Bearer {self.access_token}"},
                data=data,
                files=files,
                timeout=90,
            )
            body = up.json() if up.content else {}
            media_id = body.get("id")
            if not media_id:
                logger.warning("[media_upload] Meta rejected upload url=%s body=%s", url[:120], str(body)[:300])
            return media_id
        except Exception as exc:
            logger.warning("[media_upload] exception url=%s err=%s", url[:120], exc)
            return None

    def send_image_with_url_candidates(
        self,
        to: str,
        image_url: Optional[str] = None,
        url_candidates: Optional[List[str]] = None,
        caption: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Try each candidate URL — upload to Meta first, then fall back to link send."""
        urls: List[str] = []
        for u in [image_url, *(url_candidates or [])]:
            if u and u not in urls:
                urls.append(u)
        last_result: Dict[str, Any] = {"success": False, "error": "no image url"}
        for url in urls:
            media_id = self.upload_media_from_url(url)
            if media_id:
                result = self.send_image(
                    to=to,
                    image_id=media_id,
                    caption=caption,
                    conversation_id=conversation_id,
                    defer_post_send=defer_post_send,
                    broadcast_on_success=broadcast_on_success,
                )
                if result.get("success"):
                    return result
                last_result = result
            result = self.send_image(
                to=to,
                image_url=url,
                caption=caption,
                conversation_id=conversation_id,
                defer_post_send=defer_post_send,
                broadcast_on_success=broadcast_on_success,
            )
            if result.get("success"):
                return result
            last_result = result
        return last_result

    def send_image(
        self,
        to: str,
        image_url: Optional[str] = None,
        image_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send an image message."""
        return self._send_media(
            to,
            "image",
            media_url=image_url,
            media_id=image_id,
            caption=caption,
            waba_id=waba_id,
            conversation_id=conversation_id,
            defer_post_send=defer_post_send,
            broadcast_on_success=broadcast_on_success,
        )

    def send_video(
        self,
        to: str,
        video_url: Optional[str] = None,
        video_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send a video message."""
        return self._send_media(
            to,
            "video",
            media_url=video_url,
            media_id=video_id,
            caption=caption,
            waba_id=waba_id,
            conversation_id=conversation_id,
            defer_post_send=defer_post_send,
            broadcast_on_success=broadcast_on_success,
        )

    def send_audio(
        self,
        to: str,
        audio_url: Optional[str] = None,
        audio_id: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send an audio message (no caption support)."""
        return self._send_media(
            to,
            "audio",
            media_url=audio_url,
            media_id=audio_id,
            caption=None,
            waba_id=waba_id,
            conversation_id=conversation_id,
            defer_post_send=defer_post_send,
            broadcast_on_success=broadcast_on_success,
        )

    def send_document(
        self,
        to: str,
        document_url: Optional[str] = None,
        document_id: Optional[str] = None,
        caption: Optional[str] = None,
        filename: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send a document message."""
        return self._send_media(
            to,
            "document",
            media_url=document_url,
            media_id=document_id,
            caption=caption,
            waba_id=waba_id,
            filename=filename,
            conversation_id=conversation_id,
            defer_post_send=defer_post_send,
            broadcast_on_success=broadcast_on_success,
        )

    def _send_media(
        self,
        to: str,
        media_type: str,
        media_url: Optional[str] = None,
        media_id: Optional[str] = None,
        caption: Optional[str] = None,
        waba_id: Optional[str] = None,
        filename: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Internal method to send any media message."""
        to = self._normalize_phone(to)

        if not media_url and not media_id:
            return {"success": False, "error": "Either media_url or media_id is required"}

        media_obj = {}
        if media_id:
            media_obj["id"] = media_id
        else:
            media_obj["link"] = media_url

        if caption and media_type != "audio":
            media_obj["caption"] = caption

        if filename and media_type == "document":
            media_obj["filename"] = filename

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": media_type,
            media_type: media_obj,
        }

        # Get or create conversation
        conversation = self._resolve_outbound_conversation(to, conversation_id)

        # Send via API
        result = self._send_api_request(payload)

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type=media_type,
            content={
                "media_type": media_type,
                "url": media_url,
                "id": media_id,
                "caption": caption,
                "filename": filename,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()

        if result.get("success"):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=broadcast_on_success,
            )

        return result

    # ============================================================
    # Send Interactive Messages
    # ============================================================

    def send_interactive_buttons(
        self,
        to: str,
        body_text: str,
        buttons: List[Dict[str, str]],
        header_text: Optional[str] = None,
        header_image_url: Optional[str] = None,
        header_video_url: Optional[str] = None,
        header_document_url: Optional[str] = None,
        header_document_filename: Optional[str] = None,
        footer_text: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send an interactive message with buttons (max 3 buttons)."""
        to = self._normalize_phone(to)

        # Format buttons - handle both pre-formatted and simple formats
        formatted_buttons = []
        for btn in buttons[:3]:  # Max 3 buttons
            if btn.get("type") == "reply" and isinstance(btn.get("reply"), dict):
                btn_id = btn["reply"].get("id", str(len(formatted_buttons) + 1))
                btn_title = btn["reply"].get("title", "")
            else:
                btn_id = btn.get("id", str(len(formatted_buttons) + 1))
                btn_title = (
                    btn.get("title")
                    or btn.get("label")
                    or btn.get("text")
                    or ""
                )

            if not btn_title:
                btn_title = f"Option {len(formatted_buttons) + 1}"

            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn_id,
                    "title": btn_title[:100],
                },
            })

        # Debug: print the formatted buttons
        print(f"Formatted buttons for Meta API: {formatted_buttons}")

        interactive = {
            "type": "button",
            "body": {"text": body_text},
            "action": {"buttons": formatted_buttons},
        }

        # Header support: image > video > document > text (priority order)
        header_type = None
        print(f"[DEBUG] send_interactive_buttons header params:")
        print(f"        header_image_url: {header_image_url}")
        print(f"        header_video_url: {header_video_url}")
        print(f"        header_document_url: {header_document_url}")
        print(f"        header_text: {header_text}")

        if header_image_url:
            interactive["header"] = {"type": "image", "image": {"link": header_image_url}}
            header_type = "image"
            print(f"        -> Using IMAGE header: {header_image_url}")
        elif header_video_url:
            interactive["header"] = {"type": "video", "video": {"link": header_video_url}}
            header_type = "video"
            print(f"        -> Using VIDEO header: {header_video_url}")
        elif header_document_url:
            doc_header = {"link": header_document_url}
            if header_document_filename:
                doc_header["filename"] = header_document_filename
            interactive["header"] = {"type": "document", "document": doc_header}
            header_type = "document"
            print(f"        -> Using DOCUMENT header: {header_document_url}")
        elif header_text:
            interactive["header"] = {"type": "text", "text": header_text}
            header_type = "text"
            print(f"        -> Using TEXT header: {header_text}")
        else:
            print(f"        -> NO header")

        if footer_text:
            interactive["footer"] = {"text": footer_text}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }

        # Get or create conversation
        conversation = self._resolve_outbound_conversation(to, conversation_id)

        # Send via API
        result = self._send_api_request(payload)

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="interactive",
            content={
                "interactive_type": "button",
                "body": body_text,
                "header": header_text,
                "header_type": header_type,
                "header_image_url": header_image_url,
                "header_video_url": header_video_url,
                "header_document_url": header_document_url,
                "header_document_filename": header_document_filename,
                "footer": footer_text,
                "buttons": buttons,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()

        if result.get("success"):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=broadcast_on_success,
            )

        return result

    def send_interactive_list(
        self,
        to: str,
        body_text: str,
        button_text: str,
        sections: List[Dict],
        header_text: Optional[str] = None,
        footer_text: Optional[str] = None,
        waba_id: Optional[str] = None,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send an interactive message with a list menu."""
        to = self._normalize_phone(to)

        interactive = {
            "type": "list",
            "body": {"text": body_text},
            "action": {
                "button": button_text[:100],
                "sections": sections,
            },
        }

        if header_text:
            interactive["header"] = {"type": "text", "text": header_text}

        if footer_text:
            interactive["footer"] = {"text": footer_text}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }

        # Get or create conversation
        conversation = self._resolve_outbound_conversation(to, conversation_id)

        # Send via API
        result = self._send_api_request(payload)

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="interactive",
            content={
                "interactive_type": "list",
                "body": body_text,
                "header": header_text,
                "footer": footer_text,
                "button": button_text,
                "sections": sections,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()

        if result.get("success"):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=broadcast_on_success,
            )

        return result

    def send_interactive_passthrough(
        self,
        to: str,
        interactive: Dict[str, Any],
        *,
        conversation_id: Optional[int] = None,
        defer_post_send: bool = False,
        broadcast_on_success: bool = False,
    ) -> Dict[str, Any]:
        """Send a Meta Cloud API interactive object as-is (list, button, product, etc.)."""
        to = self._normalize_phone(to)
        if not isinstance(interactive, dict) or not interactive.get("type"):
            return {"success": False, "error": "interactive.type is required for passthrough"}

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }

        conversation = self._resolve_outbound_conversation(to, conversation_id)
        result = self._send_api_request(payload)

        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="interactive",
            content={
                "interactive_type": interactive.get("type"),
                "interactive": interactive,
                "passthrough": True,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
            commit=not defer_post_send,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()
        result["payload_sent"] = payload

        if result.get("success"):
            self._broadcast_outgoing_if_requested(
                message=message,
                conversation=conversation,
                enabled=broadcast_on_success,
            )

        return result

    # ============================================================
    # Send Sticker
    # ============================================================

    def send_sticker(self, to: str, sticker: str) -> Dict[str, Any]:
        """Send a sticker message."""
        to = self._normalize_phone(to)

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "sticker",
            "sticker": {}
        }

        sticker_url = None
        sticker_id = None
        if sticker.startswith("http"):
            payload["sticker"]["link"] = sticker
            sticker_url = sticker
        else:
            payload["sticker"]["id"] = sticker
            sticker_id = sticker

        # Get or create conversation
        conversation = self._get_or_create_conversation(to)

        # Send via API
        result = self._send_api_request(payload)

        # Store message in DB
        message = self._store_outgoing_message(
            conversation=conversation,
            message_type="sticker",
            content={
                "media_type": "sticker",
                "url": sticker_url,
                "id": sticker_id,
            },
            wamid=result.get("wamid"),
            status="sent" if result.get("success") else "failed",
            error_code=str(result.get("error_code")) if result.get("error_code") else None,
            error_message=result.get("error") if not result.get("success") else None,
        )

        result["message_id"] = message.id
        result["conversation_id"] = conversation.id
        result["message"] = message.to_dict()

        return result
