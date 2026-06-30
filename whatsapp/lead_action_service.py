"""
Lead Action Service (Flow → CRM Lead engine)
============================================

Turns flow node `leadAction` config into real CRM leads on the shared DB.

A flow node (input / message / api) may carry a canonical `leadAction` block on
its `node.data`:

    leadAction = {
        "enabled": bool,
        "condition": {
            "source": "response" | "field" | "apiPath",
            "path": str,
            "operator": "equals" | "not_equals" | "contains" | "exists"
                        | "not_exists" | "gt" | "lt" | "regex" | "any",
            "value": str,
        },
        "leadType": str,            # lead_types.key
        "stage": str,               # pipeline_stages.key
        "mapFields": {              # values are {{templates}} over collected fields
            "name": str, "email": str, "phone": str, "company": str
        },
    }

When the condition matches, we UPSERT a lead (deduped by workspace + phone),
resolve the configured stage within the workspace's default pipeline, mirror the
legacy `leads.status` enum from the stage key, write the back-reference on
`whatsapp_conversations.lead_id`, and log a CRM Activity row.

Everything here is BEST-EFFORT: every public entry wraps its body in try/except
and never raises into the flow engine.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import text

from shared_models import db
from . import input_node_handler
from . import api_node_executor
from .crm_lead_models import CrmLead, CrmPipeline, CrmPipelineStage
from .lead_intent_router import classify_lead_intent
from .lead_growth_models import get_lead_growth_settings
from .utils import normalize_phone, normalize_phone_robust

logger = logging.getLogger(__name__)


# Canonical pipeline stage keys (mirror of the default pipeline seeded in P0).
_DEFAULT_STAGE_KEY = "new"

# Intents that are strong enough to auto-create/update a lead on their own when
# full auto-discovery is enabled (feature B). A high-intent inbound message with
# sufficient classifier confidence upserts a lead even without a flow leadAction.
_HIGH_INTENT = frozenset({"pricing", "purchase", "lead_capture", "catalog"})

# Mirror the configurable stage key onto the legacy leads.status enum, whose
# allowed values are: new / contacted / qualified / proposal / closed.
_LEGACY_STATUS_ENUM = ("new", "contacted", "qualified", "proposal", "closed")


def _mirror_legacy_status(stage_key: Optional[str]) -> str:
    """Map a pipeline stage key onto the legacy leads.status enum value.

    new/contacted/qualified/proposal -> same.
    won -> closed. lost/negotiation/nurture -> nearest enum value.
    Anything unknown -> 'new'.
    """
    key = (stage_key or "").strip().lower()
    if key in _LEGACY_STATUS_ENUM:
        return key
    if key == "won":
        return "closed"
    if key == "lost":
        return "closed"
    if key == "negotiation":
        # furthest pre-close stage we model in the legacy enum
        return "proposal"
    if key == "nurture":
        return "contacted"
    return "new"


def _template(value: Optional[str], collected: Optional[Dict[str, Any]]) -> Optional[str]:
    """Render a {{var}} template over collected fields. Empty -> None."""
    if not value:
        return None
    try:
        rendered = input_node_handler.substitute_variables(str(value), collected or {})
    except Exception:
        rendered = str(value)
    rendered = (rendered or "").strip()
    # If the template referenced a missing field it stays as the literal {{...}};
    # treat an unresolved placeholder as no value.
    if not rendered or ("{{" in rendered and "}}" in rendered):
        return None
    return rendered


def _coerce_number(value: Any) -> Optional[float]:
    """Coerce a value to float, stripping common thousands separators first.

    Returns None when coercion fails (caller is responsible for logging).
    """
    try:
        cleaned = str(value).strip().replace(",", "").replace("_", "")
        return float(cleaned)
    except (TypeError, ValueError):
        return None


# Cap the user-controlled regex pattern length to avoid pathological/ReDoS input.
_MAX_REGEX_PATTERN_LEN = 512


def evaluate_condition(
    condition: Optional[Dict[str, Any]],
    *,
    captured_value: Any = None,
    collected: Optional[Dict[str, Any]] = None,
    api_result: Any = None,
) -> bool:
    """Shared operator evaluator for a leadAction condition.

    Returns True when the lead action should fire. No condition (or operator
    'any') => always True.
    """
    if not condition or not isinstance(condition, dict):
        return True

    operator = str(condition.get("operator") or "any").strip().lower()
    if operator == "any":
        return True

    source = str(condition.get("source") or "response").strip().lower()
    path = condition.get("path")
    expected = condition.get("value")

    # Resolve the actual value the operator runs against.
    if source == "response":
        actual = captured_value
    elif source == "field":
        actual = (collected or {}).get(path) if path else None
    elif source == "apipath":
        parsed = getattr(api_result, "parsed", None)
        actual = api_node_executor.get_path(parsed, path) if path else parsed
    else:
        actual = captured_value

    if operator == "exists":
        return actual is not None and str(actual).strip() != ""
    if operator == "not_exists":
        return actual is None or str(actual).strip() == ""

    actual_str = "" if actual is None else str(actual)
    expected_str = "" if expected is None else str(expected)

    if operator == "equals":
        return actual_str.strip().lower() == expected_str.strip().lower()
    if operator == "not_equals":
        return actual_str.strip().lower() != expected_str.strip().lower()
    if operator == "contains":
        return expected_str.strip().lower() in actual_str.lower()
    if operator in ("gt", "lt"):
        a, b = _coerce_number(actual), _coerce_number(expected)
        if a is None or b is None:
            logger.debug(
                "[lead_action] %s comparison skipped — non-numeric operand(s) actual=%r expected=%r",
                operator, actual, expected,
            )
            return False
        return (a > b) if operator == "gt" else (a < b)
    if operator == "regex":
        # Both pattern and subject are user-controlled: cap pattern length and guard
        # execution so a malformed/pathological pattern can never raise into the flow.
        if len(expected_str) > _MAX_REGEX_PATTERN_LEN:
            logger.warning(
                "[lead_action] regex pattern too long (%d > %d) — treating as no-match",
                len(expected_str), _MAX_REGEX_PATTERN_LEN,
            )
            return False
        try:
            return bool(re.search(expected_str, actual_str))
        except Exception:
            logger.warning("[lead_action] regex evaluation failed for pattern %r — no-match", expected_str)
            return False

    # Unknown operator: be permissive but log.
    logger.warning("[lead_action] unknown operator %r — treating as no-match", operator)
    return False


class LeadActionService:
    """Best-effort flow→lead writer. Never raises into the flow engine."""

    @staticmethod
    def maybe_mark_lead(
        workspace_id: Any,
        conversation: Any,
        node_data: Optional[Dict[str, Any]],
        captured_value: Any = None,
        collected: Optional[Dict[str, Any]] = None,
        api_result: Any = None,
        message_text: Optional[str] = None,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """Evaluate a node's leadAction and UPSERT a CRM lead if it matches.

        Returns a tuple ``(lead_id, is_new, resolved_stage_key)``:
          * lead_id            — the lead id (str) on success, else None.
          * is_new             — True when this call CREATED the lead, else False.
          * resolved_stage_key — the pipeline stage key the lead was routed to.

        On any no-op or failure this returns ``(None, False, None)``. All failures
        are swallowed — this must never break the WhatsApp flow.

        `message_text` is the raw inbound message text (when available) threaded
        through for downstream intent-aware lead handling; it is optional and the
        path never breaks when it is None.
        """
        try:
            return LeadActionService._maybe_mark_lead_impl(
                workspace_id=workspace_id,
                conversation=conversation,
                node_data=node_data,
                captured_value=captured_value,
                collected=collected,
                api_result=api_result,
                message_text=message_text,
            )
        except Exception:
            logger.exception("[lead_action] maybe_mark_lead failed (best-effort, ignored)")
            try:
                db.session.rollback()
            except Exception:
                pass
            return (None, False, None)

    @staticmethod
    def maybe_discover_lead(
        workspace_id: Any,
        conversation: Any,
        message_text: Optional[str],
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """Full auto-discovery: classify EVERY inbound message and, when it shows
        high purchase intent, auto-create/update a CRM lead — independent of any
        flow `leadAction` node.

        Gated by the per-workspace ``auto_discovery_enabled`` toggle. Fires only when
        the classified intent is in the HIGH-INTENT set AND the classifier confidence
        clears ``confidence_threshold``. It synthesizes a leadAction block and routes
        the upsert through the SAME ``_maybe_mark_lead_impl`` path so dedup, stage
        routing, notification (C) and nurture (D) all behave identically to a node-
        driven lead.

        Returns the same ``(lead_id, is_new, resolved_stage_key)`` tuple as
        ``maybe_mark_lead``; ``(None, False, None)`` on any no-op or failure. Strictly
        best-effort — never raises into the inbound path.
        """
        try:
            if not message_text or not str(message_text).strip():
                return (None, False, None)

            try:
                ws_id = int(workspace_id)
            except (TypeError, ValueError):
                return (None, False, None)

            settings = get_lead_growth_settings(ws_id)
            if not settings.get("auto_discovery_enabled"):
                return (None, False, None)

            classification = classify_lead_intent(message_text)
            intent = classification.get("intent")
            try:
                confidence = float(classification.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            try:
                threshold = float(settings.get("confidence_threshold") or 0.0)
            except (TypeError, ValueError):
                threshold = 0.0

            if intent not in _HIGH_INTENT or confidence < threshold:
                return (None, False, None)

            # Route the discovered lead to the intent's configured stage when one is
            # mapped, otherwise leave it blank so the DEFAULT stage applies. We pass an
            # explicit stage here (rather than relying on the intent-override path in
            # _maybe_mark_lead_impl) so the synthetic node is self-describing.
            intent_rules = settings.get("intent_rules") or {}
            stage = intent_rules.get(intent) if isinstance(intent_rules, dict) else None

            # Map extracted entities onto the lead's identity fields. These are literal
            # values (not {{templates}}); _template leaves plain strings untouched.
            entities = classification.get("entities") or {}
            map_fields: Dict[str, Any] = {}
            for key in ("name", "email", "phone", "company"):
                value = entities.get(key)
                if value:
                    map_fields[key] = str(value)

            synthetic_node = {
                "id": f"auto-discovery:{intent}",
                "leadAction": {
                    "enabled": True,
                    "stage": stage,
                    "leadType": None,
                    "condition": {"operator": "any"},
                    "mapFields": map_fields,
                },
            }

            result = LeadActionService._maybe_mark_lead_impl(
                workspace_id=workspace_id,
                conversation=conversation,
                node_data=synthetic_node,
                captured_value=message_text,
                collected={},
                api_result=None,
                message_text=message_text,
            )

            # Unlike the node-driven path (which rides the flow engine's transaction),
            # auto-discovery owns its own transaction boundary: it runs standalone in
            # the inbound router where many code paths return without a later commit.
            # Persist the upsert + its back-reference/activity ourselves so a discovered
            # lead is never lost. Best-effort.
            if result and result[0] is not None:
                try:
                    db.session.commit()
                except Exception:
                    logger.exception("[lead_action] auto-discovery commit failed (best-effort)")
                    try:
                        db.session.rollback()
                    except Exception:
                        pass
                    return (None, False, None)
            return result
        except Exception:
            logger.exception("[lead_action] maybe_discover_lead failed (best-effort, ignored)")
            try:
                db.session.rollback()
            except Exception:
                pass
            return (None, False, None)

    # ── internals ────────────────────────────────────────────────────────

    @staticmethod
    def _maybe_mark_lead_impl(
        *,
        workspace_id: Any,
        conversation: Any,
        node_data: Optional[Dict[str, Any]],
        captured_value: Any,
        collected: Optional[Dict[str, Any]],
        api_result: Any,
        message_text: Optional[str] = None,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        # `message_text` is the raw inbound text threaded from the engine (optional).
        # It is accepted here so intent-aware handling can consume it later; the
        # lead-upsert path below does not depend on it and never breaks when None.
        node_data = node_data or {}
        lead_action = node_data.get("leadAction")
        if not isinstance(lead_action, dict) or not lead_action.get("enabled"):
            return (None, False, None)

        # workspace_id is a STRING in whatsapp-service; leads.workspace_id is INTEGER.
        try:
            ws_id = int(workspace_id)
        except (TypeError, ValueError):
            logger.warning("[lead_action] non-integer workspace_id=%r — skipping", workspace_id)
            return (None, False, None)

        # Condition gate.
        if not evaluate_condition(
            lead_action.get("condition"),
            captured_value=captured_value,
            collected=collected,
            api_result=api_result,
        ):
            return (None, False, None)

        collected = collected or {}
        map_fields = lead_action.get("mapFields") or {}

        # Resolve identity fields (templated → collected → conversation fallback).
        conv_phone = getattr(conversation, "user_phone", None)
        conv_name = getattr(conversation, "user_name", None)
        conv_id = getattr(conversation, "id", None)

        phone = (
            _template(map_fields.get("phone"), collected)
            or (str(collected.get("phone")).strip() if collected.get("phone") else None)
            or (str(conv_phone).strip() if conv_phone else None)
        )
        if not phone:
            logger.info("[lead_action] no phone resolvable — skipping lead upsert ws=%s", ws_id)
            return (None, False, None)

        # Normalize to the SAME canonical form the conversation layer stores
        # (services._normalize_phone delegates to normalize_phone_robust, with the
        # basic normalize_phone as fallback). Doing this BEFORE the dedup lookup AND
        # before insert prevents duplicate leads for the same contact.
        canonical_phone = normalize_phone_robust(phone) or normalize_phone(phone)
        if canonical_phone:
            phone = canonical_phone

        name = (
            _template(map_fields.get("name"), collected)
            or (str(collected.get("name")).strip() if collected.get("name") else None)
            or (str(conv_name).strip() if conv_name else None)
            or phone
        )
        email = _template(map_fields.get("email"), collected) or (
            str(collected.get("email")).strip() if collected.get("email") else None
        )
        company = _template(map_fields.get("company"), collected) or (
            str(collected.get("company")).strip() if collected.get("company") else None
        )

        # Resolve default pipeline + configured stage.
        #
        # Stage precedence:
        #   1. An EXPLICIT stage on the node's leadAction always wins.
        #   2. Otherwise (the node left stage blank => DEFAULT), an intent classified
        #      from the inbound text — gated by per-workspace settings — may route the
        #      lead to a configured stage via settings.intent_rules.
        #   3. Fall back to the canonical DEFAULT stage key.
        # _resolve_stage already safely falls back when a key doesn't exist, so a bad
        # intent_rules value can never break the upsert.
        explicit_node_stage = (lead_action.get("stage") or "").strip()
        stage_key = explicit_node_stage or _DEFAULT_STAGE_KEY

        if not explicit_node_stage and message_text:
            intent_stage = LeadActionService._intent_stage_override(ws_id, message_text)
            if intent_stage:
                stage_key = intent_stage

        pipeline_id, stage_id, resolved_stage_key = LeadActionService._resolve_stage(
            ws_id, stage_key
        )
        legacy_status = _mirror_legacy_status(resolved_stage_key)

        wa_account_id = getattr(conversation, "account_id", None)
        lead_type = lead_action.get("leadType")

        now = datetime.utcnow()

        # Concurrent-duplicate guard: there is no DB unique constraint on
        # (workspace_id, phone) — existing data may contain dupes, so we cannot add
        # one. Instead serialize concurrent webhooks for the SAME contact with a
        # transaction-level Postgres advisory lock keyed on (workspace_id, phone).
        # The lock auto-releases at COMMIT/ROLLBACK, so two concurrent inbound
        # messages run the select-then-insert one after the other and only one
        # INSERT happens. Best-effort: a non-Postgres backend just skips the lock.
        try:
            db.session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": f"{ws_id}:{phone}"},
            )
        except Exception:
            logger.debug("[lead_action] advisory lock unavailable (best-effort) ws=%s", ws_id)

        # UPSERT deduped by (workspace_id, phone).
        existing = (
            db.session.query(CrmLead)
            .filter(CrmLead.workspace_id == ws_id, CrmLead.phone == phone)
            .order_by(CrmLead.created_at.asc())
            .first()
        )

        is_new = existing is None
        if is_new:
            lead = CrmLead(
                id=str(uuid.uuid4()),
                workspace_id=ws_id,
                name=name,
                phone=phone,
                # status column is physically NOT NULL — set the mirrored status at
                # construction so the row is valid the instant it flushes.
                status=legacy_status or _DEFAULT_STAGE_KEY,
                source="whatsapp",
                created_at=now,
            )
            db.session.add(lead)
        else:
            lead = existing

        prev_stage_id = getattr(lead, "stage_id", None)
        # Snapshot the material fields BEFORE applying updates so we can record an
        # Activity when ANY of them actually changes (not just on stage move).
        prev_material = {
            "name": getattr(lead, "name", None),
            "email": getattr(lead, "email", None),
            "company": getattr(lead, "company", None),
            "lead_type": getattr(lead, "lead_type", None),
            "stage_id": prev_stage_id,
        }

        # Apply mapped / resolved fields (only overwrite when we have a value).
        if name:
            lead.name = name
        if email:
            lead.email = email
        if company:
            lead.company = company
        if pipeline_id is not None:
            lead.pipeline_id = pipeline_id
        if stage_id is not None:
            lead.stage_id = stage_id
        if legacy_status:
            lead.status = legacy_status
        if lead_type:
            lead.lead_type = lead_type
        if conv_id is not None:
            lead.conversation_id = conv_id
        if wa_account_id is not None:
            lead.wa_account_id = wa_account_id
        if not getattr(lead, "source", None):
            lead.source = "whatsapp"
        lead.updated_at = now
        lead.last_interaction_at = now

        stage_changed = (prev_stage_id != stage_id)
        material_changed = stage_changed or any(
            prev_material[f] != getattr(lead, f, None)
            for f in ("name", "email", "company", "lead_type")
        )

        # Wrap the lead + conversation.lead_id + activity writes in a SAVEPOINT.
        # We do NOT commit here: the engine owns the single transaction boundary, so
        # lead writes ride the current step's transaction. begin_nested() lets us
        # release (on success) or roll back (on failure) JUST these writes without
        # prematurely persisting engine flow-state or defeating rollback-on-send-failure.
        nested = db.session.begin_nested()
        try:
            db.session.flush()  # ensure lead.id is populated for back-ref/activity
            lead_id = lead.id

            # Back-reference: whatsapp_conversations.lead_id (column added in P0; not in ORM).
            if conv_id is not None:
                LeadActionService._set_conversation_lead_ref(conv_id, lead_id)

            # Activity row (best-effort, valid enum value only).
            LeadActionService._write_activity(
                workspace_id=ws_id,
                lead_id=lead_id,
                is_new=is_new,
                fields_changed=material_changed,
                stage_key=resolved_stage_key,
                timestamp=now,
            )
            nested.commit()  # release savepoint; does NOT commit the outer txn
        except Exception:
            logger.exception("[lead_action] lead write failed (best-effort, savepoint rolled back)")
            try:
                nested.rollback()
            except Exception:
                pass
            return (None, False, None)

        logger.info(
            "[lead_action] %s lead=%s ws=%s phone=%s stage=%s status=%s type=%s conv=%s",
            "created" if is_new else "updated",
            lead_id, ws_id, phone, resolved_stage_key, legacy_status, lead_type, conv_id,
        )

        # ── New-lead side effects (features C + D) ──
        # Only fire on CREATION, and only when the workspace has them enabled. Both
        # are strictly best-effort: a failure here never affects the lead upsert (the
        # savepoint already committed) and never raises into the flow engine.
        if is_new:
            try:
                settings = get_lead_growth_settings(ws_id)
            except Exception:
                logger.exception("[lead_action] settings fetch for new-lead side effects failed (best-effort)")
                settings = {}

            if settings.get("notify_enabled"):
                # (C) Notify the configured destinations from the OFFICIAL number.
                LeadActionService._notify_new_lead(
                    settings=settings,
                    lead_id=lead_id,
                    name=name,
                    phone=phone,
                )

            if settings.get("nurture_enabled"):
                # (D) Enroll the new lead into matching 'new_lead' drip campaigns.
                LeadActionService._nurture_new_lead(
                    ws_id=ws_id,
                    name=name,
                    phone=phone,
                    email=email,
                    company=company,
                )

        return (lead_id, is_new, resolved_stage_key)

    @staticmethod
    def _resolve_stage(ws_id: int, stage_key: str):
        """Return (pipeline_id, stage_id, resolved_stage_key) for the workspace's
        default pipeline. Falls back to the 'new' stage when the requested key is
        missing. Any value may be None if not resolvable."""
        try:
            pipeline = (
                db.session.query(CrmPipeline)
                .filter(CrmPipeline.workspace_id == ws_id, CrmPipeline.is_default.is_(True))
                .order_by(CrmPipeline.id.asc())
                .first()
            )
            if pipeline is None:
                pipeline = (
                    db.session.query(CrmPipeline)
                    .filter(CrmPipeline.workspace_id == ws_id)
                    .order_by(CrmPipeline.id.asc())
                    .first()
                )
            if pipeline is None:
                return None, None, stage_key

            def _stage_by_key(k: str):
                return (
                    db.session.query(CrmPipelineStage)
                    .filter(
                        CrmPipelineStage.pipeline_id == pipeline.id,
                        CrmPipelineStage.key == k,
                    )
                    .first()
                )

            stage = _stage_by_key(stage_key)
            resolved_key = stage_key
            if stage is None and stage_key != _DEFAULT_STAGE_KEY:
                stage = _stage_by_key(_DEFAULT_STAGE_KEY)
                resolved_key = _DEFAULT_STAGE_KEY

            stage_id = stage.id if stage is not None else None
            return pipeline.id, stage_id, resolved_key
        except Exception:
            logger.exception("[lead_action] stage resolution failed (best-effort)")
            return None, None, stage_key

    @staticmethod
    def _set_conversation_lead_ref(conversation_id: Any, lead_id: str) -> None:
        """Write whatsapp_conversations.lead_id (varchar) back-reference via raw SQL.

        The column exists at DB level (P0) but isn't declared on the ORM model, so
        we update it directly. Best-effort."""
        try:
            db.session.execute(
                text(
                    "UPDATE whatsapp_conversations SET lead_id = :lead_id "
                    "WHERE id = :cid"
                ),
                {"lead_id": str(lead_id), "cid": conversation_id},
            )
        except Exception:
            logger.exception("[lead_action] failed to set conversation.lead_id (best-effort)")

    @staticmethod
    def _write_activity(
        *,
        workspace_id: int,
        lead_id: str,
        is_new: bool,
        fields_changed: bool,
        stage_key: Optional[str],
        timestamp: datetime,
    ) -> None:
        """Insert a CRM `activities` row via raw SQL. Best-effort.

        The activities.type enum does NOT contain 'lead_created', so we always use
        the valid 'stage_change' value and describe creation vs. update in the
        title/description.

        Logs whenever the lead is new OR any material field (name/email/company/
        lead_type/stage) changed — not only on stage change.
        """
        try:
            title = "Lead created from WhatsApp" if is_new else "Lead updated from WhatsApp"
            if not is_new and not fields_changed:
                # No material change worth recording.
                return
            description = f"stage={stage_key}" if stage_key else None
            db.session.execute(
                text(
                    "INSERT INTO activities "
                    "(id, workspace_id, entity_type, entity_id, type, title, description, timestamp) "
                    "VALUES (:id, :ws, 'lead', :entity_id, 'stage_change', :title, :description, :ts)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "ws": workspace_id,
                    "entity_id": str(lead_id),
                    "title": title,
                    "description": description,
                    "ts": timestamp,
                },
            )
        except Exception:
            logger.exception("[lead_action] failed to write activity (best-effort)")

    # ── intent-aware lead growth (features A / C / D) ─────────────────────

    @staticmethod
    def _intent_stage_override(ws_id: int, message_text: str) -> Optional[str]:
        """Return the configured pipeline stage_key for the message's classified
        intent, or None when no override applies.

        Feature A. Only overrides the DEFAULT stage: classifies the inbound text,
        and if the workspace has an ``intent_rules`` mapping AND the classifier
        confidence clears ``confidence_threshold`` AND the top intent has a mapped
        stage, returns that stage_key. Best-effort: any failure returns None so the
        caller keeps the DEFAULT stage.
        """
        try:
            settings = get_lead_growth_settings(ws_id)
            intent_rules = settings.get("intent_rules") or {}
            if not isinstance(intent_rules, dict) or not intent_rules:
                return None

            classification = classify_lead_intent(message_text)
            intent = classification.get("intent")
            try:
                confidence = float(classification.get("confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            try:
                threshold = float(settings.get("confidence_threshold") or 0.0)
            except (TypeError, ValueError):
                threshold = 0.0

            if confidence < threshold:
                return None
            mapped = intent_rules.get(intent)
            if mapped:
                logger.info(
                    "[lead_action] intent override ws=%s intent=%s conf=%.3f -> stage=%s",
                    ws_id, intent, confidence, mapped,
                )
                return str(mapped).strip() or None
            return None
        except Exception:
            logger.exception("[lead_action] intent stage override failed (best-effort)")
            return None

    @staticmethod
    def _notify_new_lead(
        *,
        settings: Dict[str, Any],
        lead_id: str,
        name: Optional[str],
        phone: Optional[str],
    ) -> None:
        """Send the configured new-lead notification template from the OFFICIAL
        Sociovia number to each configured destination (feature C).

        Dispatch path: enqueue the shared ``send-notification`` job with
        ``workspace_id=None`` so the worker resolves the official WABA creds from
        env (WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_ACCESS_TOKEN — platform phone
        909603232241870). ``dispatch_internal_job`` runs the job inline when no Redis
        backend is configured, so this works in every environment.

        Idempotent per (lead, destination): the job carries an ``idempotency_key`` of
        ``lead-notify:{lead_id}:{dest}`` so a redelivery never double-sends. Strictly
        best-effort — never raises.
        """
        template = settings.get("notify_template")
        if not template:
            logger.info("[lead_action] notify_enabled but no notify_template configured — skipping")
            return

        destinations = settings.get("notify_destinations") or []
        if not isinstance(destinations, list) or not destinations:
            logger.info("[lead_action] notify_enabled but no notify_destinations configured — skipping")
            return

        try:
            from core.queue import dispatch_internal_job
        except Exception:
            logger.exception("[lead_action] notification dispatch unavailable (best-effort)")
            return

        # Template body params: [lead name, lead phone]. The configured template is
        # responsible for declaring matching body placeholders; if it has none these
        # are simply ignored by Meta.
        body_params = [str(name or phone or "New lead"), str(phone or "")]
        components = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in body_params],
            }
        ]

        for dest in destinations:
            dest_phone = normalize_phone_robust(str(dest)) or normalize_phone(str(dest)) or str(dest).strip()
            if not dest_phone:
                continue
            try:
                dispatch_internal_job(
                    "send-notification",
                    {
                        "to": dest_phone,
                        "template_name": template,
                        "language_code": "en_US",
                        "components": components,
                        # workspace_id=None => official env creds in the worker.
                        "workspace_id": None,
                        # Per-(lead,destination) idempotency.
                        "idempotency_key": f"lead-notify:{lead_id}:{dest_phone}",
                    },
                    source="lead_action.new_lead",
                )
                logger.info(
                    "[lead_action] new-lead notification dispatched lead=%s to=%s template=%s",
                    lead_id, dest_phone, template,
                )
            except Exception:
                logger.exception(
                    "[lead_action] new-lead notification dispatch failed lead=%s to=%s (best-effort)",
                    lead_id, dest_phone,
                )

    @staticmethod
    def _nurture_new_lead(
        *,
        ws_id: int,
        name: Optional[str],
        phone: Optional[str],
        email: Optional[str],
        company: Optional[str],
    ) -> None:
        """Enroll a newly-created lead into matching 'new_lead' drip campaigns
        (feature D) via the shared CRM enrollment entry point.

        Best-effort: import + call are guarded so a drip-side failure never affects
        the lead upsert or the inbound flow.
        """
        if not phone:
            return
        try:
            from .drip_routes import enroll_from_crm

            # commit=False: the nurture enrollments must NEVER commit the shared
            # session here. On the flow-engine NODE path the engine owns the outer
            # commit; on the discovery path maybe_discover_lead commits at the end.
            # Either way the flushed enrollments are persisted by the caller's commit.
            result = enroll_from_crm(
                "lead",
                {
                    "name": name,
                    "phone": phone,
                    "email": email,
                    "company": company,
                },
                int(ws_id),
                commit=False,
            )
            logger.info(
                "[lead_action] nurture enroll ws=%s phone=%s -> campaigns=%s",
                ws_id, phone, (result or {}).get("enrolled_campaigns"),
            )
        except Exception:
            logger.exception("[lead_action] nurture enroll failed ws=%s (best-effort)", ws_id)
