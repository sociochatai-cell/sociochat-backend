"""
Interactive Automation Execution Engine
========================================

Executes interactive automation flows when users send messages or click buttons.

Performance Optimizations:
- O(1) node/edge lookups via hashmaps (not O(N) linear scans)
- O(1) keyword trigger matching via keyword index
- Account cached once per engine instance (not per-method)
- No print() debug I/O — uses logger.debug() only where essential
- Phase timing logs for observability
"""

import logging
import os
import re
import threading

from . import input_node_handler
import time
from difflib import SequenceMatcher
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from typing import Dict, Any, Optional, Tuple, List
from sqlalchemy import func
from sqlalchemy.orm.attributes import flag_modified

from shared_models import db
from .visual_automation_models import WhatsAppVisualAutomation, WhatsAppConversationState
from .models import WhatsAppAccount, WhatsAppConversation, WhatsAppMessage
from .services import WhatsAppService
from .template_node_executor import TemplateNodeExecutor
from . import api_node_executor
from . import flow_variables
from .lead_action_service import LeadActionService
from .utils import normalize_phone
from notifications import notification_manager

logger = logging.getLogger(__name__)

_API_DEFAULT_CONTINUE_HANDLES = frozenset({"success", "output", "default"})

# ── Module-level automation trigger cache ──────────────────────────────
# Key: (account_id, workspace_id) → (trigger_index, expiry)
# trigger_index = {"keyword_map": {kw: automation}, "any_reply": automation, "exact_map": {text: automation}}
_TRIGGER_CACHE: Dict[tuple, tuple] = {}
_TRIGGER_CACHE_TTL = int(os.getenv("WHATSAPP_TRIGGER_CACHE_TTL", "5"))
_FLOW_CACHE: Dict[int, tuple] = {}
_FLOW_CACHE_TTL = int(os.getenv("WHATSAPP_FLOW_CACHE_TTL", "5"))
_TEMPLATE_CACHE: Dict[tuple, tuple] = {}
_TEMPLATE_CACHE_TTL = int(os.getenv("WHATSAPP_TEMPLATE_CACHE_TTL", "5"))
_SEEN_PHONE_CACHE: Dict[tuple, float] = {}
_SEEN_PHONE_CACHE_TTL = int(os.getenv("WHATSAPP_SEEN_PHONE_CACHE_TTL", "86400"))
_ACTIVE_STATE_ID_CACHE: Dict[tuple, tuple] = {}
_ACTIVE_STATE_CACHE_TTL = int(os.getenv("WHATSAPP_ACTIVE_STATE_CACHE_TTL", "300"))
_FUZZY_MATCH_THRESHOLD = float(os.getenv("WHATSAPP_INTERACTIVE_FUZZY_THRESHOLD", "0.70"))
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _trigger_toggle_bool(
    trigger_node_data: Optional[dict],
    trigger_config: Optional[dict],
    key: str,
) -> bool:
    """Read trigger toggles from flow node data and persisted trigger_config."""
    node_val = False
    if isinstance(trigger_node_data, dict):
        node_val = bool(trigger_node_data.get(key))
    cfg_val = bool((trigger_config or {}).get(key))
    return node_val or cfg_val


def _keyword_matches_message(msg_lower: str, kw_norm: str) -> bool:
    """
    Match keywords without accidental substring hits (e.g. 'hi' in 'this').

    Multi-word keywords use phrase containment; single tokens use word boundaries.
    """
    if not kw_norm or not msg_lower:
        return False
    if " " in kw_norm:
        return kw_norm in msg_lower
    return re.search(rf"\b{re.escape(kw_norm)}\b", msg_lower) is not None


def _seen_phone_cache_key(workspace_id: str, automation_id: int, phone_number: str) -> tuple:
    return (str(workspace_id), int(automation_id), str(phone_number))


def _active_state_cache_key(workspace_id: str, conversation_id: int) -> tuple:
    return (str(workspace_id), int(conversation_id))


def _get_cached_active_state_id(workspace_id: str, conversation_id: int) -> Optional[int]:
    cache_key = _active_state_cache_key(workspace_id, conversation_id)
    cached = _ACTIVE_STATE_ID_CACHE.get(cache_key)
    if not cached:
        return None

    state_id, expiry = cached
    if time.time() >= expiry:
        _ACTIVE_STATE_ID_CACHE.pop(cache_key, None)
        return None

    return int(state_id)


def _set_cached_active_state_id(workspace_id: str, conversation_id: int, state_id: int) -> None:
    _ACTIVE_STATE_ID_CACHE[_active_state_cache_key(workspace_id, conversation_id)] = (
        int(state_id),
        time.time() + _ACTIVE_STATE_CACHE_TTL,
    )


def _clear_cached_active_state_id(workspace_id: str, conversation_id: int) -> None:
    _ACTIVE_STATE_ID_CACHE.pop(_active_state_cache_key(workspace_id, conversation_id), None)


def _has_seen_phone_cache(workspace_id: str, automation_id: int, phone_numbers: List[str]) -> bool:
    now = time.time()
    for phone_number in phone_numbers:
        expiry = _SEEN_PHONE_CACHE.get(_seen_phone_cache_key(workspace_id, automation_id, phone_number))
        if expiry and now < expiry:
            return True
    return False


def _clear_seen_phone_cache_keys(workspace_id: str, automation_id: int, phone_numbers: List[str]) -> None:
    """Clear worker-local cache entries for exact workspace/automation/phone candidates."""
    if not phone_numbers:
        return

    workspace_id = str(workspace_id)
    automation_id = int(automation_id)
    phone_set = {str(phone_number) for phone_number in phone_numbers if phone_number}
    if not phone_set:
        return

    for cache_key in list(_SEEN_PHONE_CACHE.keys()):
        cached_workspace_id, cached_automation_id, cached_phone_number = cache_key
        if cached_workspace_id != workspace_id:
            continue
        if cached_automation_id != automation_id:
            continue
        if cached_phone_number not in phone_set:
            continue
        _SEEN_PHONE_CACHE.pop(cache_key, None)


def _mark_seen_phone_cache(workspace_id: str, automation_id: int, phone_numbers: List[str]) -> None:
    if not phone_numbers:
        return

    expiry = time.time() + _SEEN_PHONE_CACHE_TTL
    for phone_number in phone_numbers:
        _SEEN_PHONE_CACHE[_seen_phone_cache_key(workspace_id, automation_id, phone_number)] = expiry


def clear_seen_phone_cache(
    workspace_id: str,
    phone_numbers: List[str],
    automation_id: Optional[int] = None,
) -> int:
    """Forget worker-local first-message cache entries for a phone number."""
    if not phone_numbers:
        return 0

    workspace_id = str(workspace_id)
    phone_set = {str(phone_number) for phone_number in phone_numbers if phone_number}
    if not phone_set:
        return 0

    removed = 0
    for cache_key in list(_SEEN_PHONE_CACHE.keys()):
        cached_workspace_id, cached_automation_id, cached_phone_number = cache_key
        if cached_workspace_id != workspace_id:
            continue
        if automation_id is not None and cached_automation_id != int(automation_id):
            continue
        if cached_phone_number not in phone_set:
            continue

        _SEEN_PHONE_CACHE.pop(cache_key, None)
        removed += 1

    return removed


def invalidate_trigger_cache(workspace_id: str = None, account_id: int = None):
    """Invalidate trigger cache when automations change."""
    if workspace_id and account_id:
        _TRIGGER_CACHE.pop((account_id, str(workspace_id)), None)
    else:
        _TRIGGER_CACHE.clear()


def invalidate_flow_cache(automation_id: int = None):
    """Invalidate compiled flow cache when automations change."""
    if automation_id is not None:
        _FLOW_CACHE.pop(int(automation_id), None)
    else:
        _FLOW_CACHE.clear()


def invalidate_template_cache(workspace_id: str = None):
    """Invalidate cached templates when template records change."""
    if workspace_id is None:
        _TEMPLATE_CACHE.clear()
        return

    ws = str(workspace_id)
    for key in list(_TEMPLATE_CACHE.keys()):
        if key[0] == ws:
            _TEMPLATE_CACHE.pop(key, None)


class InteractiveAutomationEngine:
    """
    Executes interactive automation flows for WhatsApp conversations.

    Performance: Account is loaded once, node/edge maps built once per flow.
    """

    def __init__(self, account_id: int, workspace_id: str, account: Optional[Any] = None):
        self.account_id = account_id
        self.workspace_id = str(workspace_id)
        self._account: Optional[Any] = account  # Lazy-loaded, cached

    def _get_account(self) -> Optional[WhatsAppAccount]:
        """Load account once per engine instance. Cached for all methods."""
        if self._account is None:
            self._account = WhatsAppAccount.query.get(self.account_id)
        return self._account

    def _get_service(self) -> Optional[WhatsAppService]:
        """Get WhatsApp service using cached account."""
        account = self._get_account()
        if not account:
            logger.error(f"Account {self.account_id} not found")
            return None
        return WhatsAppService(
            phone_number_id=account.phone_number_id,
            access_token=account.get_access_token(),
        )

    @staticmethod
    def _automation_version_token(automation: WhatsAppVisualAutomation) -> str:
        updated_at = automation.updated_at.isoformat() if automation.updated_at else ""
        version = automation.version or 0
        return f"{version}:{updated_at}"

    def _record_trigger_hit(self, automation_id: int) -> None:
        """Increment trigger counters after the outbound send succeeds."""
        now = datetime.now(timezone.utc)
        db.session.query(WhatsAppVisualAutomation).filter_by(id=automation_id).update(
            {
                WhatsAppVisualAutomation.trigger_count: func.coalesce(WhatsAppVisualAutomation.trigger_count, 0) + 1,
                WhatsAppVisualAutomation.last_triggered_at: now,
            },
            synchronize_session=False,
        )

    def _get_compiled_flow(
        self,
        automation_id: Optional[int],
        version_token: Optional[str] = None,
        automation: Optional[WhatsAppVisualAutomation] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return a cached, precompiled flow graph for the automation."""
        if automation_id in (None, ""):
            return None

        try:
            automation_id_int = int(automation_id)
        except (TypeError, ValueError):
            logger.warning(f"[interactive_engine] Invalid automation_id for compiled flow: {automation_id}")
            return None

        cached = _FLOW_CACHE.get(automation_id_int)
        now = time.time()
        if cached and now < cached[2]:
            compiled_flow, cached_version, _ = cached
            if version_token is None or cached_version == version_token:
                return compiled_flow

        automation = automation or WhatsAppVisualAutomation.query.get(automation_id_int)
        if not automation:
            return None

        compiled_flow = self._compile_flow_data(automation)
        version_token = self._automation_version_token(automation)
        _FLOW_CACHE[automation_id_int] = (compiled_flow, version_token, now + _FLOW_CACHE_TTL)
        return compiled_flow

    # ── Node/Edge Hashmaps (O(1) lookups) ─────────────────────────────

    @staticmethod
    def _build_node_map(nodes: list) -> Dict[str, dict]:
        """Build {node_id: node_dict} for O(1) lookup."""
        return {n.get("id"): n for n in nodes if n.get("id")}

    @staticmethod
    def _build_edge_maps(edges: list) -> Tuple[Dict[str, list], Dict[str, str]]:
        """
        Build two edge lookup structures:
        - source_edges: {source_id: [edge, ...]} for finding outgoing edges
        - handle_edges: {sourceHandle: target_id} for button → target lookup
        """
        source_edges: Dict[str, list] = {}
        handle_edges: Dict[str, str] = {}
        for edge in edges:
            src = edge.get("source")
            if src:
                source_edges.setdefault(src, []).append(edge)
            handle = edge.get("sourceHandle")
            if handle:
                handle_edges[handle] = edge.get("target")
        return source_edges, handle_edges

    @staticmethod
    def _find_node_by_type(node_map: dict, node_type: str) -> Optional[dict]:
        """Find first node of a given type from the node map."""
        for node in node_map.values():
            if node.get("type") == node_type:
                return node
        return None

    def _compile_flow_data(self, automation: WhatsAppVisualAutomation) -> Dict[str, Any]:
        """Precompile the flow graph so runtime sends do minimal work."""
        nodes = automation.nodes or []
        edges = automation.edges or []
        node_map = self._build_node_map(nodes)
        source_edges, handle_edges = self._build_edge_maps(edges)

        trigger_node = self._find_node_by_type(node_map, "trigger")
        trigger_node_id = trigger_node.get("id") if trigger_node else None
        first_node_id = None

        if trigger_node_id:
            logger.debug(f"Trigger node found: {trigger_node_id}, outgoing edges: {source_edges.get(trigger_node_id, [])}")
            for edge in source_edges.get(trigger_node_id, []):
                target_id = edge.get("target")
                target = node_map.get(target_id)
                logger.debug(f"Edge target: {target_id}, target node: {target}")
                if target and target.get("type") in ("message", "template", "input", "api", "lead"):
                    first_node_id = target_id
                    logger.debug(f"Set first_node_id to {first_node_id}")
                    break

        logger.debug(f"Compiled flow result: first_node_id={first_node_id}")

        return {
            "automation_id": automation.id,
            "name": automation.name,
            "version_token": self._automation_version_token(automation),
            "variables": automation.variables if isinstance(automation.variables, dict) else {},
            "flow_config": automation.flow_config if isinstance(automation.flow_config, dict) else {},
            "node_map": node_map,
            "source_edges": source_edges,
            "handle_edges": handle_edges,
            "trigger_node_id": trigger_node_id,
            "first_node_id": first_node_id,
        }

    # ── Trigger Matching (O(1) keyword index) ─────────────────────────

    def _build_trigger_index(self) -> dict:
        """
        Build a trigger index for O(1) matching:
        - keyword_map: {keyword_lower: match_entry} for keyword triggers
        - exact_map: {text_lower: match_entry} for exact match triggers
        - first_message: first first_message match_entry (or None)
        - any_reply: first any_reply match_entry (or None)
        """
        automations = WhatsAppVisualAutomation.query.filter_by(
            account_id=self.account_id,
            workspace_id=self.workspace_id,
            is_active=True,
            status="active"
        ).all()

        keyword_map = {}
        keyword_contains: List[Tuple[str, Dict[str, Any]]] = []
        exact_map = {}
        first_message = None
        any_reply = None

        for automation in automations:
            compiled_flow = self._get_compiled_flow(
                automation.id,
                version_token=self._automation_version_token(automation),
                automation=automation,
            )
            if not compiled_flow or not compiled_flow.get("first_node_id"):
                logger.warning(f"Interactive automation {automation.id} has no startable first node")
                continue

            trigger_config = automation.trigger_config or {}

            # Use trigger node as source of truth when available because this is
            # what the flow editor updates directly.
            trigger_node = compiled_flow["node_map"].get(compiled_flow.get("trigger_node_id"))
            trigger_node_data = (trigger_node or {}).get("data", {}) or {}
            trigger_type = (
                trigger_node_data.get("triggerType")
                or trigger_config.get("type")
                or automation.trigger_type
            )
            if trigger_node_data.get("keywords"):
                trigger_config = {**trigger_config, "keywords": trigger_node_data["keywords"]}
            first_message_only = _trigger_toggle_bool(
                trigger_node_data, trigger_config, "firstMessageOnly"
            )
            one_time_only = _trigger_toggle_bool(trigger_node_data, trigger_config, "oneTimeOnly")

            match_entry = {
                "automation_id": automation.id,
                "name": automation.name,
                "version_token": compiled_flow["version_token"],
                "first_message_only": first_message_only,
                "one_time_only": one_time_only,
            }
            
            logger.debug(
                f"[trigger_index] Automation {automation.id} ({automation.name}): "
                f"type={trigger_type}, firstMessageOnly={first_message_only}, oneTimeOnly={one_time_only}"
            )

            if trigger_type == "any_reply" and any_reply is None:
                any_reply = match_entry
            elif trigger_type == "first_message" and first_message is None:
                first_message = match_entry
            elif trigger_type == "keyword":
                keywords = trigger_config.get("keywords", [])
                if isinstance(keywords, str):
                    keywords = [k.strip() for k in keywords.split(",")]
                for kw in keywords:
                    kw_norm = str(kw or "").strip().lower()
                    if not kw_norm:
                        continue
                    keyword_map[kw_norm] = match_entry
                    keyword_contains.append((kw_norm, match_entry))
            elif trigger_type == "exact_match":
                expected = trigger_config.get("message", "").lower()
                if expected:
                    exact_map[expected] = match_entry

        keyword_contains.sort(key=lambda item: len(item[0]), reverse=True)

        return {
            "keyword_map": keyword_map,
            "keyword_contains": keyword_contains,
            "exact_map": exact_map,
            "first_message": first_message,
            "any_reply": any_reply,
        }

    def _phone_candidates(self, phone_number: str) -> List[str]:
        canonical = normalize_phone(phone_number or "")
        candidates: List[str] = []

        if phone_number:
            candidates.append(str(phone_number))

        if canonical:
            candidates.append(canonical)
            if len(canonical) > 10:
                candidates.append(canonical[-10:])

        # Preserve order while removing duplicates
        return list(dict.fromkeys(candidates))

    def _is_first_inbound_from_unique_number(self, from_phone: str) -> bool:
        phone_candidates = self._phone_candidates(from_phone)
        if not phone_candidates:
            return False

        incoming_rows = (
            db.session.query(WhatsAppMessage.id)
            .join(WhatsAppConversation, WhatsAppConversation.id == WhatsAppMessage.conversation_id)
            .filter(
                WhatsAppConversation.account_id == self.account_id,
                WhatsAppConversation.user_phone.in_(phone_candidates),
                WhatsAppMessage.direction == "incoming",
            )
            .order_by(WhatsAppMessage.id.asc())
            .limit(2)
            .all()
        )

        # When routing happens before persistence, the current inbound message
        # is not stored yet, so the first message looks like count=0. After
        # persistence it looks like count=1. Both mean "first inbound".
        return len(incoming_rows) <= 1

    def _automation_already_seen_phone(self, automation_id: int, from_phone: str) -> bool:
        phone_candidates = self._phone_candidates(from_phone)
        if not phone_candidates:
            return False

        # Multi-worker safety: cache is process-local and can become stale after
        # conversation/state deletion handled by another worker. On cache hit,
        # always revalidate against DB before returning True.
        cache_hit = _has_seen_phone_cache(self.workspace_id, automation_id, phone_candidates)
        if cache_hit:
            existing_state = (
                WhatsAppConversationState.query.filter(
                    WhatsAppConversationState.workspace_id == self.workspace_id,
                    WhatsAppConversationState.automation_id == automation_id,
                    WhatsAppConversationState.phone_number.in_(phone_candidates),
                )
                .order_by(WhatsAppConversationState.id.desc())
                .first()
            )
            if existing_state is not None:
                return True

            _clear_seen_phone_cache_keys(self.workspace_id, automation_id, phone_candidates)
            logger.debug(
                "[firstMessageOnly] Cleared stale worker cache for automation=%s phone=%s",
                automation_id,
                from_phone,
            )
            return False

        existing_state = (
            WhatsAppConversationState.query.filter(
                WhatsAppConversationState.workspace_id == self.workspace_id,
                WhatsAppConversationState.automation_id == automation_id,
                WhatsAppConversationState.phone_number.in_(phone_candidates),
            )
            .order_by(WhatsAppConversationState.id.desc())
            .first()
        )
        if existing_state is not None:
            _mark_seen_phone_cache(self.workspace_id, automation_id, phone_candidates)
            return True

        return False

    def _get_trigger_index(self) -> dict:
        """Get cached trigger index, rebuild if expired."""
        key = (self.account_id, self.workspace_id)
        cached = _TRIGGER_CACHE.get(key)
        if cached and time.time() < cached[1]:
            return cached[0]

        index = self._build_trigger_index()
        _TRIGGER_CACHE[key] = (index, time.time() + _TRIGGER_CACHE_TTL)
        return index

    def _find_matching_automation(
        self,
        message_text: str,
        is_button_reply: bool,
        from_phone: Optional[str] = None,
        is_first_inbound: Optional[bool] = None,
        allow_restart_automation_id: Optional[int] = None,
        button_payload: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """O(1) automation matching via trigger index."""
        match = self._find_matching_automation_for_text(
            message_text,
            is_button_reply,
            from_phone=from_phone,
            is_first_inbound=is_first_inbound,
            allow_restart_automation_id=allow_restart_automation_id,
        )
        if match:
            return match
        payload_clean = str(button_payload or "").strip()
        if payload_clean and payload_clean.lower() != str(message_text or "").strip().lower():
            return self._find_matching_automation_for_text(
                payload_clean,
                is_button_reply,
                from_phone=from_phone,
                is_first_inbound=is_first_inbound,
                allow_restart_automation_id=allow_restart_automation_id,
            )
        return None

    def _find_matching_automation_for_text(
        self,
        message_text: str,
        is_button_reply: bool,
        from_phone: Optional[str] = None,
        is_first_inbound: Optional[bool] = None,
        allow_restart_automation_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Match a single inbound text/payload string against the trigger index."""
        index = self._get_trigger_index()
        msg_lower = (message_text or "").strip().lower()
        if not msg_lower:
            return None

        # Helper to check trigger restrictions (firstMessageOnly / oneTimeOnly)
        def _passes_trigger_restrictions(match: Dict[str, Any]) -> bool:
            """Returns True if match passes all trigger toggle checks."""
            restricted_by_first_message = bool(match.get("first_message_only"))
            restricted_by_one_time = bool(match.get("one_time_only"))

            if not restricted_by_first_message and not restricted_by_one_time:
                return True

            if not from_phone:
                logger.debug(
                    "[trigger_toggles] No phone number, failing check for automation %s",
                    match["automation_id"],
                )
                return False

            # oneTimeOnly: only once per automation+phone (never bypass for paused/active restarts).
            if restricted_by_one_time:
                already_seen = self._automation_already_seen_phone(match["automation_id"], from_phone)
                if already_seen:
                    logger.info(
                        "[oneTimeOnly] Automation %s already triggered for %s — skipping",
                        match["automation_id"],
                        from_phone,
                    )
                    return False

            # firstMessageOnly: require first inbound + not seen.
            if not restricted_by_first_message:
                return True

            first_inbound = is_first_inbound
            if first_inbound is None:
                first_inbound = self._is_first_inbound_from_unique_number(from_phone)

            if not first_inbound:
                logger.debug(f"[firstMessageOnly] Not first inbound for {from_phone}, failing check for automation {match['automation_id']}")
                return False

            # Check if automation has already seen this phone
            already_seen = self._automation_already_seen_phone(match["automation_id"], from_phone)
            if already_seen:
                logger.debug(f"[firstMessageOnly] Automation {match['automation_id']} already seen {from_phone}, failing check")
                return False

            logger.info(f"[firstMessageOnly] First message check PASSED for automation {match['automation_id']} from {from_phone}")
            return True

        # 1. Exact match
        exact = index["exact_map"].get(msg_lower)
        if exact and _passes_trigger_restrictions(exact):
            return exact

        # 2. Keyword match (word and phrase contains)
        tokens = re.findall(r"\w+", msg_lower)
        for word in tokens:
            match = index["keyword_map"].get(word)
            if match and _passes_trigger_restrictions(match):
                return match

        # Also check full text as keyword (for exact multi-word keywords)
        kw_match = index["keyword_map"].get(msg_lower)
        if kw_match and _passes_trigger_restrictions(kw_match):
            return kw_match

        # Contains fallback for multi-word keywords / punctuation variations.
        for keyword, match in index.get("keyword_contains", []):
            if _keyword_matches_message(msg_lower, keyword) and _passes_trigger_restrictions(match):
                return match

        # 3. First message from a unique number (legacy "first_message" trigger type)
        if not is_button_reply and from_phone and index["first_message"]:
            first_message_match = index["first_message"]
            first_inbound = is_first_inbound
            if first_inbound is None:
                first_inbound = self._is_first_inbound_from_unique_number(from_phone)
            if (
                first_inbound
                and
                not self._automation_already_seen_phone(first_message_match["automation_id"], from_phone)
            ):
                return first_message_match

        # 4. Any reply (not for button replies mid-flow)
        if not is_button_reply and index["any_reply"]:
            any_reply_match = index["any_reply"]
            if _passes_trigger_restrictions(any_reply_match):
                return any_reply_match

        return None

    # ── Template / Flow button helpers ────────────────────────────────

    @staticmethod
    def _template_has_flow_or_quick_reply_button(template) -> bool:
        if not template or not template.components:
            return False
        for comp in template.components:
            if comp.get("type") == "BUTTONS":
                for btn in comp.get("buttons", []):
                    if btn.get("type") in ("QUICK_REPLY", "FLOW"):
                        return True
        return False

    def _template_waits_for_user_input(self, template, raw_button_mappings) -> bool:
        """True when the automation should stay active after sending this template."""
        if isinstance(raw_button_mappings, list):
            for mapping in raw_button_mappings:
                if not isinstance(mapping, dict):
                    continue
                btn_type = str(mapping.get("buttonType") or mapping.get("button_type") or "").lower()
                target = mapping.get("targetNodeId") or mapping.get("target_node_id")
                if btn_type in ("quick_reply", "flow") or target:
                    return True
        elif isinstance(raw_button_mappings, dict) and any(raw_button_mappings.values()):
            return True
        return self._template_has_flow_or_quick_reply_button(template)

    def _resolve_template_flow_next_node(
        self,
        current_node: Dict[str, Any],
        current_node_id: str,
        source_edges: dict,
        handle_edges: dict,
    ) -> Optional[str]:
        """After a WhatsApp Flow form submit, find the next automation node."""
        node_data = current_node.get("data", {})
        raw_mappings = node_data.get("button_mappings") or node_data.get("buttonMappings") or []

        if isinstance(raw_mappings, list):
            for mapping in raw_mappings:
                if not isinstance(mapping, dict):
                    continue
                target = mapping.get("targetNodeId") or mapping.get("target_node_id")
                if not target:
                    continue
                btn_type = str(mapping.get("buttonType") or mapping.get("button_type") or "").lower()
                if btn_type in ("flow", "quick_reply", "phone", "url", ""):
                    return target
                btn_index = mapping.get("buttonIndex")
                if btn_index is not None and handle_edges.get(f"btn-{btn_index}"):
                    return handle_edges.get(f"btn-{btn_index}")

        for key in ("btn-0", "0", "button_0"):
            scoped = handle_edges.get(f"{current_node_id}:{key}")
            if scoped:
                return scoped
            if handle_edges.get(key):
                return handle_edges.get(key)

        for edge in source_edges.get(current_node_id, []):
            handle = edge.get("sourceHandle") or ""
            if handle.startswith("btn-"):
                target = edge.get("target")
                if target:
                    return target

        for edge in source_edges.get(current_node_id, []):
            handle = edge.get("sourceHandle")
            if not handle or handle in ("output", "default"):
                return edge.get("target")

        return None

    def _recover_state_for_flow_reply(
        self, conversation_id: int
    ) -> Optional[WhatsAppConversationState]:
        """Reattach flow state when nfm_reply arrives after a premature completion."""
        try:
            states = (
                WhatsAppConversationState.query.filter_by(
                    conversation_id=conversation_id,
                    workspace_id=self.workspace_id,
                )
                .order_by(WhatsAppConversationState.updated_at.desc())
                .limit(8)
                .all()
            )
            now = datetime.now(timezone.utc)
            for state in states:
                if not state.automation_id or not state.current_node_id:
                    continue
                compiled_flow = self._get_compiled_flow(state.automation_id)
                if not compiled_flow:
                    continue
                current_node = compiled_flow["node_map"].get(state.current_node_id)
                if not current_node or current_node.get("type") != "template":
                    continue

                if state.is_active:
                    return state

                # Skip SWEPT (abandoned + torn down) states — they carry completed_at for the
                # resume guard but must not be recovered for a late flow-reply.
                if (state.state_data or {}).get("swept_stale"):
                    continue

                # Paused flows no longer stamp completed_at (reserved for genuine
                # completions); fall back to updated_at so a paused/interrupted flow is
                # still recoverable within the 24h window.
                recency = state.completed_at or state.updated_at
                if not recency:
                    continue
                if recency.tzinfo is None:
                    recency = recency.replace(tzinfo=timezone.utc)
                age_hours = (now - recency).total_seconds() / 3600
                if age_hours > 24:
                    continue

                state.is_active = True
                state.completed_at = None
                state.updated_at = now
                logger.info(
                    "[interactive_engine] Recovered template flow state=%s for nfm_reply "
                    "conversation=%s automation=%s",
                    state.id,
                    conversation_id,
                    state.automation_id,
                )
                return state
        except Exception:
            db.session.rollback()
        return None

    # ── Main Entry Point ──────────────────────────────────────────────

    def process_incoming_message(
        self,
        message_text: str,
        conversation_id: int,
        from_phone: str,
        is_button_reply: bool = False,
        button_payload: Optional[str] = None,
        is_first_inbound: Optional[bool] = None,
        inbound_wamid: Optional[str] = None,
        is_flow_reply: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Process an incoming message against interactive automations."""
        t_start = time.perf_counter()

        # Reset the per-inbound-message lead-hook de-dup guard. The button-capture
        # path can invoke _maybe_mark_lead several times for the same node+payload
        # within a single inbound message (api re-resolution, button action, default
        # continuation); track (node_id, payload) here so the hook fires at most once.
        self._lead_hook_fired = set()
        # Per-inbound guard against cycles through dedicated 'lead' pass-through nodes.
        self._lead_nodes_visited = set()

        try:
            preview_limit = 30
            message_preview = message_text if len(message_text) <= preview_limit else f"{message_text[:preview_limit]}..."
            logger.info(
                f"[interactive_engine] Processing message: text='{message_preview}' "
                f"from={from_phone}, flow_context={'button_reply' if is_button_reply else 'new_message'}"
            )
            
            # Check for active conversation state (mid-flow)
            t_state_lookup = time.perf_counter()
            active_state = self._get_active_conversation_state(conversation_id)
            state_lookup_ms = (time.perf_counter() - t_state_lookup) * 1000

            if active_state:
                compiled = self._get_compiled_flow(active_state.automation_id) or {}
                stuck_end = (compiled.get("node_map") or {}).get(active_state.current_node_id)
                if stuck_end and stuck_end.get("type") == "end":
                    self._mark_flow_completed(active_state)
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    active_state = None

            if is_flow_reply and not active_state:
                active_state = self._recover_state_for_flow_reply(conversation_id)
                if active_state:
                    _set_cached_active_state_id(
                        self.workspace_id, conversation_id, active_state.id
                    )

            if is_button_reply:
                target_state = self._select_button_reply_state(
                    active_state,
                    conversation_id,
                    message_text,
                    button_payload,
                )
                if target_state is not None and target_state.id != (active_state.id if active_state else None):
                    target_state.is_active = True
                    target_state.completed_at = None
                    target_state.updated_at = datetime.now(timezone.utc)
                    db.session.commit()
                    _set_cached_active_state_id(self.workspace_id, conversation_id, target_state.id)
                    logger.info(
                        "[interactiveResume] ✅ AUTO-RESUMED matching paused flow via button click: state=%s conversation=%s button_text='%s'",
                        target_state.id,
                        conversation_id,
                        message_text,
                    )
                    return self._handle_flow_continuation(
                        target_state,
                        message_text,
                        from_phone,
                        is_button_reply,
                        button_payload,
                        inbound_wamid=inbound_wamid,
                    )

            if active_state:
                if is_flow_reply:
                    result = self._handle_flow_continuation(
                        active_state,
                        message_text,
                        from_phone,
                        is_button_reply,
                        button_payload,
                        inbound_wamid=inbound_wamid,
                        is_flow_reply=True,
                    )
                    t_total = (time.perf_counter() - t_start) * 1000
                    logger.info(
                        f"[interactive_engine] Flow form continuation in {t_total:.0f}ms "
                        f"(state_lookup={state_lookup_ms:.0f}ms)"
                    )
                    return result

                if is_button_reply:
                    # Always try mid-flow continuation first. API quickReplies often use
                    # labels like "American Library" that contain trigger keywords
                    # (e.g. "library") and must not restart the active automation.
                    result = self._handle_flow_continuation(
                        active_state,
                        message_text,
                        from_phone,
                        is_button_reply,
                        button_payload,
                        inbound_wamid=inbound_wamid,
                    )
                    if result and not result.get("clarification"):
                        t_total = (time.perf_counter() - t_start) * 1000
                        logger.info(
                            f"[interactive_engine] Flow continuation in {t_total:.0f}ms "
                            f"(state_lookup={state_lookup_ms:.0f}ms)"
                        )
                        return result

                    # Keyword/exact triggers on button labels (template QR outside API picks).
                    trigger_match = self._find_matching_automation(
                        message_text,
                        is_button_reply,
                        from_phone=from_phone,
                        is_first_inbound=is_first_inbound,
                        button_payload=button_payload,
                    )
                    if trigger_match and not (
                        trigger_match.get("one_time_only")
                        and self._automation_already_seen_phone(
                            trigger_match["automation_id"], from_phone
                        )
                    ):
                        self._stop_active_state_for_restart(active_state)
                        db.session.commit()
                        logger.info(
                            "[interactive_engine] Button reply matched trigger; restarted automation=%s (was state=%s)",
                            trigger_match.get("automation_id"),
                            active_state.id,
                        )
                        return self._start_automation_flow(
                            trigger_match, conversation_id, from_phone, trigger_message=message_text
                        )

                    if result:
                        t_total = (time.perf_counter() - t_start) * 1000
                        logger.info(
                            f"[interactive_engine] Flow clarification in {t_total:.0f}ms "
                            f"(state_lookup={state_lookup_ms:.0f}ms)"
                        )
                        return result
                    t_total = (time.perf_counter() - t_start) * 1000
                    logger.info(
                        f"[interactive_engine] Flow continuation in {t_total:.0f}ms "
                        f"(state_lookup={state_lookup_ms:.0f}ms)"
                    )
                    return result

                # ── STRICT INPUT LOCK (before trigger index) ───────────
                # Skip expensive _find_matching_automation while collecting
                # free text; also do not allow a trigger keyword to preempt input.
                if active_state.is_waiting_for_input:
                    logger.info(
                        "[interactive_engine] Input lock active for state=%s field=%s",
                        active_state.id,
                        active_state.current_input_field,
                    )
                    result = self._handle_flow_continuation(
                        active_state,
                        message_text,
                        from_phone,
                        False,
                        None,
                        inbound_wamid=inbound_wamid,
                    )
                    t_total = (time.perf_counter() - t_start) * 1000
                    logger.info(
                        f"[interactive_engine] Input lock continuation in {t_total:.0f}ms "
                        f"(state_lookup={state_lookup_ms:.0f}ms)"
                    )
                    return result

                trigger_match = self._find_matching_automation(
                    message_text,
                    is_button_reply,
                    from_phone=from_phone,
                    is_first_inbound=is_first_inbound,
                    button_payload=button_payload,
                )

                # Text mid-flow: try continuation before keyword restart (mirrors button path).
                result = self._handle_flow_continuation(
                    active_state,
                    message_text,
                    from_phone,
                    is_button_reply,
                    button_payload,
                    inbound_wamid=inbound_wamid,
                )
                if result is not None:
                    if (
                        result.get("clarification")
                        and trigger_match
                        and int(trigger_match.get("automation_id") or 0)
                        == int(active_state.automation_id or 0)
                        and not (
                            trigger_match.get("one_time_only")
                            and self._automation_already_seen_phone(
                                trigger_match["automation_id"], from_phone
                            )
                        )
                    ):
                        self._stop_active_state_for_restart(active_state)
                        db.session.commit()
                        logger.info(
                            "[interactive_engine] Keyword restart after clarification "
                            "state=%s automation=%s keyword='%s'",
                            active_state.id,
                            trigger_match.get("automation_id"),
                            message_text[:40],
                        )
                        return self._start_automation_flow(
                            trigger_match, conversation_id, from_phone, trigger_message=message_text
                        )
                    t_total = (time.perf_counter() - t_start) * 1000
                    logger.info(
                        f"[interactive_engine] Flow continuation in {t_total:.0f}ms "
                        f"(state_lookup={state_lookup_ms:.0f}ms)"
                    )
                    return result

                if (
                    trigger_match
                    and not self._is_offscript_question(message_text, active_state)
                    and not (
                        trigger_match.get("one_time_only")
                        and self._automation_already_seen_phone(
                            trigger_match["automation_id"], from_phone
                        )
                    )
                ):
                    self._stop_active_state_for_restart(active_state)
                    db.session.commit()
                    logger.info(
                        "[interactive_engine] Stopped active flow state=%s and restarted automation=%s",
                        active_state.id,
                        trigger_match.get("automation_id"),
                    )
                    return self._start_automation_flow(
                        trigger_match, conversation_id, from_phone, trigger_message=message_text
                    )
                elif trigger_match and self._is_offscript_question(message_text, active_state):
                    # Off-script QUESTION that happens to contain another automation's trigger word:
                    # do NOT hijack the active flow. The continuation already paused it for AI/FAQ;
                    # keep it paused + resumable instead of restarting a different automation.
                    logger.info(
                        "[interactive_engine] Off-script question matched trigger automation=%s but "
                        "keeping the active flow (state=%s) — answering, not restarting",
                        trigger_match.get("automation_id"), active_state.id,
                    )
                elif trigger_match:
                    logger.info(
                        "[oneTimeOnly] Ignoring keyword restart while active flow state=%s automation=%s",
                        active_state.id,
                        trigger_match.get("automation_id"),
                    )

                t_total = (time.perf_counter() - t_start) * 1000
                logger.info(
                    f"[interactive_engine] Active flow had no handler in {t_total:.0f}ms "
                    f"(state_lookup={state_lookup_ms:.0f}ms)"
                )
                return None

            # ROBUST RESUME (takes priority): an explicit "resume"/"continue"/"thanks" returns the
            # user to their flow even if a long AI digression cleared/superseded the paused state.
            if not is_button_reply and (
                self._looks_like_flow_resume_request(message_text)
                or self._is_satisfaction_ack(message_text)
            ) and self._get_resumable_conversation_state(conversation_id) is not None:
                logger.warning(
                    "[interactiveResume] ▶️ Resume/ack recovering flow conversation=%s text=%r",
                    conversation_id, message_text,
                )
                recovered = self.resume_paused_step(conversation_id, from_phone)
                if recovered.get("resumed"):
                    return recovered

            # No active state: if we previously paused this conversation, allow resume.
            paused_state = self._get_paused_conversation_state(conversation_id)
            if paused_state:
                if is_button_reply:
                    target_state = self._select_button_reply_state(
                        None,
                        conversation_id,
                        message_text,
                        button_payload,
                    )
                    if target_state:
                        target_state.is_active = True
                        target_state.completed_at = None
                        target_state.updated_at = datetime.now(timezone.utc)
                        db.session.commit()
                        _set_cached_active_state_id(self.workspace_id, conversation_id, target_state.id)
                        logger.info(
                            "[interactiveResume] ✅ AUTO-RESUMED paused flow via button click: state=%s conversation=%s button_text='%s'",
                            target_state.id,
                            conversation_id,
                            message_text,
                        )
                        return self._handle_flow_continuation(
                            target_state,
                            message_text,
                            from_phone,
                            is_button_reply,
                            button_payload,
                            inbound_wamid=inbound_wamid,
                        )

                    restart_match = self._find_matching_automation(
                        message_text,
                        is_button_reply,
                        from_phone=from_phone,
                        is_first_inbound=is_first_inbound,
                        button_payload=button_payload,
                    )
                    if restart_match and not (
                        restart_match.get("one_time_only")
                        and self._automation_already_seen_phone(
                            restart_match["automation_id"], from_phone
                        )
                    ):
                        self._stop_paused_state_for_restart(paused_state)
                        db.session.commit()
                        logger.info(
                            "[interactive_engine] Button reply matched trigger; restarted automation=%s from paused state=%s",
                            restart_match.get("automation_id"),
                            paused_state.id,
                        )
                        return self._start_automation_flow(
                            restart_match, conversation_id, from_phone, trigger_message=message_text
                        )

                    logger.debug(
                        "[interactive_engine] Paused flow exists for conversation %s, but button reply did not match the paused state or a trigger",
                        conversation_id,
                    )
                    return None
                
                # Text-based resume: an explicit resume keyword OR a satisfaction ack ("thanks",
                # "got it") signalling the off-script query is resolved. Re-ask the step the user
                # left off on (resume_paused_step) instead of forwarding the keyword into
                # _handle_flow_continuation, which would advance/skip past that step.
                if self._looks_like_flow_resume_request(message_text) or self._is_satisfaction_ack(message_text):
                    logger.warning(
                        "[interactiveResume] ▶️ User signalled resume: state=%s conversation=%s text=%r",
                        paused_state.id,
                        conversation_id,
                        message_text,
                    )
                    return self.resume_paused_step(conversation_id, from_phone)

                # A flow that was paused for an OFF-SCRIPT query (the user stepped away mid-flow to
                # ask something) must SURVIVE the whole digression — never let an intervening
                # message stop/restart it. Keep it paused and let FAQ/AI answer; only an explicit
                # resume/satisfaction (handled above) brings it back. This is what lets the user
                # chat freely (demo Q&A, etc.) and still "resume" to exactly where they left off.
                paused_data = paused_state.state_data if isinstance(paused_state.state_data, dict) else {}
                paused_offscript = paused_data.get("pause_reason") in ("new_query", "new_query_during_input")

                # Keyword restart while paused — blocked for oneTimeOnly automations.
                restart_match = self._find_matching_automation(
                    message_text,
                    is_button_reply,
                    from_phone=from_phone,
                    is_first_inbound=is_first_inbound,
                    allow_restart_automation_id=paused_state.automation_id,
                    button_payload=button_payload,
                )
                if (
                    restart_match
                    and not paused_offscript
                    and not self._is_offscript_question(message_text, paused_state)
                    and not (
                        restart_match.get("one_time_only")
                        and self._automation_already_seen_phone(
                            restart_match["automation_id"], from_phone
                        )
                    )
                ):
                    self._stop_paused_state_for_restart(paused_state)
                    db.session.commit()
                    logger.info(
                        "[interactive_engine] Stopped paused state=%s and restarted automation=%s",
                        paused_state.id,
                        restart_match.get("automation_id"),
                    )
                    return self._start_automation_flow(
                        restart_match, conversation_id, from_phone, trigger_message=message_text
                    )
                elif restart_match and (paused_offscript or self._is_offscript_question(message_text, paused_state)):
                    # Off-script digression (question, or any message while an off-script-paused
                    # flow is alive): keep the flow paused and let FAQ/AI answer, so the user can
                    # still resume later. Do NOT stop the paused flow.
                    logger.warning(
                        "[interactive_engine] Keeping off-script-paused flow alive (state=%s, "
                        "matched automation=%s, text=%r) — answering, not restarting",
                        paused_state.id, restart_match.get("automation_id"), message_text,
                    )
                elif restart_match:
                    logger.info(
                        "[oneTimeOnly] Ignoring keyword restart while paused state=%s automation=%s",
                        paused_state.id,
                        restart_match.get("automation_id"),
                    )

                # User is on a different topic: keep paused and let normal automations/AI continue.
                logger.debug(
                    "[interactive_engine] Paused flow exists for conversation %s, allowing fallback routing",
                    conversation_id,
                )
                return None

            if is_flow_reply:
                logger.warning(
                    "[interactive_engine] nfm_reply without recoverable flow state conversation=%s",
                    conversation_id,
                )
                return {"success": False, "reason": "flow_reply_no_state"}

            # No active state — find matching automation
            t_match = time.perf_counter()
            automation_match = self._find_matching_automation(
                message_text,
                is_button_reply,
                from_phone=from_phone,
                is_first_inbound=is_first_inbound,
                button_payload=button_payload,
            )
            match_ms = (time.perf_counter() - t_match) * 1000

            if automation_match:
                logger.info(
                    f"[interactive_engine] Matched automation '{automation_match['name']}' "
                    f"(id={automation_match['automation_id']}) in {match_ms:.0f}ms"
                )
                try:
                    result = self._start_automation_flow(
                        automation_match, conversation_id, from_phone, trigger_message=message_text
                    )
                except Exception as e:
                    logger.exception(
                        "[interactive_engine] Matched automation failed after claiming input: %s",
                        e,
                    )
                    # Return non-None so the router does not execute a second engine path.
                    return {
                        "success": False,
                        "matched": True,
                        "automation_id": automation_match.get("automation_id"),
                        "error": str(e),
                    }
                t_total = (time.perf_counter() - t_start) * 1000
                logger.info(
                    f"[interactive_engine] Total: {t_total:.0f}ms "
                    f"(state_lookup={state_lookup_ms:.0f}ms, match={match_ms:.0f}ms)"
                )
                return result

            followup = self._handle_recent_completed_flow_followup(
                conversation_id,
                message_text,
                from_phone,
            )
            if followup is not None:
                t_total = (time.perf_counter() - t_start) * 1000
                logger.info(
                    "[interactive_engine] Post-flow follow-up handled in %.0fms",
                    t_total,
                )
                return followup

            logger.debug(f"[interactive_engine] No matching automation for '{message_text[:50]}'")
            return None

        except Exception as e:
            logger.exception(f"Interactive automation error (non-fatal): {e}")
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

    def _get_paused_conversation_state(self, conversation_id: int) -> Optional[WhatsAppConversationState]:
        """Get most recent explicitly paused state — never resurrect completed flows."""
        try:
            states = self._get_paused_conversation_states(conversation_id)
            for state in states:
                state_data = state.state_data if isinstance(state.state_data, dict) else {}
                if state_data.get("paused"):
                    return state
            return None
        except Exception:
            db.session.rollback()
            return None

    def _get_resumable_conversation_state(self, conversation_id: int) -> Optional[WhatsAppConversationState]:
        """A flow state the user can be returned to on an explicit 'resume'/'thanks'. Prefers a
        truly paused state, but ALSO recovers a recently inactive/stopped flow (within 12h) that
        still has a current node + automation — so a long AI digression that cleared the paused
        flag doesn't strand the user. Used by the resume path only (never auto)."""
        try:
            from datetime import timedelta
            paused = self._get_paused_conversation_state(conversation_id)
            if paused:
                return paused
            cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
            candidates = (
                WhatsAppConversationState.query.filter_by(
                    conversation_id=conversation_id,
                    workspace_id=self.workspace_id,
                    is_active=False,
                )
                .order_by(WhatsAppConversationState.updated_at.desc())
                .limit(5)
                .all()
            )
            for st in candidates:
                if st.automation_id is None or not st.current_node_id:
                    continue
                # Never resurrect a genuinely COMPLETED or explicitly STOPPED flow on a
                # casual ack ('thanks'/'ok') — only recover a paused/interrupted flow whose
                # paused flag may have been cleared during a long AI digression. Paused flows
                # no longer carry completed_at, so a set completed_at == a real completion.
                _sd = st.state_data if isinstance(st.state_data, dict) else {}
                if st.completed_at is not None or _sd.get("stopped"):
                    continue
                upd = st.updated_at
                if upd is not None and upd.tzinfo is None:
                    upd = upd.replace(tzinfo=timezone.utc)
                if upd is not None and upd < cutoff:
                    continue
                return st
            return None
        except Exception:
            db.session.rollback()
            return None

    def get_paused_flow_hint(self, conversation_id: int) -> Optional[str]:
        """If an interactive flow is paused for an off-script query, return a short human-readable
        description of where the user is (flow name + the step's question) so the AI answer can be
        flow-aware. Returns None when the conversation is not paused mid-flow. Non-fatal."""
        try:
            state = self._get_paused_conversation_state(conversation_id)
            if not state or state.automation_id is None:
                return None
            sd = state.state_data if isinstance(state.state_data, dict) else {}
            if sd.get("pause_reason") not in ("new_query", "new_query_during_input"):
                return None
            compiled_flow = self._get_compiled_flow(state.automation_id)
            if not compiled_flow:
                return None
            flow_name = compiled_flow.get("name") or "an automated flow"
            node = (compiled_flow.get("node_map") or {}).get(sd.get("paused_node_id") or state.current_node_id)
            step_q = ""
            if node:
                node_data = node.get("data") or {}
                if node.get("type") == "input":
                    try:
                        step_q = input_node_handler.build_question_message(node_data, state.get_collected_fields())
                    except Exception:
                        step_q = node_data.get("body") or ""
                else:
                    step_q = node_data.get("body") or self._build_flow_clarification_text(node, state=state)
            step_q = " ".join((step_q or "").split())
            if len(step_q) > 200:
                step_q = step_q[:200] + "…"
            hint = f'The user is in the middle of the "{flow_name}" flow and stepped away to ask the current question.'
            if step_q:
                hint += f' The flow step they paused on was asking: "{step_q}".'
            return hint
        except Exception:
            try:
                db.session.rollback()
            except Exception:
                pass
            return None

    def offer_resume_after_off_script(self, conversation_id: int, to_phone: str) -> Dict[str, Any]:
        """After an off-script (FAQ/AI) answer has been sent, send a ONE-TIME nudge inviting the
        user to resume the flow when they're satisfied — and KEEP the flow paused (do NOT auto
        re-activate). The user resumes by replying a resume keyword (handled in the router). No-op
        unless the flow is paused for an off-script query, and only nudges once per pause."""
        try:
            state = self._get_paused_conversation_state(conversation_id)
            if not state or state.automation_id is None:
                return {"offered": False, "reason": "no_paused_state"}

            state_data = state.state_data if isinstance(state.state_data, dict) else {}
            if state_data.get("pause_reason") not in ("new_query", "new_query_during_input"):
                return {"offered": False, "reason": "not_off_script_pause"}
            if state_data.get("resume_nudge_sent"):
                return {"offered": False, "reason": "nudge_already_sent"}

            compiled_flow = self._get_compiled_flow(state.automation_id)
            flow_name = (compiled_flow or {}).get("name") or "your previous step"

            nudge = (
                f'By the way — you paused *{flow_name}* to ask this. '
                f'Whenever you\'re ready, reply *resume* (or *continue*) to pick up where you left off. 👍'
            )
            send_result = self._send_text_message(to_phone, nudge, conversation_id)
            ok = bool(send_result.get("success"))
            if ok:
                # Keep the flow PAUSED; just remember we already offered so we don't nag.
                state_data["resume_nudge_sent"] = True
                state_data["resume_nudge_at"] = datetime.now(timezone.utc).isoformat()
                state.state_data = state_data
                flag_modified(state, "state_data")
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    return {"offered": False, "reason": "commit_failed"}
                logger.info(
                    "[interactiveResume] 💬 Offered resume after off-script answer: state=%s conversation=%s flow=%s",
                    state.id, conversation_id, flow_name,
                )
            else:
                db.session.rollback()
            return {"offered": ok}
        except Exception:
            logger.exception("[interactiveResume] offer_resume_after_off_script failed")
            try:
                db.session.rollback()
            except Exception:
                pass
            return {"offered": False, "reason": "exception"}

    def resume_paused_step(self, conversation_id: int, to_phone: str) -> Dict[str, Any]:
        """Resume a paused flow ON THE STEP IT LEFT OFF and re-ask that step ("you left here: …"),
        then re-activate so the user's next reply continues the flow. Called when the user signals
        they're satisfied/done with their off-script query (resume keyword or satisfaction ack)."""
        try:
            # resume can auto-chain into a downstream lead node — reset the per-flow-advance
            # lead guards so a reused engine instance doesn't carry stale visited/fired sets.
            self._lead_nodes_visited = set()
            self._lead_hook_fired = set()
            state = self._get_resumable_conversation_state(conversation_id)
            if not state or state.automation_id is None:
                return {"resumed": False, "reason": "no_paused_state"}

            state_data = state.state_data if isinstance(state.state_data, dict) else {}

            compiled_flow = self._get_compiled_flow(state.automation_id)
            if not compiled_flow:
                return {"resumed": False, "reason": "no_flow"}
            node_map = compiled_flow["node_map"]
            source_edges = compiled_flow["source_edges"]
            handle_edges = compiled_flow["handle_edges"]

            node_id = state_data.get("paused_node_id") or state.current_node_id
            node = node_map.get(node_id)
            if not node:
                return {"resumed": False, "reason": "node_missing"}

            node_data = node.get("data") or {}
            node_type = node.get("type")
            has_interactive = bool(node_data.get("buttons")) or bool(node_data.get("sections"))

            # Re-activate the flow on the SAME node so the next reply routes back into it.
            state.is_active = True
            state.completed_at = None
            state.advance_to_node(node_id)
            state_data["paused"] = False
            state_data["resumed_via"] = "user_resume"
            state_data.pop("resume_nudge_sent", None)  # allow a fresh offer if they pause again later
            state_data.pop("resume_nudge_at", None)
            state.state_data = state_data
            flag_modified(state, "state_data")
            if node_type == "input":
                state.set_waiting_for_input(node_data.get("field", "input"))
            state.last_user_message_at = datetime.now(timezone.utc)
            state.updated_at = datetime.now(timezone.utc)

            # Persist the re-activation BEFORE rendering — the api/template executors read the
            # live, active state from the DB.
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                return {"resumed": False, "reason": "commit_failed"}
            _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)

            # Re-render the step with the SAME renderer the flow uses for this node TYPE, so a
            # dynamic template / carousel (api node) comes back as the actual cards — not the
            # text "did you mean 1/2/3" fallback.
            self._send_text_message(to_phone, "Great — let's pick up where we left off. 👇", conversation_id)
            if node_type == "api":
                send_result = self._execute_api_node(
                    state=state, api_node=node, from_phone=to_phone,
                    node_map=node_map, source_edges=source_edges, handle_edges=handle_edges,
                )
            elif node_type == "template":
                send_result = self._send_template_node(
                    state.automation_id, node, to_phone, state,
                    node_map, source_edges, handle_edges,
                )
            elif node_type == "input":
                # Input nodes carry their OWN question; do NOT walk upstream (re-sends the menu).
                send_result = self._send_text_message(
                    to_phone,
                    input_node_handler.build_question_message(node_data, state.get_collected_fields()),
                    conversation_id,
                )
            elif has_interactive or node_type == "message":
                send_result = self._send_node_message(
                    state.automation_id, node, to_phone, state,
                    node_map=node_map, source_edges=source_edges, handle_edges=handle_edges,
                )
            else:
                send_result = self._send_text_message(
                    to_phone, self._build_flow_clarification_text(node, state=state), conversation_id
                )

            ok = bool(send_result.get("success")) if isinstance(send_result, dict) else bool(send_result)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            logger.warning(
                "[interactiveResume] %s RESUMED + re-rendered %s node: state=%s conversation=%s node=%s",
                "✅" if ok else "⚠️", node_type, state.id, conversation_id, node_id,
            )
            return {"resumed": ok, "node_id": node_id}
        except Exception:
            logger.exception("[interactiveResume] resume_paused_step failed")
            try:
                db.session.rollback()
            except Exception:
                pass
            return {"resumed": False, "reason": "exception"}

    def _mark_flow_completed(self, state: WhatsAppConversationState) -> None:
        """Complete flow state and drop worker cache so follow-up text does not resume old nodes."""
        # ── Flow→Lead hook (END node) ──
        # When the flow completes ON an end node that carries an enabled leadAction, mark the
        # lead with the last collected fields. Best-effort + savepoint-isolated + per-inbound
        # deduped inside _maybe_mark_lead; no-op for non-end completions (api/lead dangling) or
        # end nodes without an enabled leadAction. Runs BEFORE state.complete() so collected
        # fields are still intact.
        try:
            if state.automation_id is not None and state.current_node_id:
                compiled_flow = self._get_compiled_flow(state.automation_id)
                end_node = (compiled_flow or {}).get("node_map", {}).get(state.current_node_id)
                if end_node and end_node.get("type") == "end":
                    end_node_data = dict(end_node.get("data") or {})
                    lead_action = end_node_data.get("leadAction")
                    if isinstance(lead_action, dict) and lead_action.get("enabled"):
                        end_node_data["id"] = end_node.get("id")
                        collected = state.get_collected_fields() or {}
                        # captured_value = last collected value (best-effort; falls back to node id).
                        last_collected = None
                        try:
                            if collected:
                                last_collected = list(collected.values())[-1]
                        except Exception:
                            last_collected = None
                        self._maybe_mark_lead(
                            state,
                            end_node_data,
                            captured_value=last_collected if last_collected is not None else end_node.get("id"),
                        )
        except Exception:
            logger.exception("[interactive_engine] END-node lead hook failed (best-effort, ignored)")

        state.complete()

        # CRM: qualify lead on flow completion (forward-only, idempotent, never raises).
        try:
            from SocioviaCrm.lead_ingest import advance_lead_status
            advance_lead_status(
                workspace_id=self.workspace_id,
                phone=getattr(state, "phone_number", None),
                external_id=str(state.conversation_id) if state.conversation_id is not None else None,
                target_status="qualified",
                reason="Completed automation flow",
                db_session=db.session,
            )
        except Exception as e:
            logger.warning(f"[interactive_engine] CRM advance_lead_status failed on flow completion: {e}")

        _clear_cached_active_state_id(self.workspace_id, state.conversation_id)

    def _deactivate_existing_active_states(self, conversation_id: int) -> None:
        """Ensure only one active automation state per conversation."""
        active_states = WhatsAppConversationState.query.filter_by(
            conversation_id=conversation_id,
            workspace_id=self.workspace_id,
            is_active=True,
        ).all()
        if not active_states:
            return
        for st in active_states:
            st.complete()
        _clear_cached_active_state_id(self.workspace_id, conversation_id)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    def _get_paused_conversation_states(self, conversation_id: int) -> List[WhatsAppConversationState]:
        """Get recent inactive states for a conversation ordered by recency."""
        try:
            states = (
                WhatsAppConversationState.query.filter_by(
                    conversation_id=conversation_id,
                    workspace_id=self.workspace_id,
                    is_active=False,
                )
                .order_by(WhatsAppConversationState.updated_at.desc())
                .limit(10)
                .all()
            )
            return states
        except Exception:
            db.session.rollback()
            return []

    def _state_can_handle_button_reply(
        self,
        state: Optional[WhatsAppConversationState],
        message_text: str,
        button_payload: Optional[str],
    ) -> bool:
        """Return True when the supplied state can safely consume the button reply."""
        if not state or state.automation_id is None:
            return False

        compiled_flow = self._get_compiled_flow(state.automation_id)
        if not compiled_flow:
            return False

        node_map = compiled_flow["node_map"]
        handle_edges = compiled_flow["handle_edges"]
        current_node = node_map.get(state.current_node_id)
        if not current_node:
            return False

        if button_payload:
            decoded = TemplateNodeExecutor.decode_button_payload(button_payload)
            if decoded and str(decoded.get("automation_id")) == str(state.automation_id):
                return True

            if button_payload in handle_edges:
                return True

            if state.last_button_clicked and str(state.last_button_clicked) == str(button_payload):
                return True

            if self._get_button_action(node_map, state.current_node_id, button_payload):
                return True

            if current_node.get("type") == "api":
                node_data = current_node.get("data") or {}
                if node_data.get("defaultNextNodeId"):
                    return True
                compiled_flow = self._get_compiled_flow(state.automation_id) or {}
                if self._resolve_quick_reply_route(
                    compiled_flow.get("flow_config"),
                    button_payload,
                ):
                    return True
                for option in self._extract_api_quick_reply_options(state, current_node):
                    if str(option.get("id")) == str(button_payload):
                        return True
                    if message_text and self._normalized_text(option.get("label", "")) == self._normalized_text(message_text):
                        return True

        if message_text and current_node.get("type") == "message":
            normalized_message = message_text.strip().lower()
            buttons = current_node.get("data", {}).get("buttons", [])
            for button in buttons:
                label = (button.get("label") or button.get("title") or button.get("text") or "").strip().lower()
                if label and label == normalized_message:
                    return True

        return False

    def _select_button_reply_state(
        self,
        active_state: Optional[WhatsAppConversationState],
        conversation_id: int,
        message_text: str,
        button_payload: Optional[str],
    ) -> Optional[WhatsAppConversationState]:
        """Pick the best state for a button reply, preferring the state that can actually consume it."""
        if active_state and self._state_can_handle_button_reply(active_state, message_text, button_payload):
            return active_state

        for paused_state in self._get_paused_conversation_states(conversation_id):
            if active_state and paused_state.id == active_state.id:
                continue
            if self._state_can_handle_button_reply(paused_state, message_text, button_payload):
                return paused_state

        return active_state

    def _looks_like_flow_resume_request(self, message_text: str) -> bool:
        text = (message_text or "").strip().lower()
        if not text:
            return False
        resume_keywords = {
            "resume",
            "continue",
            "back",
            "go back",
            "continue flow",
            "resume flow",
            "menu",
            "options",
            "start again",
        }
        return text in resume_keywords

    def _is_satisfaction_ack(self, message_text: str) -> bool:
        """True when the user signals their off-script question is resolved / they're satisfied
        (e.g. 'thanks', 'got it', 'ok done'). In a paused-flow context we treat that as 'ready to
        resume the flow'."""
        text = self._normalized_text(message_text)
        if not text:
            return False
        acks = {
            "thanks", "thank you", "thankyou", "thanks!", "thank u", "ty", "tysm",
            "ok", "okay", "k", "kk", "okk", "ok thanks", "okay thanks", "ok thank you",
            "got it", "gotit", "understood", "great", "great thanks", "perfect", "perfect thanks",
            "cool", "nice", "awesome", "done", "all good", "makes sense", "clear", "noted",
        }
        return text in acks

    def _is_offscript_question(self, message_text: str, state) -> bool:
        """True when a mid-flow message is an off-script question/request (so it should pause the
        current flow and be answered, NOT hijack it into a different automation just because it
        happens to contain another automation's trigger word, e.g. asking 'is there a demo?'
        while choosing a library)."""
        try:
            compiled = self._get_compiled_flow(state.automation_id) if state and state.automation_id else None
            node = (compiled or {}).get("node_map", {}).get(state.current_node_id) if compiled else None
            return self._classify_unmatched_mid_flow_input(message_text, node, state=state) == "new_query"
        except Exception:
            return False

    def _stop_paused_state_for_restart(self, paused_state: WhatsAppConversationState) -> None:
        """Mark paused state as superseded before fresh trigger restart."""
        state_data = paused_state.state_data if isinstance(paused_state.state_data, dict) else {}
        state_data.update(
            {
                "paused": False,
                "stopped": True,
                "stop_reason": "new_trigger_restart",
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        paused_state.state_data = state_data
        paused_state.is_active = False
        paused_state.updated_at = datetime.now(timezone.utc)
        if not paused_state.completed_at:
            paused_state.completed_at = datetime.now(timezone.utc)

    def _stop_active_state_for_restart(self, active_state: WhatsAppConversationState) -> None:
        """Mark active flow as completed before starting a fresh trigger flow."""
        state_data = active_state.state_data if isinstance(active_state.state_data, dict) else {}
        state_data.update(
            {
                "stopped": True,
                "stop_reason": "trigger_restart",
                "stopped_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        active_state.state_data = state_data
        active_state.is_active = False
        active_state.updated_at = datetime.now(timezone.utc)
        if not active_state.completed_at:
            active_state.completed_at = datetime.now(timezone.utc)

    @staticmethod
    def _normalized_text(text: str) -> str:
        return " ".join((text or "").lower().strip().split())

    @staticmethod
    def _normalize_option_label(text: str) -> str:
        """Loose label compare for plan/library quick replies (currency, punctuation)."""
        s = (text or "").lower()
        for token in ("₹", "rs.", "rs", "inr"):
            s = s.replace(token, " ")
        s = re.sub(r"[^\w\s]", " ", s)
        return " ".join(s.split())

    def _message_pick_candidates(self, message_text: str) -> List[str]:
        """Quoted WhatsApp replies often embed the prior bot prompt — use the last line too."""
        text = (message_text or "").strip()
        if not text:
            return []
        candidates = [text]
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) > 1:
            candidates.append(lines[-1])
        return candidates

    def _pick_from_option_list(
        self,
        message_text: str,
        options: List[Dict[str, str]],
    ) -> Optional[str]:
        if not options:
            return None
        for candidate in self._message_pick_candidates(message_text):
            norm_candidate = self._normalize_option_label(candidate)
            if not norm_candidate:
                continue
            for option in options:
                label = option.get("label") or ""
                norm_label = self._normalize_option_label(label)
                if norm_label and norm_candidate == norm_label:
                    return str(option["id"])
            best_id = None
            best_score = 0.0
            for option in options:
                score = self._similarity(candidate, option.get("label") or "")
                if score > best_score:
                    best_score = score
                    best_id = option["id"]
            if best_id and best_score >= _FUZZY_MATCH_THRESHOLD:
                return str(best_id)
        return None

    def _pick_selectable_option_id(
        self,
        message_text: str,
        current_node: Optional[dict],
        state: Optional[WhatsAppConversationState] = None,
    ) -> Optional[str]:
        options = self._extract_selectable_options(current_node, state=state)
        return self._pick_from_option_list(message_text, options)

    def _extract_search_library_options(
        self,
        state: Optional[WhatsAppConversationState],
    ) -> List[Dict[str, str]]:
        if not state:
            return []
        search = (state.get_collected_fields() or {}).get("search_result")
        if not isinstance(search, dict):
            return []
        options: List[Dict[str, str]] = []
        for item in search.get("quickReplies") or []:
            if not isinstance(item, dict):
                continue
            btn_id = item.get("id")
            label = item.get("label") or item.get("title") or item.get("text")
            if btn_id and label:
                options.append({"id": str(btn_id), "label": str(label)})
        data = search.get("data")
        if isinstance(data, dict):
            for lib in data.get("libraries") or []:
                if not isinstance(lib, dict):
                    continue
                lib_id = lib.get("id")
                name = lib.get("name") or lib.get("label")
                if lib_id and name:
                    options.append({"id": str(lib_id), "label": str(name)})
        return options

    def _try_library_label_repick(
        self,
        message_text: str,
        state: WhatsAppConversationState,
        compiled_flow: Dict[str, Any],
    ) -> Optional[Tuple[str, str]]:
        """Match library name from typed/quoted text against the last search results."""
        options = self._extract_search_library_options(state)
        if not options:
            return None
        pick_id = self._pick_from_option_list(message_text, options)
        if not pick_id:
            return None
        target = self._resolve_library_repick_target(compiled_flow)
        if not target:
            return None
        return pick_id, target

    def _similarity(self, a: str, b: str) -> float:
        a_norm = self._normalized_text(a)
        b_norm = self._normalized_text(b)
        if not a_norm or not b_norm:
            return 0.0
        seq = SequenceMatcher(None, a_norm, b_norm).ratio()
        a_tokens = set(a_norm.split())
        b_tokens = set(b_norm.split())
        token_overlap = len(a_tokens & b_tokens) / max(1, len(b_tokens))
        return max(seq, token_overlap)

    _POST_FLOW_ACK_EXACT = frozenset(
        {
            "ok",
            "okay",
            "k",
            "kk",
            "thanks",
            "thank you",
            "thankyou",
            "thx",
            "ty",
            "cool",
            "great",
            "got it",
            "sure",
            "alright",
            "fine",
            "nice",
            "perfect",
            "noted",
        }
    )
    _POST_FLOW_ACK_TOKENS = frozenset(
        {
            "ok",
            "okay",
            "k",
            "kk",
            "thanks",
            "thank",
            "thankyou",
            "thx",
            "ty",
            "cool",
            "great",
            "got",
            "it",
            "sure",
            "alright",
            "fine",
            "nice",
            "perfect",
            "noted",
        }
    )
    _POST_FLOW_WINDOW_HOURS = 24

    @staticmethod
    def _resolve_quick_reply_route(
        flow_config: Optional[Dict[str, Any]],
        button_payload: str,
    ) -> Optional[str]:
        """Map BookMyLibrary-style quickReply id → target node via flow_config.quickReplyRoutes."""
        payload = str(button_payload or "").strip()
        if not payload or not isinstance(flow_config, dict):
            return None
        routes = flow_config.get("quickReplyRoutes") or flow_config.get("quick_reply_routes") or []
        if not isinstance(routes, list):
            return None
        for route in routes:
            if not isinstance(route, dict):
                continue
            route_id = str(route.get("button_id") or route.get("buttonId") or route.get("id") or "").strip()
            if route_id and (route_id == payload or payload.startswith(f"{route_id}__")):
                target = route.get("target") or route.get("targetNodeId") or route.get("target_node_id")
                if target:
                    return str(target)
        return None

    @staticmethod
    def _resolve_entry_route(
        flow_config: Optional[Dict[str, Any]],
        message_text: str,
    ) -> Optional[str]:
        """Map post-flow keywords (e.g. faq) → target node via flow_config.entryRoutes."""
        msg_lower = (message_text or "").strip().lower()
        if not msg_lower or not isinstance(flow_config, dict):
            return None
        routes = flow_config.get("entryRoutes") or flow_config.get("entry_routes") or []
        if not isinstance(routes, list):
            return None
        tokens = set(re.findall(r"\w+", msg_lower))
        for route in routes:
            if not isinstance(route, dict):
                continue
            target = route.get("target") or route.get("targetNodeId") or route.get("target_node_id")
            if not target:
                continue
            keywords = route.get("keywords") or route.get("keyword") or []
            if isinstance(keywords, str):
                keywords = [keywords]
            for keyword in keywords:
                kw_lower = str(keyword or "").strip().lower()
                if not kw_lower:
                    continue
                if kw_lower == msg_lower or kw_lower in tokens:
                    return str(target)
                if _keyword_matches_message(msg_lower, kw_lower):
                    return str(target)
        return None

    def _get_recently_completed_state(
        self,
        conversation_id: int,
    ) -> Optional[WhatsAppConversationState]:
        """Most recent completed flow state within the post-flow follow-up window."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self._POST_FLOW_WINDOW_HOURS)
        try:
            candidates = (
                WhatsAppConversationState.query.filter_by(
                    conversation_id=conversation_id,
                    workspace_id=self.workspace_id,
                    is_active=False,
                )
                .filter(WhatsAppConversationState.completed_at.isnot(None))
                .filter(WhatsAppConversationState.completed_at >= cutoff)
                .order_by(WhatsAppConversationState.completed_at.desc())
                .limit(5)
                .all()
            )
            for st in candidates:
                # Skip SWEPT (abandoned + torn down) states — they set completed_at for the
                # resume guard but are NOT genuine completions; treating them as "recently
                # completed" would wrongly send "You're all set!" to a returning customer.
                if (st.state_data or {}).get("swept_stale"):
                    continue
                return st
            return None
        except Exception:
            db.session.rollback()
            return None

    @staticmethod
    def _is_post_flow_casual_ack(message_text: str) -> bool:
        msg = (message_text or "").strip().lower()
        if not msg or len(msg) > 40:
            return False
        msg = re.sub(r"[^\w\s']", " ", msg)
        msg = " ".join(msg.split())
        if msg in InteractiveAutomationEngine._POST_FLOW_ACK_EXACT:
            return True
        tokens = set(re.findall(r"\w+", msg))
        return bool(tokens and tokens <= InteractiveAutomationEngine._POST_FLOW_ACK_TOKENS)

    def _handle_recent_completed_flow_followup(
        self,
        conversation_id: int,
        message_text: str,
        from_phone: str,
    ) -> Optional[Dict[str, Any]]:
        """Handle casual acks and entry-route keywords after a recently completed flow."""
        recent = self._get_recently_completed_state(conversation_id)
        if not recent or recent.automation_id is None:
            return None

        if self._is_post_flow_casual_ack(message_text):
            result = self._send_text_message(
                from_phone,
                "You're all set! Send *study* to book again or *faq* for common questions.",
                conversation_id=conversation_id,
            )
            return {
                "success": bool(result.get("success")),
                "handled": "post_flow_ack",
                "automation_id": recent.automation_id,
            }

        compiled_flow = self._get_compiled_flow(recent.automation_id)
        if not compiled_flow:
            return None
        flow_config = compiled_flow.get("flow_config") or {}
        target_node_id = self._resolve_entry_route(flow_config, message_text)
        if not target_node_id:
            return None

        inherited = recent.get_collected_fields() if hasattr(recent, "get_collected_fields") else {}
        logger.info(
            "[interactive_engine] Entry route '%s' → node=%s automation=%s (inherited fields=%s)",
            message_text[:40],
            target_node_id,
            recent.automation_id,
            list((inherited or {}).keys()),
        )
        return self._start_automation_at_node(
            automation_id=recent.automation_id,
            conversation_id=conversation_id,
            from_phone=from_phone,
            node_id=target_node_id,
            inherited_collected=inherited,
            version_token=compiled_flow.get("version_token"),
        )

    def _start_automation_at_node(
        self,
        automation_id: int,
        conversation_id: int,
        from_phone: str,
        node_id: str,
        inherited_collected: Optional[Dict[str, Any]] = None,
        version_token: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resume an automation at a specific node, preserving collected fields."""
        compiled_flow = self._get_compiled_flow(automation_id, version_token=version_token)
        if not compiled_flow:
            logger.error("[interactive_engine] Could not compile automation %s for entry route", automation_id)
            return None

        target_node = compiled_flow["node_map"].get(node_id)
        if not target_node:
            logger.warning(
                "[interactive_engine] Entry route target node %s missing in automation %s",
                node_id,
                automation_id,
            )
            return None

        self._deactivate_existing_active_states(conversation_id)

        state_data = {"collected": dict(inherited_collected or {})}
        state = WhatsAppConversationState(
            workspace_id=self.workspace_id,
            conversation_id=conversation_id,
            phone_number=from_phone,
            automation_id=automation_id,
            current_node_id=node_id,
            is_active=True,
            state_data=state_data,
            last_user_message_at=datetime.now(timezone.utc),
        )

        node_type = target_node.get("type")
        if node_type == "input":
            try:
                db.session.add(state)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
            except Exception as e:
                db.session.rollback()
                logger.exception("[interactive_engine] Failed to persist entry-route input state: %s", e)
                return {"success": False, "error": "Failed to persist flow state"}
            return self._send_input_question(
                state=state,
                input_node=target_node,
                from_phone=from_phone,
                node_map=compiled_flow["node_map"],
                source_edges=compiled_flow["source_edges"],
                handle_edges=compiled_flow["handle_edges"],
            )

        if node_type == "api":
            try:
                db.session.add(state)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
            except Exception as e:
                db.session.rollback()
                logger.exception("[interactive_engine] Failed to persist entry-route api state: %s", e)
                return {"success": False, "error": "Failed to persist flow state"}
            return self._execute_api_node(
                state=state,
                api_node=target_node,
                from_phone=from_phone,
                node_map=compiled_flow["node_map"],
                source_edges=compiled_flow["source_edges"],
                handle_edges=compiled_flow["handle_edges"],
            )

        if node_type == "message":
            result = self._send_node_message(
                automation_id,
                target_node,
                from_phone,
                state,
                compiled_flow["node_map"],
                compiled_flow["source_edges"],
                compiled_flow["handle_edges"],
            )
        elif node_type == "template":
            result = self._send_template_node(
                automation_id,
                target_node,
                from_phone,
                state,
                compiled_flow["node_map"],
                compiled_flow["source_edges"],
                compiled_flow["handle_edges"],
            )
        else:
            logger.warning(
                "[interactive_engine] Unsupported entry-route node type %s for node %s",
                node_type,
                node_id,
            )
            return None

        if result and result.get("success"):
            try:
                db.session.add(state)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
            except Exception as e:
                db.session.rollback()
                logger.exception(
                    "[interactive_engine] Failed to persist entry-route state for automation %s: %s",
                    automation_id,
                    e,
                )
                return {"success": False, "error": "Failed to persist flow state"}
        return result

    def _extract_api_quick_reply_options(
        self,
        state: Optional[WhatsAppConversationState],
        current_node: Optional[dict],
    ) -> List[Dict[str, str]]:
        if not state or not current_node or current_node.get("type") != "api":
            return []
        node_data = current_node.get("data") or {}
        store_as = node_data.get("storeAs")
        stored = None
        if store_as:
            stored = (state.get_collected_fields() or {}).get(store_as)
        if not isinstance(stored, dict):
            state_data = state.state_data if isinstance(state.state_data, dict) else {}
            pending = state_data.get("pending_api_buttons") or []
            return [
                {
                    "id": str(btn.get("id") or btn.get("reply", {}).get("id") or ""),
                    "label": str(
                        btn.get("label")
                        or btn.get("title")
                        or btn.get("text")
                        or btn.get("reply", {}).get("title")
                        or ""
                    ),
                }
                for btn in pending
                if isinstance(btn, dict)
            ]
        options: List[Dict[str, str]] = []
        for item in stored.get("quickReplies") or []:
            if not isinstance(item, dict):
                continue
            btn_id = item.get("id")
            label = item.get("label") or item.get("title") or item.get("text")
            if btn_id and label:
                options.append({"id": str(btn_id), "label": str(label)})
        return options

    def _extract_selectable_options(
        self,
        current_node: Optional[dict],
        state: Optional[WhatsAppConversationState] = None,
    ) -> List[Dict[str, str]]:
        options: List[Dict[str, str]] = []
        if not current_node:
            return options

        if current_node.get("type") == "api":
            options.extend(self._extract_api_quick_reply_options(state, current_node))

        node_data = current_node.get("data", {}) or {}
        if current_node.get("type") == "message":
            if node_data.get("interactiveType") == "list":
                for section in node_data.get("sections", []) or []:
                    for row in section.get("rows", []) or []:
                        row_id = row.get("id")
                        title = row.get("title")
                        if row_id and title:
                            options.append({"id": row_id, "label": str(title)})
            else:
                for button in node_data.get("buttons", []) or []:
                    btn_id = button.get("id")
                    label = button.get("label") or button.get("title") or button.get("text")
                    if btn_id and label:
                        options.append({"id": btn_id, "label": str(label)})

        return options

    def _fuzzy_match_option_id(
        self,
        message_text: str,
        current_node: Optional[dict],
        state: Optional[WhatsAppConversationState] = None,
    ) -> Optional[str]:
        pick_id = self._pick_selectable_option_id(message_text, current_node, state=state)
        if pick_id:
            logger.info(
                "[interactive_engine] Matched selectable option for '%s' -> '%s'",
                (message_text or "")[:60],
                pick_id,
            )
        return pick_id

    def _is_confirmation_style_input_node(self, current_node: Optional[dict]) -> bool:
        """
        True when the user is on a final 'verify your details' style text step.
        Free-text questions here should advance the flow (e.g. thank-you / end),
        not pause for AI as an unrelated 'new_query'.
        """
        if not current_node or current_node.get("type") != "input":
            return False
        body = ((current_node.get("data") or {}).get("body") or "").lower()
        markers = (
            "verify your details",
            "verify your detail",
            "verify the details",
            "verify these details",
            "verify details",
            "everything correct",
            "i hope everything correct",
            "confirm your details",
            "is everything correct",
        )
        return any(m in body for m in markers)

    def _classify_unmatched_mid_flow_input(
        self,
        message_text: str,
        current_node: Optional[dict],
        state: Optional[WhatsAppConversationState] = None,
    ) -> str:
        text = self._normalized_text(message_text)
        if not text:
            return "flow_related"

        if (
            current_node
            and current_node.get("type") == "input"
            and self._is_confirmation_style_input_node(current_node)
        ):
            return "flow_related"

        words = text.split()
        word_count = len(words)

        # 1) A SHORT reply that overlaps a selectable option label is almost certainly an option
        #    selection (a typed menu choice), so keep it in-flow. Check this first so menu picks
        #    like "contact" / "pricing" are not mistaken for off-script questions.
        options = self._extract_selectable_options(current_node, state=state)
        if options:
            option_tokens = set()
            for option in options:
                option_tokens.update(self._normalized_text(option.get("label", "")).split())
            if (set(words) & option_tokens) and word_count <= 4:
                logger.debug(
                    "[interactiveClassify] Short option-token match for '%s' (word_count=%s) -> flow_related",
                    message_text,
                    word_count,
                )
                return "flow_related"

        # 2) Off-script questions / explicit info requests -> let AI/FAQ answer (new_query).
        query_markers = (
            "what", "why", "how", "when", "where", "who", "which",
            "can", "could", "do", "does", "is", "are", "should", "would",
            "explain", "tell me",
        )
        # Imperative info-requests customers commonly type mid-flow ("give me contact info",
        # "send me the location", "show pricing"). Matched at the START only, so a user who is
        # *providing* an answer that merely contains one of these words is not mis-paused.
        request_markers = (
            "give", "send", "share", "show", "tell", "provide", "list",
            "i need", "i want", "i would like", "can you", "could you", "please", "need", "want",
        )
        msg_raw = message_text or ""
        has_question_punctuation = "?" in msg_raw
        starts_with_query = text.startswith(query_markers)
        starts_with_request = text.startswith(request_markers)
        has_query_word_anywhere = any(f" {marker} " in f" {text} " for marker in query_markers)
        looks_like_sentence_query = word_count >= 8 and (has_question_punctuation or has_query_word_anywhere)

        if (
            has_question_punctuation
            or starts_with_query
            or starts_with_request
            or looks_like_sentence_query
            or word_count >= 5
        ):
            logger.info(
                "[interactiveClassify] new_query for '%s' (q_punc=%s starts_query=%s starts_request=%s word_count=%s)",
                message_text, has_question_punctuation, starts_with_query, starts_with_request, word_count,
            )
            return "new_query"

        logger.debug(f"[interactiveClassify] Classified as flow_related for '{message_text}'")
        return "flow_related"

    def _build_flow_clarification_text(
        self,
        current_node: Optional[dict],
        state: Optional[WhatsAppConversationState] = None,
    ) -> str:
        options = self._extract_selectable_options(current_node, state=state)
        if not options:
            return "I did not catch that exactly. Please choose one of the available options or type continue to proceed."

        preview = options[:3]
        lines = ["I did not catch that exactly. Did you mean:"]
        for idx, option in enumerate(preview, start=1):
            lines.append(f"{idx}. {option['label']}")
        lines.append("You can also ask a new question anytime.")
        return "\n".join(lines)

    def _get_active_conversation_state(self, conversation_id: int) -> Optional[WhatsAppConversationState]:
        """Get active conversation state if user is mid-flow."""
        cached_state_id = _get_cached_active_state_id(self.workspace_id, conversation_id)
        if cached_state_id is not None:
            cached_state = db.session.get(WhatsAppConversationState, cached_state_id)
            if cached_state is not None and cached_state.automation_id is None:
                logger.info(
                    f"[interactive_engine] Clearing cached orphaned state {cached_state.id} "
                    f"for conversation {conversation_id} (automation_id is null)"
                )
                cached_state.complete()
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                _clear_cached_active_state_id(self.workspace_id, conversation_id)
                return None
            if (
                cached_state
                and cached_state.is_active
                and str(cached_state.workspace_id) == self.workspace_id
                and int(cached_state.conversation_id) == int(conversation_id)
            ):
                return cached_state
            _clear_cached_active_state_id(self.workspace_id, conversation_id)

        state = WhatsAppConversationState.query.filter_by(
            conversation_id=conversation_id,
            workspace_id=self.workspace_id,
            is_active=True
        ).first()
        if state is not None and state.automation_id is None:
            logger.info(
                f"[interactive_engine] Clearing orphaned conversation state {state.id} "
                f"for conversation {conversation_id} (automation_id is null)"
            )
            state.complete()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            _clear_cached_active_state_id(self.workspace_id, conversation_id)
            return None

        if state is not None:
            _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
        return state

    # ── Start New Flow ────────────────────────────────────────────────

    def _start_automation_flow(
        self,
        automation_match: Dict[str, Any],
        conversation_id: int,
        from_phone: str,
        trigger_message: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Start a new automation flow for the user."""
        t_prepare = time.perf_counter()
        automation_id = automation_match["automation_id"]
        compiled_flow = self._get_compiled_flow(
            automation_id,
            version_token=automation_match.get("version_token"),
        )
        if not compiled_flow:
            logger.error(f"Automation {automation_id} could not be compiled")
            return None

        self._deactivate_existing_active_states(conversation_id)

        # Reserve one-time slot immediately so rapid duplicate webhooks cannot re-trigger.
        if automation_match.get("one_time_only"):
            _mark_seen_phone_cache(
                self.workspace_id,
                automation_id,
                self._phone_candidates(from_phone),
            )

        first_node = compiled_flow["node_map"].get(compiled_flow.get("first_node_id"))
        if not first_node:
            logger.warning(f"Automation {automation_id} has no message/template node connected to trigger")
            return None
        prepare_ms = (time.perf_counter() - t_prepare) * 1000

        # Create conversation state in memory first so we can send before paying
        # for the commit. We only persist this state after the outbound send
        # succeeds.
        state = WhatsAppConversationState(
            workspace_id=self.workspace_id,
            conversation_id=conversation_id,
            phone_number=from_phone,
            automation_id=automation_id,
            current_node_id=first_node.get("id"),
            is_active=True,
            last_user_message_at=datetime.now(timezone.utc),
        )

        # ── Flow→Lead hook (TRIGGER node) ──
        # Fire at flow start when the trigger node carries an enabled leadAction.
        # captured_value = the inbound trigger message. Best-effort + savepoint-isolated
        # + per-inbound deduped inside _maybe_mark_lead; no-op when no enabled leadAction.
        trigger_node = compiled_flow["node_map"].get(compiled_flow.get("trigger_node_id"))
        if trigger_node:
            trigger_node_data = dict(trigger_node.get("data") or {})
            if isinstance(trigger_node_data.get("leadAction"), dict) and trigger_node_data["leadAction"].get("enabled"):
                trigger_node_data["id"] = trigger_node.get("id")
                self._maybe_mark_lead(
                    state,
                    trigger_node_data,
                    captured_value=trigger_message,
                    message_text=trigger_message,
                )

        # Send the first node
        if first_node.get("type") == "input":
            # Input node as first node: persist state first, then send question.
            # _send_input_question manages its own state advancement and commits.
            try:
                db.session.add(state)
                self._record_trigger_hit(automation_id)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
                _mark_seen_phone_cache(
                    self.workspace_id,
                    automation_id,
                    self._phone_candidates(from_phone),
                )
            except Exception as e:
                db.session.rollback()
                logger.exception(f"Failed to persist flow start for input node: {e}")
                return {"success": False, "error": "Failed to persist flow state"}

            return self._send_input_question(
                state=state,
                input_node=first_node,
                from_phone=from_phone,
                node_map=compiled_flow["node_map"],
                source_edges=compiled_flow["source_edges"],
                handle_edges=compiled_flow["handle_edges"],
            )
        elif first_node.get("type") == "template":
            result = self._send_template_node(
                automation_id,
                first_node,
                from_phone,
                state,
                compiled_flow["node_map"],
                compiled_flow["source_edges"],
                compiled_flow["handle_edges"],
            )
        elif first_node.get("type") == "api":
            try:
                db.session.add(state)
                self._record_trigger_hit(automation_id)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
                _mark_seen_phone_cache(
                    self.workspace_id,
                    automation_id,
                    self._phone_candidates(from_phone),
                )
            except Exception as e:
                db.session.rollback()
                logger.exception(f"Failed to persist flow start for api node: {e}")
                return {"success": False, "error": "Failed to persist flow state"}
            result = self._execute_api_node(
                state=state,
                api_node=first_node,
                from_phone=from_phone,
                node_map=compiled_flow["node_map"],
                source_edges=compiled_flow["source_edges"],
                handle_edges=compiled_flow["handle_edges"],
            )
        elif first_node.get("type") == "lead":
            # Lead node as first node after the trigger: persist state, mark the lead,
            # then pass through to the next node. _execute_lead_node owns its commits.
            try:
                db.session.add(state)
                self._record_trigger_hit(automation_id)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
                _mark_seen_phone_cache(
                    self.workspace_id,
                    automation_id,
                    self._phone_candidates(from_phone),
                )
            except Exception as e:
                db.session.rollback()
                logger.exception(f"Failed to persist flow start for lead node: {e}")
                return {"success": False, "error": "Failed to persist flow state"}
            return self._execute_lead_node(
                state=state,
                lead_node=first_node,
                from_phone=from_phone,
                node_map=compiled_flow["node_map"],
                source_edges=compiled_flow["source_edges"],
                handle_edges=compiled_flow["handle_edges"],
                captured_value=None,
            )
        else:
            result = self._send_node_message(
                automation_id,
                first_node,
                from_phone,
                state,
                compiled_flow["node_map"],
                compiled_flow["source_edges"],
                compiled_flow["handle_edges"],
            )
            if result and result.get("success") and first_node.get("type") == "message":
                self._advance_to_following_input_if_any(state, first_node, compiled_flow)

        if result and result.get("success"):
            t_commit = time.perf_counter()
            try:
                db.session.add(state)
                self._record_trigger_hit(automation_id)
                db.session.commit()
                _set_cached_active_state_id(self.workspace_id, conversation_id, state.id)
                _mark_seen_phone_cache(
                    self.workspace_id,
                    automation_id,
                    self._phone_candidates(from_phone),
                )
                commit_ms = (time.perf_counter() - t_commit) * 1000
                logger.info(
                    f"[interactive_engine] Flow start prepared in {prepare_ms:.0f}ms, "
                    f"committed in {commit_ms:.0f}ms after send"
                )
            except Exception as e:
                db.session.rollback()
                logger.exception(f"Failed to persist flow start for automation {automation_id}: {e}")
                return {"success": False, "error": "Failed to persist flow state"}

        return result

    # ── Flow Continuation ─────────────────────────────────────────────

    def _handle_flow_continuation(
        self,
        state: WhatsAppConversationState,
        message_text: str,
        from_phone: str,
        is_button_reply: bool,
        button_payload: Optional[str],
        inbound_wamid: Optional[str] = None,
        is_flow_reply: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Handle continuation of an active flow (button click, flow form, or text)."""

        if state.automation_id is None:
            logger.info(
                f"[interactive_engine] Clearing state {state.id} because automation_id is null"
            )
            state.complete()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            _clear_cached_active_state_id(self.workspace_id, state.conversation_id)
            return {"state_cleared": True, "reason": "missing_automation"}

        # Check stale state (>24h)
        if state.last_user_message_at:
            last_msg_at = state.last_user_message_at
            if last_msg_at.tzinfo is None:
                last_msg_at = last_msg_at.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - last_msg_at).total_seconds() / 3600
            if age_hours > 24:
                logger.info(f"Clearing stale state {state.id} (age={age_hours:.0f}h)")
                state.complete()
                db.session.commit()
                return None

        # Per-wamid dedup for button/flow-form continuations (text/input has its own guard in
        # _handle_input_node_response). Prevents a retried inbound webhook from double-advancing
        # the flow or sending duplicate messages.
        if inbound_wamid and (is_button_reply or is_flow_reply):
            _sd0 = state.state_data if isinstance(state.state_data, dict) else {}
            if _sd0.get("last_flow_event_wamid") == inbound_wamid:
                logger.info(
                    "[interactive_engine] Skipping duplicate button/flow wamid=%s state=%s",
                    inbound_wamid, state.id,
                )
                return {"success": True, "duplicate_inbound_skipped": True}
            _sd1 = dict(_sd0)
            _sd1["last_flow_event_wamid"] = inbound_wamid
            state.state_data = _sd1
            flag_modified(state, "state_data")
            # Do NOT commit here — let the marker ride the terminal commit that accompanies a
            # successful advance/send, so a rollback on send failure also reverts the marker
            # and the retry can recover (mirrors the input path's last_input_handled_wamid).

        compiled_flow = self._get_compiled_flow(state.automation_id)
        if not compiled_flow:
            state.complete()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            _clear_cached_active_state_id(self.workspace_id, state.conversation_id)
            return None

        node_map = compiled_flow["node_map"]
        source_edges = compiled_flow["source_edges"]
        handle_edges = compiled_flow["handle_edges"]

        current_node_id = state.current_node_id
        next_node_id = None

        # ── Mid-flow exit (Done button uses normal routing; keywords here) ──
        if not is_button_reply and message_text:
            exit_target = self._resolve_entry_route(
                compiled_flow.get("flow_config") or {}, message_text
            )
            if exit_target:
                exit_node = node_map.get(exit_target)
                if exit_node and exit_node.get("type") == "end":
                    state.advance_to_node(exit_target)
                    self._mark_flow_completed(state)
                    end_message = (exit_node.get("data") or {}).get("message")
                    if end_message:
                        collected = state.get_collected_fields()
                        end_message = input_node_handler.substitute_variables(end_message, collected)
                        result = self._send_text_message(from_phone, end_message, state.conversation_id)
                        if result.get("success"):
                            try:
                                db.session.commit()
                            except Exception:
                                db.session.rollback()
                        else:
                            db.session.rollback()
                        return result
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return {"completed": True, "reason": "user_exit"}

        # ── STRICT INPUT LOCK: process input node before anything else ──
        if state.is_waiting_for_input and not is_button_reply and message_text:
            current_node_for_input = node_map.get(current_node_id)
            if not current_node_for_input or current_node_for_input.get("type") != "input":
                field = state.current_input_field
                if field:
                    for candidate in node_map.values():
                        if candidate.get("type") != "input":
                            continue
                        if (candidate.get("data") or {}).get("field") == field:
                            current_node_for_input = candidate
                            state.advance_to_node(candidate.get("id"))
                            flag_modified(state, "state_data")
                            break
            if current_node_for_input and current_node_for_input.get("type") == "input":
                return self._handle_input_node_response(
                    state=state,
                    message_text=message_text,
                    from_phone=from_phone,
                    current_node=current_node_for_input,
                    node_map=node_map,
                    source_edges=source_edges,
                    handle_edges=handle_edges,
                    inbound_wamid=inbound_wamid,
                )

        # ── WhatsApp Flow form completed (nfm_reply) ──
        if is_flow_reply:
            current_node = node_map.get(current_node_id)
            if current_node and current_node.get("type") == "template":
                next_node_id = self._resolve_template_flow_next_node(
                    current_node,
                    current_node_id,
                    source_edges,
                    handle_edges,
                )
                if not next_node_id:
                    state.complete()
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return {"flow_completed": True, "reason": "template_flow_no_next_node"}

        # ── Resolve next node ──
        if is_button_reply and button_payload and not next_node_id and not is_flow_reply:
            current_node_for_api = node_map.get(current_node_id)
            if current_node_for_api and current_node_for_api.get("type") == "api":
                # Capture library_id / plan_type / demo_date before routing to the next API node.
                self._apply_button_capture(state, button_payload, node_map, compiled_flow)
                flag_modified(state, "state_data")
                next_node_id = self._resolve_api_button_next_node(
                    state,
                    current_node_for_api,
                    button_payload,
                    message_text,
                    compiled_flow,
                    source_edges,
                    handle_edges,
                )
                if not next_node_id and message_text:
                    pick_id = self._pick_selectable_option_id(
                        message_text, current_node_for_api, state=state
                    )
                    if pick_id:
                        self._apply_button_capture(state, pick_id, node_map, compiled_flow)
                        button_payload = pick_id
                        next_node_id = self._resolve_api_button_next_node(
                            state,
                            current_node_for_api,
                            pick_id,
                            message_text,
                            compiled_flow,
                            source_edges,
                            handle_edges,
                        )

            # Check template button payload (iflow_ prefix)
            decoded = TemplateNodeExecutor.decode_button_payload(button_payload)
            if decoded:
                decoded_automation_id = decoded.get('automation_id')
                decoded_node_id = decoded.get('target_node_id')
                if str(decoded_automation_id) == str(state.automation_id):
                    if decoded_node_id is None:
                        state.complete()
                        db.session.commit()
                        return {"flow_completed": True, "reason": "template_end_button"}
                    else:
                        next_node_id = decoded_node_id

            # Regular button payload — O(1) edge lookup
            if not next_node_id:
                next_node_id = handle_edges.get(button_payload)

            if (
                not next_node_id
                and button_payload
                and self._looks_like_library_uuid(button_payload)
            ):
                repick_target = self._resolve_library_repick_target(compiled_flow)
                if repick_target:
                    self._apply_button_capture(state, button_payload, node_map, compiled_flow)
                    next_node_id = repick_target
                    logger.info(
                        "[interactive_engine] Library button from search → %s",
                        repick_target,
                    )

            if not next_node_id and message_text:
                lib_repick = self._try_library_label_repick(message_text, state, compiled_flow)
                if lib_repick:
                    lib_id, repick_target = lib_repick
                    state.set_collected_field("library_id", lib_id)
                    next_node_id = repick_target
                    logger.info(
                        "[interactive_engine] Library label re-pick (button ctx) '%s' → %s",
                        message_text[:40],
                        repick_target,
                    )

            # Runtime button ids can vary, so fall back to the visible title.
            if not next_node_id and message_text:
                current_node = node_map.get(current_node_id)
                if current_node and current_node.get("type") == "message":
                    buttons = current_node.get("data", {}).get("buttons", [])
                    normalized_message = message_text.strip().lower()
                    for button in buttons:
                        label = (button.get("label") or button.get("title") or button.get("text") or "").strip().lower()
                        if label and label == normalized_message:
                            button_id = button.get("id")
                            next_node_id = handle_edges.get(button_id)
                            if next_node_id:
                                button_payload = button_id
                            break

                    if not next_node_id:
                        fuzzy_option_id = self._fuzzy_match_option_id(message_text, current_node, state=state)
                        if fuzzy_option_id:
                            next_node_id = handle_edges.get(fuzzy_option_id)
                            if next_node_id:
                                button_payload = fuzzy_option_id
                            elif current_node and current_node.get("type") == "api":
                                next_node_id = self._resolve_api_button_next_node(
                                    state,
                                    current_node,
                                    fuzzy_option_id,
                                    message_text,
                                    compiled_flow,
                                    source_edges,
                                    handle_edges,
                                )
        elif not is_flow_reply:
            # Text response — match API quick replies and message buttons.
            current_node = node_map.get(current_node_id)
            if current_node:
                pick_id = self._pick_selectable_option_id(message_text, current_node, state=state)
                if pick_id:
                    if current_node.get("type") == "api":
                        self._apply_button_capture(state, pick_id, node_map, compiled_flow)
                        flag_modified(state, "state_data")
                        next_node_id = self._resolve_api_button_next_node(
                            state,
                            current_node,
                            pick_id,
                            message_text,
                            compiled_flow,
                            source_edges,
                            handle_edges,
                        )
                        button_payload = pick_id
                    elif current_node.get("type") == "message":
                        next_node_id = handle_edges.get(pick_id)

            if not next_node_id:
                lib_repick = self._try_library_label_repick(message_text, state, compiled_flow)
                if lib_repick:
                    lib_id, repick_target = lib_repick
                    state.set_collected_field("library_id", lib_id)
                    next_node_id = repick_target
                    logger.info(
                        "[interactive_engine] Library label re-pick '%s' → %s",
                        (message_text or "")[:40],
                        repick_target,
                    )

        if not next_node_id:
            next_node_id = self._resolve_default_continue_node_id(
                current_node_id,
                node_map,
                source_edges,
                is_button_reply=bool(is_button_reply and button_payload),
            )

        # Button action without edge (e.g. send_document)
        if not next_node_id and is_button_reply and button_payload:
            button_action = self._get_button_action(node_map, current_node_id, button_payload)
            if button_action and button_action.get("type") == "send_document":
                self._execute_button_action(node_map, current_node_id, button_payload, from_phone, state.conversation_id)
                # Look for default edge after action
                for edge in source_edges.get(current_node_id, []):
                    if not edge.get("sourceHandle"):
                        next_node_id = edge.get("target")
                        break
                if not next_node_id:
                    state.complete()
                    db.session.commit()
                    return {"completed": True, "document_sent": True, "message": "Document sent, flow completed"}

        if not next_node_id:
            current_node = node_map.get(current_node_id)
            if (
                current_node
                and current_node.get("type") == "api"
                and self._is_post_flow_casual_ack(message_text)
                and self._extract_selectable_options(current_node, state=state)
            ):
                send_result = self._send_text_message(
                    from_phone,
                    "Please tap one of the membership options above to continue.",
                    state.conversation_id,
                )
                return {
                    "success": bool(send_result.get("success")),
                    "clarification": True,
                    "reason": "casual_ack_at_api_buttons",
                }

            input_intent = self._classify_unmatched_mid_flow_input(message_text, current_node, state=state)
            logger.info(f"[interactivePause] Unmatched mid-flow input: text='{message_text}' intent={input_intent} current_node_type={current_node.get('type') if current_node else None}")

            if input_intent == "new_query":
                # Pause flow and let normal rule/AI routing take over.
                state_data = state.state_data if isinstance(state.state_data, dict) else {}
                state_data.update(
                    {
                        "paused": True,
                        "paused_at": datetime.now(timezone.utc).isoformat(),
                        "paused_node_id": state.current_node_id,
                        "pause_reason": "new_query",
                    }
                )
                state.state_data = state_data
                state.is_active = False
                # Do NOT set completed_at on pause — a paused flow is resumable and must
                # stay distinguishable from a genuinely completed one. Recency is tracked
                # via updated_at; resumability via state_data['paused'].
                state.updated_at = datetime.now(timezone.utc)
                _clear_cached_active_state_id(self.workspace_id, state.conversation_id)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    return {"success": False, "error": "failed_to_pause_flow"}

                logger.info(
                    "[interactivePause] ✅ PAUSED flow: state=%s for AI takeover conversation=%s text='%s'",
                    state.id,
                    state.conversation_id,
                    message_text,
                )
                return {
                    "success": True,
                    "paused": True,
                    "allow_ai_takeover": True,
                    "reason": "new_query",
                }

            # Flow-related but unclear: keep state active, ask clarification.
            clarification_text = self._build_flow_clarification_text(current_node, state=state)
            send_result = self._send_text_message(from_phone, clarification_text, state.conversation_id)
            if send_result.get("success"):
                try:
                    state.last_user_message_at = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            else:
                db.session.rollback()
            return {
                "success": True,
                "clarification": True,
                "reason": "flow_related_unmatched",
            }

        # Execute button action if button click
        if is_button_reply and button_payload:
            self._apply_button_capture(state, button_payload, node_map, compiled_flow)
            self._clear_pending_api_buttons(state)
            self._execute_button_action(node_map, current_node_id, button_payload, from_phone, state.conversation_id)

        # Re-resolve default continuation after capture (e.g. library_id) when still on same API node.
        if (
            not next_node_id
            and is_button_reply
            and button_payload
            and (node_map.get(current_node_id) or {}).get("type") == "api"
        ):
            next_node_id = self._resolve_default_continue_node_id(
                current_node_id,
                node_map,
                source_edges,
                is_button_reply=True,
            )

        # Find next node — O(1) lookup
        next_node = node_map.get(next_node_id)
        if not next_node:
            logger.warning(f"Target node {next_node_id} not found")
            return None

        # ── Input node: send question and wait ──
        if next_node.get("type") == "input":
            current_type = (node_map.get(current_node_id) or {}).get("type")
            if (
                message_text
                and not is_button_reply
                and current_type != "input"
                and not state.is_waiting_for_input
            ):
                state.advance_to_node(next_node_id)
                field = (next_node.get("data") or {}).get("field", "unknown")
                state.set_waiting_for_input(field)
                flag_modified(state, "state_data")
                return self._handle_input_node_response(
                    state=state,
                    message_text=message_text,
                    from_phone=from_phone,
                    current_node=next_node,
                    node_map=node_map,
                    source_edges=source_edges,
                    handle_edges=handle_edges,
                    inbound_wamid=inbound_wamid,
                )
            return self._send_input_question(
                state=state,
                input_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        # ── API node: HTTP call + mapped response ──
        if next_node.get("type") == "api":
            if button_payload:
                self._apply_button_capture(state, button_payload, node_map, compiled_flow)
                state.record_button_click(button_payload)
            state.advance_to_node(next_node_id)
            state.last_user_message_at = datetime.now(timezone.utc)
            return self._execute_api_node(
                state=state,
                api_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        # ── Lead node: mark lead then pass through to the next node (sends nothing) ──
        if next_node.get("type") == "lead":
            if button_payload:
                self._apply_button_capture(state, button_payload, node_map, compiled_flow)
                state.record_button_click(button_payload)
            return self._execute_lead_node(
                state=state,
                lead_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
                captured_value=button_payload if is_button_reply else message_text,
            )

        # Update state
        state.advance_to_node(next_node_id)
        if button_payload:
            state.record_button_click(button_payload)
        state.last_user_message_at = datetime.now(timezone.utc)

        # End node
        if next_node.get("type") == "end":
            self._mark_flow_completed(state)
            end_message = next_node.get("data", {}).get("message")
            if end_message:
                result = self._send_text_message(from_phone, end_message, state.conversation_id)
                if result.get("success"):
                    try:
                        db.session.commit()
                    except Exception as e:
                        db.session.rollback()
                        logger.warning(f"[interactive_engine] commit-after-send failed at end node: {e}")
                else:
                    db.session.rollback()
                return result
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"[interactive_engine] end-node commit failed: {e}")
            return {"completed": True, "message": "Flow completed"}

        # Template node
        if next_node.get("type") == "template":
            result = self._send_template_node(
                state.automation_id,
                next_node,
                from_phone,
                state,
                node_map,
                source_edges,
                handle_edges,
            )
            if result and result.get("success"):
                try:
                    db.session.commit()
                except Exception as e:
                    db.session.rollback()
                    logger.warning(f"[interactive_engine] commit-after-send failed at template node: {e}")
            else:
                db.session.rollback()
            return result

        result = self._send_node_message(
            state.automation_id,
            next_node,
            from_phone,
            state,
            node_map,
            source_edges,
            handle_edges,
        )
        if result and result.get("success"):
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"[interactive_engine] commit-after-send failed at message node: {e}")
        else:
            db.session.rollback()
        return result

    # ── Button Actions ────────────────────────────────────────────────

    def _get_button_action(
        self, node_map: dict, current_node_id: str, button_payload: str
    ) -> Optional[Dict[str, Any]]:
        """Get the action configuration for a specific button."""
        current_node = node_map.get(current_node_id)
        if not current_node or current_node.get("type") != "message":
            return None
        buttons = current_node.get("data", {}).get("buttons", [])
        for button in buttons:
            if button.get("id") == button_payload:
                return button.get("action", {})
        return None

    def _execute_button_action(
        self, node_map: dict, current_node_id: str, button_payload: str,
        to_phone: str, conversation_id: Optional[int] = None
    ) -> None:
        """Execute button-specific action (e.g. send_document)."""
        current_node = node_map.get(current_node_id)
        if not current_node or current_node.get("type") != "message":
            return

        buttons = current_node.get("data", {}).get("buttons", [])
        clicked_button = None
        for button in buttons:
            if button.get("id") == button_payload:
                clicked_button = button
                break

        if not clicked_button:
            return

        action = clicked_button.get("action", {})
        action_type = action.get("type")

        if action_type == "send_document":
            document_url = action.get("documentUrl")
            document_filename = action.get("documentFilename", "document.pdf")
            document_caption = action.get("documentCaption", "")
            if document_url:
                service = self._get_service()
                if service:
                    service.send_document(
                        to=to_phone, document_url=document_url,
                        caption=document_caption,
                        filename=document_filename,
                        conversation_id=conversation_id,
                        defer_post_send=True,
                        broadcast_on_success=True,
                    )

    def _resolve_flow_context(
        self,
        automation_ref,
        node_map: dict = None,
        source_edges: dict = None,
        handle_edges: dict = None,
    ) -> Tuple[Optional[int], dict, dict, dict]:
        """Resolve an automation id plus compiled graph data from either an id or ORM object."""
        automation_id = automation_ref.id if isinstance(automation_ref, WhatsAppVisualAutomation) else automation_ref
        if automation_id is None:
            return None, node_map, source_edges, handle_edges

        if node_map is not None and source_edges is not None and handle_edges is not None:
            return int(automation_id), node_map, source_edges, handle_edges

        compiled_flow = self._get_compiled_flow(int(automation_id))
        if not compiled_flow:
            return int(automation_id), node_map, source_edges, handle_edges

        return (
            int(automation_id),
            node_map or compiled_flow["node_map"],
            source_edges or compiled_flow["source_edges"],
            handle_edges or compiled_flow["handle_edges"],
        )

    def _get_cached_template(self, template_id: Optional[int], template_name: Optional[str]):
        """Get template from in-memory cache first, then DB (account-scoped)."""
        now = time.time()

        from .models import WhatsAppTemplate

        cache_key = None
        if template_id:
            cache_key = (int(self.account_id), "id", int(template_id))
        elif template_name:
            cache_key = (int(self.account_id), "name", str(template_name).strip().lower())

        if cache_key:
            cached = _TEMPLATE_CACHE.get(cache_key)
            if cached and now < cached[1]:
                return SimpleNamespace(**cached[0])

        template = None
        if template_id:
            template = WhatsAppTemplate.query.filter_by(
                id=template_id, account_id=self.account_id
            ).first()
        elif template_name:
            template = WhatsAppTemplate.query.filter_by(
                name=template_name, account_id=self.account_id, status="APPROVED"
            ).first()

        if template and cache_key:
            snapshot = {
                "id": template.id,
                "name": template.name,
                "language": template.language,
                "components": template.components,
            }
            _TEMPLATE_CACHE[cache_key] = (snapshot, now + _TEMPLATE_CACHE_TTL)
            return SimpleNamespace(**snapshot)

        return template

    # ── API Node Methods ────────────────────────────────────────────

    def _build_flow_variables(
        self,
        state: WhatsAppConversationState,
        from_phone: str,
        compiled_flow: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        compiled_flow = compiled_flow or self._get_compiled_flow(state.automation_id)
        variables = dict(state.get_collected_fields() or {})
        if compiled_flow:
            automation_vars = compiled_flow.get("variables") or {}
            if isinstance(automation_vars, dict):
                variables.update(automation_vars)
            flow_config = compiled_flow.get("flow_config") or {}
            defaults = flow_config.get("variableDefaults") if isinstance(flow_config, dict) else {}
            variables = flow_variables.merge_variable_defaults(variables, defaults)
        variables.update(self._get_runtime_variables(from_phone, state))
        plan_type = variables.get("plan_type")
        if isinstance(plan_type, str) and "__" in plan_type:
            base, _, price_part = plan_type.partition("__")
            variables["plan_selection_id"] = plan_type
            variables["plan_type"] = base
            if price_part.isdigit():
                variables["plan_price_inr"] = int(price_part)
        if not variables.get("plan_selection_id") and state.last_button_clicked:
            variables["plan_selection_id"] = state.last_button_clicked
        if not variables.get("library_id"):
            search = variables.get("search_result")
            if isinstance(search, dict):
                libs = (search.get("data") or {}).get("libraries") or []
                if isinstance(libs, list) and len(libs) == 1 and isinstance(libs[0], dict):
                    variables["library_id"] = libs[0].get("id") or variables.get("library_id")
        if state.last_button_clicked:
            variables["last_button_clicked"] = state.last_button_clicked
            variables["button_id"] = state.last_button_clicked
        return variables

    @staticmethod
    def _looks_like_library_uuid(payload: str) -> bool:
        return bool(_UUID_RE.match(str(payload or "").strip()))

    def _resolve_library_repick_target(
        self,
        compiled_flow: Dict[str, Any],
    ) -> Optional[str]:
        """When user taps a library from search results, route to amenities (or configured target)."""
        flow_config = compiled_flow.get("flow_config") or {}
        configured = flow_config.get("libraryPickTarget") or flow_config.get("library_pick_target")
        if configured:
            return str(configured)
        node_map = compiled_flow.get("node_map") or {}
        search_node = node_map.get("api_search")
        if search_node:
            default_next = (search_node.get("data") or {}).get("defaultNextNodeId")
            if default_next:
                return str(default_next)
        return None

    @staticmethod
    def _node_is_internal_router(node: Optional[Dict[str, Any]]) -> bool:
        if not node:
            return False
        return bool((node.get("data") or {}).get("internalRouter"))

    def _apply_button_capture(
        self,
        state: WhatsAppConversationState,
        button_payload: str,
        node_map: dict,
        compiled_flow: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not button_payload or not state.current_node_id:
            return
        current_node = node_map.get(state.current_node_id) or {}
        node_data = current_node.get("data") or {}
        compiled_flow = compiled_flow or self._get_compiled_flow(state.automation_id)
        flow_config = (compiled_flow or {}).get("flow_config") or {}
        rules = flow_variables.collect_button_capture_rules(node_data, flow_config)
        flow_variables.apply_button_capture_rules(
            rules,
            button_payload,
            state.set_collected_field,
        )
        if self._looks_like_library_uuid(button_payload):
            state.set_collected_field("library_id", str(button_payload).strip())

        # ── Flow→Lead hook (button capture) ──
        self._maybe_mark_lead(
            state,
            node_data,
            captured_value=button_payload,
        )

    def _maybe_mark_lead(
        self,
        state: WhatsAppConversationState,
        node_data: Optional[Dict[str, Any]],
        captured_value: Any = None,
        api_result: Any = None,
        message_text: Optional[str] = None,
    ) -> None:
        """Best-effort Flow→CRM-Lead bridge. Only runs when node_data carries an
        enabled `leadAction`; never raises into the flow.

        `message_text` is the raw inbound message text (when in scope) threaded
        through to the lead service for downstream intent-aware handling. It is
        optional — call sites where the raw text isn't available pass the best
        substitute (captured_value) or None, and the path never breaks."""
        try:
            lead_action = (node_data or {}).get("leadAction")
            if not isinstance(lead_action, dict) or not lead_action.get("enabled"):
                return

            # De-dup: the button-capture path can call this several times for the
            # same node+payload within one inbound message. Fire at most once per
            # (node_id, captured_value) per inbound message.
            fired = getattr(self, "_lead_hook_fired", None)
            if fired is None:
                fired = set()
                self._lead_hook_fired = fired
            node_id = (node_data or {}).get("id") or getattr(state, "current_node_id", None)
            try:
                dedup_key = (node_id, str(captured_value) if captured_value is not None else None)
            except Exception:
                dedup_key = (node_id, None)
            if dedup_key in fired:
                return
            fired.add(dedup_key)

            conversation = None
            if state.conversation_id is not None:
                conversation = WhatsAppConversation.query.get(state.conversation_id)
            if conversation is None:
                # Fall back to a lightweight carrier so phone/name still resolve.
                conversation = SimpleNamespace(
                    id=state.conversation_id,
                    user_phone=getattr(state, "phone_number", None),
                    user_name=None,
                    account_id=self.account_id,
                )
            # Fall back to captured_value when no raw inbound text was threaded in,
            # so the lead service always gets the best-available message text.
            effective_message_text = message_text
            if effective_message_text is None and captured_value is not None:
                effective_message_text = str(captured_value)

            # Tolerate the new (lead_id, is_new, resolved_stage_key) return shape;
            # this wrapper does not consume it, so we simply don't unpack-break.
            LeadActionService.maybe_mark_lead(
                self.workspace_id,
                conversation,
                node_data,
                captured_value=captured_value,
                collected=state.get_collected_fields(),
                api_result=api_result,
                message_text=effective_message_text,
            )
        except Exception:
            logger.exception("[interactive_engine] _maybe_mark_lead failed (best-effort, ignored)")

    def _advance_to_following_input_if_any(
        self,
        state: WhatsAppConversationState,
        message_node: Dict[str, Any],
        compiled_flow: Dict[str, Any],
    ) -> None:
        """After a prompt message (e.g. welcome), land on the next input node without re-asking."""
        node_id = message_node.get("id")
        node_map = compiled_flow.get("node_map") or {}
        source_edges = compiled_flow.get("source_edges") or {}
        for edge in source_edges.get(node_id, []):
            sh = edge.get("sourceHandle") or ""
            if sh and sh not in _API_DEFAULT_CONTINUE_HANDLES:
                continue
            target_id = edge.get("target")
            target = node_map.get(target_id)
            if not target or target.get("type") != "input":
                continue
            state.advance_to_node(target_id)
            field = (target.get("data") or {}).get("field", "input")
            state.set_waiting_for_input(field)
            flag_modified(state, "state_data")
            logger.info(
                "[interactive_engine] Advanced to input node %s after prompt (field=%s)",
                target_id,
                field,
            )
            break

    def _send_api_outbound(
        self,
        to_phone: str,
        outbound: Dict[str, Any],
        conversation_id: Optional[int],
    ) -> Dict[str, Any]:
        service = self._get_service()
        if not service:
            return {"success": False, "error": "Account not found"}

        msg_type = (outbound.get("type") or "text").lower()
        if msg_type == "text":
            return service.send_text(
                to=to_phone,
                text=outbound.get("text") or "",
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "image":
            return service.send_image_with_url_candidates(
                to=to_phone,
                image_url=outbound.get("url"),
                url_candidates=outbound.get("url_candidates"),
                caption=outbound.get("caption"),
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "document":
            return service.send_document(
                to=to_phone,
                document_url=outbound.get("url"),
                caption=outbound.get("caption"),
                filename=outbound.get("filename") or "document.pdf",
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "buttons":
            buttons = outbound.get("buttons") or []
            normalized = []
            for btn in buttons:
                if btn.get("type") == "reply" and isinstance(btn.get("reply"), dict):
                    normalized.append(btn)
                    continue
                title = btn.get("title") or btn.get("label") or btn.get("text") or ""
                normalized.append({
                    "id": btn.get("id") or f"btn_{len(normalized)}",
                    "title": title,
                })
            return service.send_interactive_buttons(
                to=to_phone,
                body_text=outbound.get("text") or "Choose an option:",
                buttons=normalized,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "list":
            sections = outbound.get("sections") or []
            return service.send_interactive_list(
                to=to_phone,
                body_text=outbound.get("text") or "Choose an option:",
                button_text=outbound.get("buttonText") or "View plans",
                sections=sections,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "carousel":
            interactive = outbound.get("interactive")
            if not isinstance(interactive, dict):
                interactive = {
                    "type": "carousel",
                    "body": {"text": outbound.get("text") or "Choose an option:"},
                    "action": {"cards": outbound.get("cards") or []},
                }
            return service.send_interactive_passthrough(
                to=to_phone,
                interactive=interactive,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        if msg_type == "template":
            kind = (outbound.get("carousel_kind") or "library").lower()
            if kind == "recruiter":
                from . import vaish_carousel_template as ct

                default_name = ct.VAISH_RECRUITER_CAROUSEL_TEMPLATE
                default_lang = ct.VAISH_RECRUITER_CAROUSEL_LANG
                default_count = ct.VAISH_RECRUITER_CAROUSEL_CARD_COUNT
                context = str(outbound.get("search_context") or "your search")
                components = ct.build_carousel_template_components(
                    service,
                    search_context=context,
                    cards=outbound.get("cards") or [],
                    card_count=int(outbound.get("card_count") or default_count),
                )
                list_body = (
                    f"We found verified recruiters for *{context}*. "
                    "Tap below to view profiles."
                )
                list_btn = "View recruiters"
                list_section = "Verified recruiters"
                id_field = "recruiter_id"
                entity_label = "Recruiter"
            else:
                from . import bml_carousel_template as ct

                default_name = ct.BML_LIBRARY_CAROUSEL_TEMPLATE
                default_lang = ct.BML_LIBRARY_CAROUSEL_LANG
                default_count = ct.BML_LIBRARY_CAROUSEL_CARD_COUNT
                context = str(outbound.get("location") or "your area")
                components = ct.build_carousel_template_components(
                    service,
                    location=context,
                    cards=outbound.get("cards") or [],
                    card_count=int(outbound.get("card_count") or default_count),
                )
                list_body = (
                    f"We found study libraries near *{context}*. "
                    "Tap below to choose your preferred space."
                )
                list_btn = "View libraries"
                list_section = "Libraries nearby"
                id_field = "library_id"
                entity_label = "Library"

            if components:
                result = service.send_template(
                    to=to_phone,
                    template_name=outbound.get("template_name") or default_name,
                    language_code=outbound.get("language") or default_lang,
                    components=components,
                    conversation_id=conversation_id,
                    defer_post_send=True,
                    broadcast_on_success=True,
                )
                if result.get("success"):
                    return result
                logger.warning(
                    "[api_node] carousel template Meta send failed: %s",
                    result.get("error"),
                )
            logger.warning(
                "[api_node] carousel template send failed; falling back to list (%s cards)",
                len(outbound.get("pending_buttons") or []),
            )
            pending = outbound.get("pending_buttons") or []
            if not pending:
                return {
                    "success": False,
                    "error": "Failed to build carousel template (image upload or template config)",
                }
            rows = []
            for btn in pending[:10]:
                rows.append(
                    {
                        "id": btn.get("id") or btn.get(id_field),
                        "title": (btn.get("label") or btn.get("title") or entity_label)[:24],
                        "description": (btn.get("label") or "")[:72],
                    }
                )
            return service.send_interactive_list(
                to=to_phone,
                body_text=list_body,
                button_text=list_btn,
                sections=[{"title": list_section, "rows": rows}],
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        return service.send_text(
            to=to_phone,
            text=str(outbound),
            conversation_id=conversation_id,
            defer_post_send=True,
            broadcast_on_success=True,
        )

    def _resolve_api_next_node_id(
        self,
        api_node: Dict[str, Any],
        handle: str,
        source_edges: dict,
        handle_edges: dict,
    ) -> Optional[str]:
        node_id = api_node.get("id")
        scoped = handle_edges.get(f"{node_id}:{handle}") if node_id else None
        if scoped:
            return scoped
        # Prefer this API node's own outgoing edge before global handle keys like "success".
        # Multiple API nodes share the same sourceHandle in the graph; the flat handle_edges
        # map keeps only the last one and would otherwise send every FAQ/post-booking API to CTA.
        for edge in source_edges.get(node_id, []):
            sh = edge.get("sourceHandle") or ""
            if sh == handle:
                return edge.get("target")
            if handle == "success" and sh in _API_DEFAULT_CONTINUE_HANDLES:
                return edge.get("target")
        if handle_edges.get(handle):
            return handle_edges.get(handle)
        if handle == "success":
            node_data = api_node.get("data") or {}
            default_next = node_data.get("defaultNextNodeId")
            if default_next:
                return default_next
            for edge in source_edges.get(node_id, []):
                if not edge.get("sourceHandle"):
                    return edge.get("target")
        return None

    def _resolve_api_button_next_node(
        self,
        state: WhatsAppConversationState,
        api_node: Dict[str, Any],
        button_payload: str,
        message_text: str,
        compiled_flow: Dict[str, Any],
        source_edges: dict,
        handle_edges: dict,
    ) -> Optional[str]:
        """Route dynamic API quickReply picks (library UUID, plan type, demo_today, …)."""
        node_data = api_node.get("data") or {}
        flow_config = compiled_flow.get("flow_config") or {}
        payload = str(button_payload or "").strip()
        if not payload:
            return None

        if self._looks_like_library_uuid(payload):
            repick_target = self._resolve_library_repick_target(compiled_flow)
            if repick_target:
                logger.info(
                    "[interactive_engine] Library re-pick uuid=%s → %s",
                    payload[:8],
                    repick_target,
                )
                return repick_target

        routed = self._resolve_quick_reply_route(flow_config, payload)
        if routed:
            return routed

        if payload in handle_edges:
            return handle_edges.get(payload)

        for option in self._extract_api_quick_reply_options(state, api_node):
            if str(option.get("id")) == payload:
                default_next = node_data.get("defaultNextNodeId")
                if default_next:
                    return default_next
                return self._resolve_default_continue_node_id(
                    api_node.get("id"),
                    compiled_flow.get("node_map") or {},
                    source_edges,
                    is_button_reply=True,
                )
            label = option.get("label") or ""
            if message_text and self._normalized_text(label) == self._normalized_text(message_text):
                if payload != str(option.get("id")):
                    continue
                default_next = node_data.get("defaultNextNodeId")
                if default_next:
                    return default_next

        default_next = node_data.get("defaultNextNodeId")
        if default_next:
            return default_next

        return self._resolve_default_continue_node_id(
            api_node.get("id"),
            compiled_flow.get("node_map") or {},
            source_edges,
            is_button_reply=True,
        )

    def _clear_pending_api_buttons(self, state: WhatsAppConversationState) -> None:
        state_data = state.state_data if isinstance(state.state_data, dict) else {}
        if "pending_api_buttons" in state_data:
            state_data = dict(state_data)
            state_data.pop("pending_api_buttons", None)
            state.state_data = state_data
            flag_modified(state, "state_data")

    def _resolve_default_continue_node_id(
        self,
        current_node_id: str,
        node_map: dict,
        source_edges: dict,
        *,
        is_button_reply: bool,
    ) -> Optional[str]:
        """Follow success/output/default edge — only on button picks, not free-text on API nodes."""
        current_node = node_map.get(current_node_id) or {}
        node_type = current_node.get("type")
        node_data = current_node.get("data") or {}

        if node_type == "api" and not is_button_reply:
            return None

        if is_button_reply:
            default_next = node_data.get("defaultNextNodeId")
            if default_next:
                return default_next

        for edge in source_edges.get(current_node_id, []):
            sh = edge.get("sourceHandle") or ""
            if sh in _API_DEFAULT_CONTINUE_HANDLES or (not sh and node_type != "api"):
                return edge.get("target")
        return None

    # ── Lead Node (mark-then-passthrough) ─────────────────────────────

    def _resolve_lead_next_node_id(
        self,
        lead_node: Dict[str, Any],
        source_edges: dict,
    ) -> Optional[str]:
        """Resolve the next node from a lead node's DEFAULT outgoing edge.

        A lead node sends no message and always passes through, so it routes on its
        single default/output/success edge (or an explicit data.targetNodeId)."""
        node_id = lead_node.get("id")
        node_data = lead_node.get("data") or {}

        explicit_next = (
            node_data.get("targetNodeId")
            or node_data.get("target_node_id")
            or node_data.get("defaultNextNodeId")
        )
        if explicit_next:
            return str(explicit_next).strip()

        edges = source_edges.get(node_id, [])
        # Prefer an explicit default/output/success handle, else the first unhandled edge.
        for edge in edges:
            sh = edge.get("sourceHandle") or ""
            if sh in _API_DEFAULT_CONTINUE_HANDLES:
                target = edge.get("target")
                if target:
                    return target
        for edge in edges:
            if not edge.get("sourceHandle"):
                target = edge.get("target")
                if target:
                    return target
        # Final fallback: a lead node has a single out-edge — take it regardless of handle
        # so an oddly-wired edge doesn't dead-end the flow.
        for edge in edges:
            target = edge.get("target")
            if target:
                return target
        return None

    def _execute_lead_node(
        self,
        state: WhatsAppConversationState,
        lead_node: Dict[str, Any],
        from_phone: str,
        node_map: dict,
        source_edges: dict,
        handle_edges: dict,
        captured_value: Any = None,
    ) -> Dict[str, Any]:
        """Mark a lead from a DEDICATED lead node, then pass through to the next node.

        A lead node sends NO message: it (a) marks/upserts the lead (best-effort,
        savepoint-isolated via _maybe_mark_lead) using a synthetic leadAction built
        from the node's own data, then (b) resolves its default outgoing edge and
        ADVANCES + renders the next node with the SAME dispatcher used after an API
        node (_continue_from_node), so the flow continues seamlessly.

        Guards against infinite loops (a lead node whose next is itself or another
        lead node already visited this inbound) and completes the flow on a
        missing/dangling next node."""
        node_id = lead_node.get("id")
        node_data = lead_node.get("data") or {}

        # Multi-lead-node loop guard: track lead nodes visited within this single inbound
        # message so a cycle (leadA → leadB → leadA) can't recurse forever. Reset per inbound
        # alongside _lead_hook_fired in process_incoming_message.
        visited = getattr(self, "_lead_nodes_visited", None)
        if visited is None:
            visited = set()
            self._lead_nodes_visited = visited
        if node_id in visited:
            logger.warning(
                "[interactive_engine] Lead node %s already visited this inbound — completing flow to avoid loop",
                node_id,
            )
            self._mark_flow_completed(state)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"success": True, "lead_node": True, "completed": True, "loop_guard": True}
        visited.add(node_id)

        # Build a synthetic enabled leadAction from the lead node's own data so the
        # shared LeadActionService logic (condition gate, mapFields, stage/leadType)
        # runs exactly as it does for the inline leadAction on other node types.
        synthetic_lead_action = {
            "enabled": True,
            "stage": node_data.get("stage"),
            "leadType": node_data.get("leadType"),
            "mapFields": node_data.get("mapFields"),
            "condition": node_data.get("condition"),
        }

        state.advance_to_node(node_id)
        state.clear_waiting_for_input()
        state.last_user_message_at = datetime.now(timezone.utc)

        # Mark the lead (best-effort; never raises into the flow).
        self._maybe_mark_lead(
            state,
            {"leadAction": synthetic_lead_action, "id": node_id},
            captured_value=captured_value,
        )

        # CRM lead status update from a status-bearing lead node — best-effort,
        # never breaks the flow. Only fires when the node explicitly carries a
        # target "status" (mode="set" sets the exact status, otherwise it is a
        # forward-only advance). The default stage-based path above is untouched.
        target_status = node_data.get("status")
        if target_status:
            mode = node_data.get("mode", "advance")
            try:
                from SocioviaCrm.lead_ingest import advance_lead_status, set_lead_status
                ext_id = str(state.conversation_id) if state.conversation_id is not None else None
                if mode == "set":
                    set_lead_status(
                        workspace_id=self.workspace_id,
                        phone=getattr(state, "phone_number", None),
                        external_id=ext_id,
                        target_status=target_status,
                        reason="Set by flow node",
                        db_session=db.session,
                    )
                else:
                    advance_lead_status(
                        workspace_id=self.workspace_id,
                        phone=getattr(state, "phone_number", None),
                        external_id=ext_id,
                        target_status=target_status,
                        reason="Advanced by flow node",
                        db_session=db.session,
                    )
            except Exception as e:
                logger.warning(
                    f"[interactive_engine] lead node CRM status update failed "
                    f"(mode={mode}, target={target_status}): {e}"
                )

        next_node_id = self._resolve_lead_next_node_id(lead_node, source_edges)

        # Loop guard: a dedicated lead node that routes back to itself must not spin.
        if next_node_id == node_id:
            logger.warning(
                "[interactive_engine] Lead node %s routes to itself — completing flow to avoid loop",
                node_id,
            )
            next_node_id = None

        if not next_node_id or not node_map.get(next_node_id):
            # Dangling / missing next node — pass-through has nowhere to go; complete.
            self._mark_flow_completed(state)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"success": True, "lead_node": True, "completed": True}

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

        return self._continue_from_node(
            state=state,
            next_node_id=next_node_id,
            from_phone=from_phone,
            node_map=node_map,
            source_edges=source_edges,
            handle_edges=handle_edges,
        )

    def _execute_api_node(
        self,
        state: WhatsAppConversationState,
        api_node: Dict[str, Any],
        from_phone: str,
        node_map: dict,
        source_edges: dict,
        handle_edges: dict,
    ) -> Dict[str, Any]:
        node_data = api_node.get("data") or {}
        compiled_flow = self._get_compiled_flow(state.automation_id)
        variables = self._build_flow_variables(state, from_phone, compiled_flow)

        url_hint = str(node_data.get("url") or "")
        if "/libraries/plans" in url_hint and not variables.get("library_id"):
            logger.warning(
                "[api_node] /libraries/plans called without library_id state=%s collected=%s",
                state.id,
                list((state.get_collected_fields() or {}).keys()),
            )

        # --- CRM/mutating idempotency guard ----------------------------------
        # A mutating API node (e.g. a CRM lead/booking POST) must run at most once per
        # flow state. Re-entries — resume_paused_step re-render, nfm/button recovery,
        # retried webhook delivery — would otherwise create DUPLICATE leads. GET nodes
        # (e.g. /libraries/plans that re-render cards on resume) are intentionally NOT
        # deduped, so resume still re-renders them.
        node_id = api_node.get("id")
        method = str(node_data.get("method") or "GET").upper()
        is_mutating = method in ("POST", "PUT", "PATCH", "DELETE")
        _sd_now = state.state_data if isinstance(state.state_data, dict) else {}
        if is_mutating and bool((_sd_now.get("api_pushed") or {}).get(str(node_id))):
            logger.info(
                "[api_node] Skipping duplicate %s for node=%s state=%s (already pushed) — "
                "prevents duplicate CRM lead on resume/recovery/webhook-retry",
                method, node_id, state.id,
            )
            state.advance_to_node(node_id)
            state.clear_waiting_for_input()
            state.last_user_message_at = datetime.now(timezone.utc)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            next_node_id = self._resolve_api_next_node_id(
                api_node, "success", source_edges, handle_edges
            )
            if not next_node_id:
                self._mark_flow_completed(state)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return {"success": True, "api_node": True, "handle": "success", "deduped": True, "completed": True}
            if not node_map.get(next_node_id):
                # Dangling edge after a deduped push — don't strand the flow on a missing
                # node; treat as completed.
                self._mark_flow_completed(state)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return {"success": True, "api_node": True, "handle": "success", "deduped": True, "completed": True}
            return self._continue_from_node(
                state=state,
                next_node_id=next_node_id,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        api_result = api_node_executor.execute_api_node(node_data, variables)
        store_as = node_data.get("storeAs")
        if store_as:
            state.set_collected_field(store_as, api_result.parsed if api_result.parsed is not None else api_result.raw_text)

        # ── Flow→Lead hook (api result) ──
        self._maybe_mark_lead(
            state,
            node_data,
            captured_value=api_result.parsed if api_result.parsed is not None else api_result.raw_text,
            api_result=api_result,
        )

        # Record a successful MUTATING push so re-entries don't POST again (idempotency).
        # Commit the marker in its OWN transaction immediately, so even if the later
        # state/advance commit fails we never re-POST and create a duplicate lead.
        if is_mutating and api_result.success:
            _sd_mark = dict(state.state_data or {})
            _pushed = dict(_sd_mark.get("api_pushed") or {})
            _pushed[str(node_id)] = True
            _sd_mark["api_pushed"] = _pushed
            state.state_data = _sd_mark
            flag_modified(state, "state_data")
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        state.advance_to_node(api_node.get("id"))
        state.clear_waiting_for_input()
        state.last_user_message_at = datetime.now(timezone.utc)
        flag_modified(state, "state_data")

        send_ok = True
        interactive_delivered = False
        delivered_pick_messages: List[Dict[str, Any]] = []
        for outbound in api_result.outbound_messages:
            send_result = self._send_api_outbound(from_phone, outbound, state.conversation_id)
            otype = (outbound.get("type") or "").lower()
            if send_result.get("success"):
                if otype in api_node_executor.API_USER_PICK_OUTBOUND_TYPES:
                    interactive_delivered = True
                    delivered_pick_messages.append(outbound)
            else:
                send_ok = False
                logger.warning(
                    "[api_node] Outbound send failed type=%s error=%s",
                    otype,
                    send_result.get("error"),
                )

        has_interactive = interactive_delivered
        if has_interactive:
            pending_buttons = api_node_executor.extract_pending_api_buttons(
                delivered_pick_messages or api_result.outbound_messages
            )
            state_data = dict(state.state_data or {})
            state_data["pending_api_buttons"] = pending_buttons
            state.state_data = state_data
            flag_modified(state, "state_data")

        # Never auto-chain after API nodes that show quick-reply buttons — wait for user pick.
        # Branch handles (e.g. not_found) still auto-route immediately.
        next_node_id = None
        if not has_interactive or str(api_result.handle or "").startswith("branch-"):
            next_node_id = self._resolve_api_next_node_id(
                api_node, api_result.handle, source_edges, handle_edges
            )
            if next_node_id:
                candidate = node_map.get(next_node_id)
                if self._node_is_internal_router(candidate) and not has_interactive:
                    next_node_id = None

        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.warning("[api_node] commit failed: %s", exc)
            return {"success": False, "error": "Failed to persist API node state"}

        if not next_node_id:
            if api_result.success:
                if has_interactive:
                    return {
                        "success": send_ok,
                        "api_node": True,
                        "handle": api_result.handle,
                        "status_code": api_result.status_code,
                        "waiting_for_button": True,
                    }
                self._mark_flow_completed(state)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return {
                    "success": send_ok,
                    "api_node": True,
                    "handle": api_result.handle,
                    "status_code": api_result.status_code,
                    "completed": True,
                }
            return {
                "success": False,
                "api_node": True,
                "handle": api_result.handle,
                "error": api_result.error or "API request failed",
            }

        next_node = node_map.get(next_node_id)
        if not next_node:
            return {"success": False, "api_node": True, "error": "Next node not found"}

        return self._continue_from_node(
            state=state,
            next_node_id=next_node_id,
            from_phone=from_phone,
            node_map=node_map,
            source_edges=source_edges,
            handle_edges=handle_edges,
        )

    def _continue_from_node(
        self,
        state: WhatsAppConversationState,
        next_node_id: str,
        from_phone: str,
        node_map: dict,
        source_edges: dict,
        handle_edges: dict,
        button_payload: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Continue flow execution from a specific node id (post-API auto chain)."""
        next_node = node_map.get(next_node_id)
        if not next_node:
            return {"success": False, "error": "Node not found"}

        if next_node.get("type") == "input":
            return self._send_input_question(
                state=state,
                input_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )
        if next_node.get("type") == "api":
            state.advance_to_node(next_node_id)
            return self._execute_api_node(
                state=state,
                api_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        # Lead node: mark-then-passthrough (sends no message, routes to its next node).
        if next_node.get("type") == "lead":
            return self._execute_lead_node(
                state=state,
                lead_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
                captured_value=button_payload,
            )

        state.advance_to_node(next_node_id)
        if button_payload:
            state.record_button_click(button_payload)
        state.last_user_message_at = datetime.now(timezone.utc)

        if next_node.get("type") == "end":
            self._mark_flow_completed(state)
            end_message = next_node.get("data", {}).get("message")
            if end_message:
                result = self._send_text_message(from_phone, end_message, state.conversation_id)
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return result
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"completed": True, "message": "Flow completed"}

        if self._node_is_internal_router(next_node):
            if button_payload:
                routed = handle_edges.get(button_payload)
                if not routed:
                    flow_config = (self._get_compiled_flow(state.automation_id) or {}).get("flow_config") or {}
                    routed = self._resolve_quick_reply_route(flow_config, button_payload)
                if routed:
                    return self._continue_from_node(
                        state=state,
                        next_node_id=routed,
                        from_phone=from_phone,
                        node_map=node_map,
                        source_edges=source_edges,
                        handle_edges=handle_edges,
                        button_payload=button_payload,
                    )
            state.advance_to_node(next_node_id)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"success": True, "internal_router": True, "waiting_for_button": True}

        if next_node.get("type") == "template":
            result = self._send_template_node(
                state.automation_id,
                next_node,
                from_phone,
                state,
                node_map,
                source_edges,
                handle_edges,
            )
            if result and result.get("success"):
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            else:
                db.session.rollback()
            return result

        result = self._send_node_message(
            state.automation_id,
            next_node,
            from_phone,
            state,
            node_map,
            source_edges,
            handle_edges,
        )
        if result and result.get("success"):
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
        else:
            db.session.rollback()
        return result

    # ── Input Node Methods ──────────────────────────────────────────

    def _send_input_question(
        self,
        state: WhatsAppConversationState,
        input_node: Dict[str, Any],
        from_phone: str,
        node_map: dict = None,
        source_edges: dict = None,
        handle_edges: dict = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send an input question to the user and lock state for input."""
        node_data = input_node.get("data", {})
        field = node_data.get("field", "unknown")

        # Advance to the input node and mark waiting
        state.advance_to_node(input_node.get("id"))
        state.set_waiting_for_input(field)

        # Build question with variable substitution
        collected = state.get_collected_fields()
        question_text = input_node_handler.build_question_message(
            node_data, collected, error_message=error_message
        )

        # Send the question
        result = self._send_text_message(from_phone, question_text, state.conversation_id)

        if result.get("success"):
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"[interactive_engine] commit failed at input question send: {e}")

            logger.info(
                f"[interactive_engine] Sent input question for field '{field}' "
                f"to {from_phone}, node={input_node.get('id')}"
            )
            return {
                "success": True,
                "automation_id": state.automation_id,
                "node_id": input_node.get("id"),
                "input_node": True,
                "waiting_for_field": field,
            }
        else:
            db.session.rollback()
            logger.error(f"[interactive_engine] Failed to send input question: {result.get('error')}")
            return {"success": False, "error": result.get("error")}

    def _handle_input_node_response(
        self,
        state: WhatsAppConversationState,
        message_text: str,
        from_phone: str,
        current_node: Dict[str, Any],
        node_map: dict,
        source_edges: dict,
        handle_edges: dict,
        inbound_wamid: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process user's text response to an input node.

        This runs under STRICT INPUT LOCK — no AI, no trigger matching.
        Only input processing happens here.
        """
        node_data = current_node.get("data", {})
        node_id = current_node.get("id")

        def _mark_input_wamid_handled() -> None:
            if not inbound_wamid:
                return
            sd = state.state_data if isinstance(state.state_data, dict) else {}
            sd = dict(sd)
            sd["last_input_handled_wamid"] = inbound_wamid
            state.state_data = sd
            flag_modified(state, "state_data")

        if inbound_wamid:
            sd0 = state.state_data if isinstance(state.state_data, dict) else {}
            if sd0.get("last_input_handled_wamid") == inbound_wamid:
                logger.info(
                    "[interactive_engine] Skipping duplicate inbound wamid=%s for input state=%s",
                    inbound_wamid,
                    state.id,
                )
                return {"success": True, "duplicate_inbound_skipped": True}

        # Classify: is this genuinely unrelated (new query) or an input attempt?
        intent = self._classify_unmatched_mid_flow_input(message_text, current_node, state=state)
        if intent == "new_query":
            # Pause flow for AI takeover (same pattern as existing engine)
            state_data = state.state_data if isinstance(state.state_data, dict) else {}
            state_data.update({
                "paused": True,
                "paused_at": datetime.now(timezone.utc).isoformat(),
                "paused_node_id": state.current_node_id,
                "pause_reason": "new_query_during_input",
            })
            state.state_data = state_data
            state.is_active = False
            # Do NOT set completed_at on pause — see note in the new_query pause path.
            state.updated_at = datetime.now(timezone.utc)
            _clear_cached_active_state_id(self.workspace_id, state.conversation_id)
            _mark_input_wamid_handled()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
                return {"success": False, "error": "failed_to_pause_flow"}

            logger.info(
                "[interactivePause] ✅ PAUSED input flow: state=%s for AI takeover "
                "conversation=%s text='%s'",
                state.id, state.conversation_id, message_text,
            )
            return {
                "success": True,
                "paused": True,
                "allow_ai_takeover": True,
                "reason": "new_query_during_input",
            }

        # Process the input
        result = input_node_handler.process_input_response(
            message_text=message_text,
            node_data=node_data,
            state=state,
        )

        # Handle correction — re-ask current question with confirmation
        if result.get("is_correction") and result.get("valid"):
            corrected_field = result.get("corrected_field", "")
            corrected_value = result.get("value")
            confirm_text = f"✅ Updated {corrected_field} to: {corrected_value}"
            self._send_text_message(from_phone, confirm_text, state.conversation_id)
            # Re-send current question (they corrected a previous field)
            collected = state.get_collected_fields()
            question_text = input_node_handler.build_question_message(node_data, collected)
            result_send = self._send_text_message(from_phone, question_text, state.conversation_id)
            try:
                if result_send.get("success"):
                    _mark_input_wamid_handled()
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {
                "success": True,
                "input_correction": True,
                "corrected_field": corrected_field,
            }

        # Invalid input — one bubble: custom error + same prompt (instant together)
        if not result.get("valid"):
            error_msg = (result.get("error_message") or "").strip() or (
                "That doesn't look right for this step. Please try again."
            )
            collected = state.get_collected_fields()
            combined = input_node_handler.build_question_message(
                node_data, collected, error_message=error_msg
            )
            send_result = self._send_text_message(from_phone, combined, state.conversation_id)
            if send_result.get("success"):
                try:
                    state.last_user_message_at = datetime.now(timezone.utc)
                    state.updated_at = datetime.now(timezone.utc)
                    _mark_input_wamid_handled()
                    db.session.commit()
                except Exception:
                    db.session.rollback()
            else:
                db.session.rollback()

            return {
                "success": True,
                "input_validation_failed": True,
                "field": node_data.get("field"),
                "error": error_msg,
            }

        # ── Valid input — advance to next node ──
        _mark_input_wamid_handled()
        state.last_user_message_at = datetime.now(timezone.utc)
        state.updated_at = datetime.now(timezone.utc)

        # ── Flow→Lead hook (input capture) ──
        self._maybe_mark_lead(
            state,
            node_data,
            captured_value=result.get("value"),
            message_text=message_text,
        )

        # Find next node — prefer data.targetNodeId when multiple default/output edges
        # (React Flow order can otherwise pick a duplicate prompt message node first).
        outgoing: List[str] = []
        for edge in source_edges.get(node_id, []):
            h = edge.get("sourceHandle")
            if not h or h in ("output", "default"):
                t = edge.get("target")
                if t:
                    outgoing.append(t)

        next_node_id = None
        explicit_next = node_data.get("targetNodeId") or node_data.get("target_node_id")
        if explicit_next:
            ex = str(explicit_next).strip()
            if ex in outgoing:
                next_node_id = ex
        if not next_node_id and outgoing:
            next_node_id = outgoing[0]

        if not next_node_id:
            # End of flow
            self._mark_flow_completed(state)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"completed": True, "message": "Input flow completed"}

        next_node = node_map.get(next_node_id)
        if not next_node:
            self._mark_flow_completed(state)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"completed": True, "message": "Flow completed (next node not found)"}

        # Next node is another input node → send question and wait
        if next_node.get("type") == "input":
            return self._send_input_question(
                state=state,
                input_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        # Next node is end node
        if next_node.get("type") == "end":
            state.advance_to_node(next_node_id)
            self._mark_flow_completed(state)
            end_message = next_node.get("data", {}).get("message")
            if end_message:
                collected = state.get_collected_fields()
                end_message = input_node_handler.substitute_variables(end_message, collected)
                send_result = self._send_text_message(from_phone, end_message, state.conversation_id)
                if send_result.get("success"):
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                else:
                    db.session.rollback()
                return send_result
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {"completed": True, "message": "Input flow completed"}

        # Next node is API — execute HTTP call and send mapped response (not the request body).
        if next_node.get("type") == "api":
            state.advance_to_node(next_node_id)
            state.clear_waiting_for_input()
            state.last_user_message_at = datetime.now(timezone.utc)
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return self._execute_api_node(
                state=state,
                api_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
            )

        # Next node is a lead node — mark lead then pass through (sends nothing).
        if next_node.get("type") == "lead":
            state.clear_waiting_for_input()
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return self._execute_lead_node(
                state=state,
                lead_node=next_node,
                from_phone=from_phone,
                node_map=node_map,
                source_edges=source_edges,
                handle_edges=handle_edges,
                captured_value=result.get("value"),
            )

        # Next node is message/template — advance and send with variable substitution
        state.advance_to_node(next_node_id)

        if next_node.get("type") == "template":
            result = self._send_template_node(
                state.automation_id, next_node, from_phone, state,
                node_map, source_edges, handle_edges,
            )
        else:
            # Substitute variables in message body before sending
            next_data = next_node.get("data", {})
            collected = state.get_collected_fields()
            if collected and next_data.get("body"):
                next_data["body"] = input_node_handler.substitute_variables(
                    next_data["body"], collected
                )
                if next_data.get("header"):
                    next_data["header"] = input_node_handler.substitute_variables(
                        next_data["header"], collected
                    )
                if next_data.get("footer"):
                    next_data["footer"] = input_node_handler.substitute_variables(
                        next_data["footer"], collected
                    )

            result = self._send_node_message(
                state.automation_id, next_node, from_phone, state,
                node_map, source_edges, handle_edges,
            )

        if result and result.get("success"):
            try:
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logger.warning(f"[interactive_engine] commit after input-advance failed: {e}")
        else:
            db.session.rollback()

        return result

    def _substitute_flow_variables(
        self,
        text: str,
        state: WhatsAppConversationState,
    ) -> str:
        """Substitute {{field}} variables from collected input data."""
        if not text or "{{" not in text:
            return text
        collected = state.get_collected_fields()
        if not collected:
            return text
        return input_node_handler.substitute_variables(text, collected)

    # ── Send Message Node ─────────────────────────────────────────────

    def _send_node_message(
        self,
        automation,
        node: Dict[str, Any],
        to_phone: str,
        state: WhatsAppConversationState,
        node_map: dict = None,
        source_edges: dict = None,
        handle_edges: dict = None,
    ) -> Dict[str, Any]:
        """Send a message node's content. Supports text, image, video, document headers."""
        t_send = time.time()

        node_data = node.get("data", {})
        body = node_data.get("body", "")
        header = node_data.get("header")
        footer = node_data.get("footer")
        buttons = node_data.get("buttons", [])

        if node_data.get("internalRouter"):
            state.advance_to_node(node.get("id"))
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()
            return {
                "success": True,
                "automation_id": automation_id if isinstance(automation, int) else automation,
                "node_id": node.get("id"),
                "internal_router": True,
            }

        # Header media — try both camelCase and snake_case
        header_image_url = node_data.get("headerImageUrl") or node_data.get("header_image_url")
        header_video_url = node_data.get("headerVideoUrl") or node_data.get("header_video_url")
        header_document_url = node_data.get("headerDocumentUrl") or node_data.get("header_document_url")
        header_document_filename = node_data.get("headerDocumentFilename") or node_data.get("header_document_filename")

        service = self._get_service()
        if not service:
            return {"error": "Account not found"}

        automation_id, node_map, source_edges, handle_edges = self._resolve_flow_context(
            automation,
            node_map=node_map,
            source_edges=source_edges,
            handle_edges=handle_edges,
        )
        if automation_id is None:
            return {"success": False, "error": "Automation flow not found"}

        # Classify buttons
        interactive_buttons = []
        call_buttons = []
        url_buttons = []

        for idx, button in enumerate(buttons):
            action = button.get("action", {})
            action_type = action.get("type", "quick_reply")
            button_label = (
                button.get("label") or button.get("title") or
                button.get("text") or action.get("label") or "Button"
            )
            button_id = button.get("id", f"btn_{idx}")

            if action_type in ("quick_reply", "reply", "send_document", None):
                has_connection = button_id in handle_edges
                if has_connection or action_type == "send_document":
                    title = button_label if button_label else f"Option {idx + 1}"
                    interactive_buttons.append({
                        "type": "reply",
                        "reply": {"id": button_id, "title": title[:100]}
                    })
            elif action_type == "call":
                phone_number = action.get("phoneNumber") or action.get("phone") or action.get("value")
                if phone_number:
                    call_buttons.append({"label": button_label, "phone": phone_number})
            elif action_type == "url":
                url = action.get("url") or action.get("value")
                if url:
                    url_buttons.append({"label": button_label, "url": url})

        # Terminal node with action buttons only
        if not interactive_buttons and (call_buttons or url_buttons):
            state.complete()

        if not body:
            body = "Please select an option:"

        # Append call/URL button info to body
        for call_btn in call_buttons:
            body += f"\n\n📞 {call_btn['label']}: {call_btn['phone']}"
        for url_btn in url_buttons:
            body += f"\n\n🔗 {url_btn['label']}: {url_btn['url']}"

        # Send based on message type
        has_media_header = header_image_url or header_video_url or header_document_url

        if node_data.get("interactiveType") == "list":
            result = self._send_list_message(
                service,
                to_phone,
                body,
                node_data,
                header if not has_media_header else None,
                footer,
                state.conversation_id,
            )
        elif interactive_buttons:
            result = service.send_interactive_buttons(
                to=to_phone,
                body_text=body,
                buttons=interactive_buttons,
                header_text=header if not has_media_header else None,
                header_image_url=header_image_url,
                header_video_url=header_video_url,
                header_document_url=header_document_url,
                header_document_filename=header_document_filename,
                footer_text=footer,
                conversation_id=state.conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        elif header_image_url:
            result = service.send_image(
                to=to_phone,
                image_url=header_image_url,
                caption=body,
                conversation_id=state.conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        elif header_video_url:
            result = service.send_video(
                to=to_phone,
                video_url=header_video_url,
                caption=body,
                conversation_id=state.conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        elif header_document_url:
            result = service.send_document(
                to=to_phone,
                document_url=header_document_url,
                caption=body,
                filename=header_document_filename,
                conversation_id=state.conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        else:
            result = service.send_text(
                to=to_phone,
                text=body,
                conversation_id=state.conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )

        send_ms = (time.time() - t_send) * 1000
        logger.info(f"[interactive_engine] send_node_message in {send_ms:.0f}ms")

        if result.get("success"):
            node_data = node.get("data") or {}
            # Auto-continue for terminal call/URL button nodes
            if not interactive_buttons and (call_buttons or url_buttons):
                return self._auto_continue(
                    automation_id,
                    node,
                    to_phone,
                    state,
                    node_map,
                    source_edges,
                    handle_edges,
                    buttons,
                )

            # Plain-text bridge messages (e.g. amenities blurb → API plans)
            if (
                not interactive_buttons
                and not call_buttons
                and not url_buttons
                and not node_data.get("internalRouter")
                and not node_data.get("terminal")
            ):
                out_edges = [
                    e
                    for e in source_edges.get(node.get("id"), [])
                    if (e.get("sourceHandle") or "output") in ("output", "default", "")
                ]
                if len(out_edges) == 1:
                    ac = self._auto_continue(
                        automation_id,
                        node,
                        to_phone,
                        state,
                        node_map,
                        source_edges,
                        handle_edges,
                        buttons,
                    )
                    if ac:
                        return ac

            return {
                "success": True,
                "automation_id": automation_id,
                "node_id": node.get("id"),
                "message_id": result.get("message_id"),
            }
        else:
            logger.error(f"Failed to send automation message: {result.get('error')}")
            return {"success": False, "error": result.get("error")}

    def _send_list_message(self, service, to_phone, body, node_data, header, footer, conversation_id):
        """Send an interactive list message."""
        # WhatsApp requires a non-empty interactive body; default rather than fail.
        body = (body or "").strip() or "Please choose an option"
        # WhatsApp button text limit is 20 chars.
        list_button_text = (node_data.get("buttonText") or "Select Options")[:20]
        raw_sections = node_data.get("sections", [])
        formatted_sections = []

        for section in raw_sections:
            rows = []
            for row in section.get("rows", []):
                row_id = row.get("id")
                title = row.get("title") or "Select"
                description = row.get("description")
                if row_id:
                    # WhatsApp hard limits: row title <=24, description <=72.
                    # Over-length values make Meta reject the whole message.
                    row_data = {"id": row_id, "title": title[:24]}
                    if description:
                        row_data["description"] = description[:72]
                    rows.append(row_data)
            if rows:
                section_data = {"rows": rows}
                if section.get("title"):
                    section_data["title"] = section.get("title")[:24]
                formatted_sections.append(section_data)

        # WhatsApp requires a non-empty `title` on EVERY section once a list has
        # more than one section (a single section may be untitled). Flows mis-built
        # with several untitled single-row sections — one "option" per section —
        # are rejected by Meta (131009); the failed send is then rolled back and the
        # flow silently stalls at the previous node (no message, no advance). Repair
        # by collapsing all rows into one untitled section, matching the valid
        # single-section shape. Capped at WhatsApp's 10-row hard limit.
        if len(formatted_sections) > 1 and any(not s.get("title") for s in formatted_sections):
            merged_rows = []
            for s in formatted_sections:
                merged_rows.extend(s.get("rows", []))
            if len(merged_rows) > 10:
                logger.warning(
                    "[interactive_engine] list node has %d rows across %d untitled sections; "
                    "dropping %d row(s) beyond WhatsApp's 10-row limit (conversation=%s). Dropped ids=%s",
                    len(merged_rows), len(formatted_sections), len(merged_rows) - 10,
                    conversation_id, [r.get("id") for r in merged_rows[10:]],
                )
            formatted_sections = [{"rows": merged_rows[:10]}]

        if formatted_sections:
            return service.send_interactive_list(
                to=to_phone, body_text=body, button_text=list_button_text,
                sections=formatted_sections,
                header_text=header,
                footer_text=footer,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )
        else:
            return service.send_text(
                to=to_phone,
                text=body,
                conversation_id=conversation_id,
                defer_post_send=True,
                broadcast_on_success=True,
            )

    def _broadcast_message(self, result: dict, state: WhatsAppConversationState):
        """Broadcast sent message via SSE for real-time inbox update."""
        try:
            message_id = result.get("message_id")
            conversation_id = result.get("conversation_id") or state.conversation_id
            msg_record = WhatsAppMessage.query.get(message_id)
            if msg_record:
                notification_manager.broadcast("whatsapp_message_received", {
                    "message": msg_record.to_dict(),
                    "conversation_id": conversation_id,
                    "account_id": self.account_id,
                    "workspace_id": self.workspace_id
                })
        except Exception as e:
            logger.error(f"Failed to broadcast automation message: {e}")

    def _auto_continue(self, automation, node, to_phone, state, node_map, source_edges, handle_edges, buttons):
        """Auto-continue to next node after call/URL button terminal nodes."""
        automation_id, node_map, source_edges, handle_edges = self._resolve_flow_context(
            automation,
            node_map=node_map,
            source_edges=source_edges,
            handle_edges=handle_edges,
        )
        current_node_id = node.get("id")
        next_node_id = None

        # Check edges from this node
        for edge in source_edges.get(current_node_id, []):
            next_node_id = edge.get("target")
            break
        # Check button edges
        if not next_node_id:
            for btn in buttons:
                btn_id = btn.get("id")
                if btn_id and btn_id in handle_edges:
                    next_node_id = handle_edges[btn_id]
                    break

        if next_node_id and node_map:
            next_node = node_map.get(next_node_id)
            if next_node:
                state.advance_to_node(next_node_id)
                state.last_user_message_at = datetime.now(timezone.utc)
                if next_node.get("type") == "end":
                    self._mark_flow_completed(state)
                    end_message = next_node.get("data", {}).get("message")
                    if end_message:
                        self._send_text_message(to_phone, end_message, state.conversation_id)
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return {"success": True, "completed": True, "message": "Flow completed"}
                if next_node.get("type") == "api":
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return self._execute_api_node(
                        state=state,
                        api_node=next_node,
                        from_phone=to_phone,
                        node_map=node_map,
                        source_edges=source_edges,
                        handle_edges=handle_edges,
                    )
                if next_node.get("type") == "lead":
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return self._execute_lead_node(
                        state=state,
                        lead_node=next_node,
                        from_phone=to_phone,
                        node_map=node_map,
                        source_edges=source_edges,
                        handle_edges=handle_edges,
                        captured_value=None,
                    )
                if next_node.get("type") == "input":
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return self._send_input_question(
                        state=state,
                        input_node=next_node,
                        from_phone=to_phone,
                        node_map=node_map,
                        source_edges=source_edges,
                        handle_edges=handle_edges,
                    )
                if next_node.get("type") == "template":
                    try:
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                    return self._send_template_node(
                        automation_id,
                        next_node,
                        to_phone,
                        state,
                        node_map,
                        source_edges,
                        handle_edges,
                    )
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                return self._send_node_message(
                    automation_id,
                    next_node,
                    to_phone,
                    state,
                    node_map,
                    source_edges,
                    handle_edges,
                )

        return {"success": True, "automation_id": automation_id, "node_id": node.get("id")}

    # ── Send Text Message ─────────────────────────────────────────────

    def _send_text_message(self, to_phone: str, text: str, conversation_id: int = None) -> Dict[str, Any]:
        """Send a simple text message."""
        service = self._get_service()
        if not service:
            return {"error": "Account not found"}

        result = service.send_text(
            to=to_phone,
            text=text,
        )

        # Defer SSE broadcast so webhook / automation path returns faster (broadcast uses its own DB pool).
        if result.get("success") and result.get("message"):
            account = self._get_account()
            acc_id = account.id if account else None
            ws_id = self.workspace_id
            conv_id = result.get("conversation_id", conversation_id)
            msg_payload = result["message"]

            def _bg_broadcast() -> None:
                try:
                    notification_manager.broadcast(
                        "whatsapp_message_received",
                        {
                            "message": msg_payload,
                            "conversation_id": conv_id,
                            "account_id": acc_id,
                            "workspace_id": ws_id,
                        },
                    )
                except Exception as e:
                    logger.error(f"Failed to broadcast text message event: {e}")

            threading.Thread(target=_bg_broadcast, daemon=True).start()

        return {"success": result.get("success", False), "message_id": result.get("message_id")}

    # ── Send Template Node ────────────────────────────────────────────

    def _send_template_node(
        self,
        automation,
        node: Dict[str, Any],
        to_phone: str,
        state: WhatsAppConversationState,
        node_map: dict = None,
        source_edges: dict = None,
        handle_edges: dict = None,
    ) -> Dict[str, Any]:
        """Send a template node's message to the user."""
        node_data = node.get("data", {})
        template_id = node_data.get("template_id") or node_data.get("templateId")
        template_name = node_data.get("template_name") or node_data.get("templateName")
        raw_button_mappings = node_data.get("button_mappings") or node_data.get("buttonMappings") or {}
        variables = node_data.get("variables", {})

        # Normalize button mappings
        button_mappings = {}
        if isinstance(raw_button_mappings, list):
            for mapping in raw_button_mappings:
                if not isinstance(mapping, dict):
                    continue
                target = mapping.get("targetNodeId") or mapping.get("target_node_id")
                if target is None:
                    continue
                btn_index = mapping.get("buttonIndex")
                btn_text = mapping.get("buttonText")
                if btn_index is not None:
                    button_mappings[str(btn_index)] = target
                    button_mappings[f"button_{btn_index}"] = target
                if btn_text:
                    button_mappings[str(btn_text)] = target
        elif isinstance(raw_button_mappings, dict):
            button_mappings = raw_button_mappings

        if not template_id and not template_name:
            logger.error(f"Template node {node.get('id')} has no template configured")
            return {"error": "No template configured"}

        service = self._get_service()
        if not service:
            return {"error": "Account not found"}

        automation_id, node_map, source_edges, handle_edges = self._resolve_flow_context(
            automation,
            node_map=node_map,
            source_edges=source_edges,
            handle_edges=handle_edges,
        )
        if automation_id is None:
            return {"success": False, "error": "Automation flow not found"}

        # Template lookup is workspace-scoped and cached for low-latency continuation.
        template = self._get_cached_template(template_id, template_name)

        if not template:
            logger.error(f"Template {template_id or template_name} not found")
            return {"error": "Template not found"}

        # Prepare runtime variables
        runtime_variables = self._get_runtime_variables(to_phone, state)
        runtime_variables.update(variables)

        # Build components
        components = TemplateNodeExecutor.build_template_components(
            template=template,
            automation_id=automation_id,
            button_mappings=button_mappings,
            variables=runtime_variables,
        )

        # Send
        result = service.send_template(
            to=to_phone,
            template_name=template.name,
            language_code=template.language or "en",
            components=components,
            conversation_id=state.conversation_id,
            defer_post_send=True,
            broadcast_on_success=True,
        )

        if result.get("success"):
            logger.info(f"Sent template {template.name} to {to_phone}")

            # ── Flow→Lead hook (TEMPLATE node, after successful send) ──
            # Best-effort + savepoint-isolated + per-inbound deduped inside _maybe_mark_lead;
            # no-op unless the node carries an enabled leadAction. captured_value = node id so a
            # template send (which has no user-captured value) still has a stable dedup key.
            lead_node_data = dict(node_data)
            lead_node_data["id"] = node.get("id")
            self._maybe_mark_lead(
                state,
                lead_node_data,
                captured_value=node.get("id"),
            )

            if not self._template_waits_for_user_input(template, raw_button_mappings):
                state.complete()

            return {
                "success": True,
                "automation_id": automation_id,
                "node_id": node.get("id"),
                "message_id": result.get("message_id"),
                "template": template.name,
            }
        else:
            logger.error(f"Failed to send template: {result.get('error')}")
            return {"success": False, "error": result.get("error")}

    def _get_runtime_variables(
        self, to_phone: str, state: WhatsAppConversationState
    ) -> Dict[str, str]:
        """Built-in runtime placeholders (phone, contact). Flow secrets come from automation.variables in DB."""
        phone = normalize_phone(to_phone) or str(to_phone or "").strip()
        variables = {"phone": phone}
        try:
            conversation = WhatsAppConversation.query.get(state.conversation_id)
            if conversation:
                variables["contact_name"] = conversation.contact_name or ""
                variables["customer_name"] = conversation.contact_name or ""
        except Exception as e:
            logger.debug(f"Could not get conversation info: {e}")
        return variables


def message_might_match_interactive_keyword(
    account_id: int,
    workspace_id: str,
    message_text: str,
) -> bool:
    """
    Cheap pre-check using the cached trigger index — no DB round-trip per keyword.
    Used by fast_router to avoid skipping keyword-triggered flows for returning users.
    """
    text = (message_text or "").strip()
    if not text:
        return False
    try:
        engine = InteractiveAutomationEngine(account_id=account_id, workspace_id=workspace_id)
        index = engine._get_trigger_index()
        if not (
            index.get("keyword_map")
            or index.get("exact_map")
            or index.get("keyword_contains")
        ):
            return False

        msg_lower = text.lower()
        if index["exact_map"].get(msg_lower):
            return True
        if index["keyword_map"].get(msg_lower):
            return True
        for word in re.findall(r"\w+", msg_lower):
            if word in index["keyword_map"]:
                return True
        for keyword, _ in index.get("keyword_contains", []):
            if _keyword_matches_message(msg_lower, keyword):
                return True
        return False
    except Exception as exc:
        logger.debug("[interactive_engine] keyword pre-check failed: %s", exc)
        return False


def conversation_has_interactive_routing_context(conversation_id: int, workspace_id: str) -> bool:
    """Fast check: active or paused visual-flow state exists for this conversation."""
    cached_state_id = _get_cached_active_state_id(workspace_id, conversation_id)
    if cached_state_id:
        return True

    try:
        from .visual_automation_models import WhatsAppConversationState

        state = (
            WhatsAppConversationState.query.filter_by(
                conversation_id=conversation_id,
                is_active=True,
            )
            .limit(1)
            .first()
        )
        if state:
            return True

        paused = (
            WhatsAppConversationState.query.filter_by(
                conversation_id=conversation_id,
                is_active=False,
            )
            .order_by(WhatsAppConversationState.updated_at.desc())
            .limit(1)
            .first()
        )
        if paused:
            state_data = paused.state_data if isinstance(paused.state_data, dict) else {}
            if state_data.get("paused"):
                return True

        cutoff = datetime.now(timezone.utc) - timedelta(
            hours=InteractiveAutomationEngine._POST_FLOW_WINDOW_HOURS
        )
        recent_completed = (
            WhatsAppConversationState.query.filter_by(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                is_active=False,
            )
            .filter(WhatsAppConversationState.completed_at.isnot(None))
            .filter(WhatsAppConversationState.completed_at >= cutoff)
            .limit(1)
            .first()
        )
        if recent_completed:
            return True
    except Exception:
        return False
    return False


def sweep_stale_flow_states(limit: int = 200) -> Dict[str, Any]:
    """Authoritative teardown of abandoned interactive-flow states. The 24h timeout is
    otherwise LAZY (only fires on the next inbound), so abandoned flows linger forever and
    partial leads are never surfaced. Run from the scheduler /tick, this sweep tears down any
    non-completed state whose last customer message is older than the window, and LOGS
    abandoned flows still holding collected data so they're recoverable (visible via the
    conversation-state API) — we deliberately do NOT auto-POST partial data to the CRM to
    avoid polluting it with incomplete leads. It also expires stale agent pauses so a
    handed-off conversation returns to the bot."""
    import os
    from datetime import timedelta

    stats = {"scanned": 0, "torn_down": 0, "with_data": 0, "agent_pauses_expired": 0, "errors": 0}
    now = datetime.now(timezone.utc)
    try:
        window_h = float(os.getenv("WHATSAPP_FLOW_STALE_HOURS", "24"))
    except Exception:
        window_h = 24.0
    cutoff = now - timedelta(hours=window_h)

    # 1. Tear down stale (active OR paused) non-completed states.
    try:
        stale = (
            WhatsAppConversationState.query
            .filter(
                WhatsAppConversationState.completed_at.is_(None),
                WhatsAppConversationState.last_user_message_at.isnot(None),
                WhatsAppConversationState.last_user_message_at < cutoff,
            )
            .order_by(WhatsAppConversationState.last_user_message_at.asc())
            .limit(limit)
            .all()
        )
        for st in stale:
            stats["scanned"] += 1
            try:
                _sdc = st.state_data if isinstance(st.state_data, dict) else {}
                if _sdc.get("paused") and _sdc.get("pause_reason") == "human_handoff":
                    continue  # human-owned; handled by agent-pause expiry + resume, not teardown
                collected = (st.state_data or {}).get("collected") or {}
                if collected:
                    stats["with_data"] += 1
                    logger.warning(
                        "[flow_sweep] abandoned flow torn down with %d collected field(s) "
                        "conv=%s automation=%s state=%s (recover via conversation-state API)",
                        len(collected), st.conversation_id, st.automation_id, st.id,
                    )
                sd = dict(st.state_data or {})
                sd["swept_stale"] = True
                st.state_data = sd
                flag_modified(st, "state_data")
                st.is_active = False
                st.completed_at = now
                st.updated_at = now
                db.session.commit()
                stats["torn_down"] += 1
            except Exception as e:
                db.session.rollback()
                stats["errors"] += 1
                logger.warning("[flow_sweep] teardown failed state=%s: %s", getattr(st, "id", "?"), e)
    except Exception as e:
        db.session.rollback()
        logger.warning("[flow_sweep] stale-state query failed: %s", e)

    # 2. Expire stale agent pauses (so a handed-off conversation returns to the bot).
    #    Handed-off conversations carry an ai_chat ContactAutomationOverride (is_enabled=False)
    #    + the ai_paused_by_agent flag — we select via the override rows because attribution_data
    #    is the generic sqlalchemy.JSON type (no .astext / SQL JSON predicate available).
    try:
        from .automation_models import (
            ContactAutomationOverride,
            _agent_pause_expired,
            clear_agent_ai_pause_flag,
            set_contact_override,
        )
        from .models import WhatsAppAccount, WhatsAppConversation

        paused_overrides = (
            ContactAutomationOverride.query
            .filter_by(rule_type="ai_chat", is_enabled=False)
            .limit(limit)
            .all()
        )
        for ov in paused_overrides:
            conv = WhatsAppConversation.query.get(ov.conversation_id)
            if conv is None:
                continue
            attr = conv.attribution_data or {}
            if not attr.get("ai_paused_by_agent") or not _agent_pause_expired(attr):
                continue
            try:
                acct = WhatsAppAccount.query.get(conv.account_id)
                if acct and acct.workspace_id:
                    set_contact_override(str(acct.workspace_id), acct.id, conv.id, "ai_chat", True)
                clear_agent_ai_pause_flag(conv.id)
                stats["agent_pauses_expired"] += 1
            except Exception as e:
                db.session.rollback()
                stats["errors"] += 1
                logger.warning("[flow_sweep] agent-pause expire failed conv=%s: %s", getattr(conv, "id", "?"), e)
    except Exception as e:
        db.session.rollback()
        logger.warning("[flow_sweep] agent-pause sweep skipped: %s", e)

    return stats


def process_interactive_automation(
    account: WhatsAppAccount,
    conversation: WhatsAppConversation,
    message_text: str,
    from_phone: str,
    is_button_reply: bool = False,
    button_payload: Optional[str] = None,
    is_first_inbound: Optional[bool] = None,
    inbound_wamid: Optional[str] = None,
    is_flow_reply: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to process a message against interactive automations.
    Called from the webhook handler / fast_router.
    """
    try:
        from .automation_models import is_automation_disabled_for_contact

        disabled_check_started = time.perf_counter()
        disabled = is_automation_disabled_for_contact(account.workspace_id, conversation.id, "interactive_flows")
        disabled_check_ms = (time.perf_counter() - disabled_check_started) * 1000
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[interactive_engine] disabled_check=%0.fms conversation=%s",
                disabled_check_ms,
                conversation.id,
            )

        if disabled:
            logger.debug(f"Interactive flows disabled for conversation {conversation.id}")
            return None

        engine = InteractiveAutomationEngine(
            account_id=account.id,
            workspace_id=account.workspace_id,
            account=account,
        )

        return engine.process_incoming_message(
            message_text=message_text,
            conversation_id=conversation.id,
            from_phone=from_phone,
            is_button_reply=is_button_reply,
            button_payload=button_payload,
            is_first_inbound=is_first_inbound,
            inbound_wamid=inbound_wamid,
            is_flow_reply=is_flow_reply,
        )

    except Exception as e:
        logger.exception(f"Interactive automation processing error: {e}")
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def reprompt_after_off_script_answer(
    account_id: int,
    conversation_id: int,
    to_phone: str,
) -> Optional[Dict[str, Any]]:
    """After an off-script (FAQ/AI/text) answer is sent, send a ONE-TIME nudge inviting the user
    to resume the paused flow when their query is resolved (they resume by replying a resume
    keyword / satisfaction ack). Keeps the flow PAUSED. Safe no-op when no flow is paused for an
    off-script query. Called from automation_engine.send_automation_response."""
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return None
        engine = InteractiveAutomationEngine(
            account_id=account.id,
            workspace_id=account.workspace_id,
            account=account,
        )
        return engine.offer_resume_after_off_script(conversation_id, to_phone)
    except Exception:
        logger.exception("[interactiveResume] reprompt_after_off_script_answer failed")
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def paused_flow_hint(account_id: int, conversation_id: int) -> Optional[str]:
    """Return a short description of the paused interactive flow (name + current step question)
    for AI flow-awareness, or None if the conversation is not paused mid-flow. Non-fatal."""
    try:
        account = WhatsAppAccount.query.get(account_id)
        if not account:
            return None
        engine = InteractiveAutomationEngine(
            account_id=account.id,
            workspace_id=account.workspace_id,
            account=account,
        )
        return engine.get_paused_flow_hint(conversation_id)
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
        return None
