"""
Intent Parser — Gemini-based NLU engine.
=========================================

Receives user message + conversation history and returns a
structured AgentIntent describing what action to perform.

Falls back to keyword matching when Gemini is unavailable.
"""

import os
import json
import re
import logging
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

# Stop words removed before keyword matching so that
# "create a drip campaign" normalises to "create drip campaign"
_STOP_WORDS = frozenset({
    "a", "an", "the", "my", "me", "i", "to", "for", "of", "in",
    "on", "is", "it", "do", "and", "or", "can", "you", "please",
    "could", "would", "should", "want", "like", "need", "help",
    "with", "about", "some", "this", "that", "just", "right",
    "now", "also", "very", "lets", "let's",
})


def _normalise(text: str) -> str:
    """Lower-case, strip punctuation, remove stop words."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    words = [w for w in text.split() if w not in _STOP_WORDS]
    return " ".join(words)


@dataclass
class AgentIntent:
    """Structured intent extracted from natural language."""
    action: str = ""           # e.g. "create", "list", "send"
    domain: str = ""           # e.g. "template", "drip", "bulk"
    params: Dict[str, Any] = field(default_factory=dict)
    missing_params: List[str] = field(default_factory=list)
    confidence: float = 0.0
    raw_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# Keyword fallback mapping
# ============================================================
# IMPORTANT: keywords here are *also* normalised (no stop words)
# so matching works after both sides go through _normalise().

_KEYWORD_MAP: List[Dict[str, Any]] = [
    # ── Navigation ────────────────────────────────────────────
    {"keywords": ["go inbox", "open inbox", "show inbox", "take inbox",
                  "navigate inbox", "messages", "chats"],
     "domain": "navigation", "action": "go_to", "params": {"target": "inbox"}},
    {"keywords": ["go templates", "open templates", "show templates",
                  "navigate templates", "take templates"],
     "domain": "navigation", "action": "go_to", "params": {"target": "templates"}},
    {"keywords": ["go drip", "open drip", "navigate drip",
                  "take drip", "go drip campaigns"],
     "domain": "navigation", "action": "go_to", "params": {"target": "drip"}},
    {"keywords": ["go contacts", "open contacts", "navigate contacts"],
     "domain": "navigation", "action": "go_to", "params": {"target": "contacts"}},
    {"keywords": ["go analytics", "open analytics", "show analytics",
                  "navigate analytics", "take analytics"],
     "domain": "navigation", "action": "go_to", "params": {"target": "analytics"}},
    {"keywords": ["go settings", "open settings", "navigate settings"],
     "domain": "navigation", "action": "go_to", "params": {"target": "settings"}},
    {"keywords": ["go automation", "open automation", "navigate automation"],
     "domain": "navigation", "action": "go_to", "params": {"target": "automation"}},
    {"keywords": ["go flows", "open flows", "navigate flows"],
     "domain": "navigation", "action": "go_to", "params": {"target": "flows"}},
    {"keywords": ["go bulk", "open bulk", "go datasets", "navigate datasets"],
     "domain": "navigation", "action": "go_to", "params": {"target": "bulk"}},

    # ── Account ───────────────────────────────────────────────
    {"keywords": ["account details", "account info", "show account",
                  "account", "phone number", "whatsapp account"],
     "domain": "account", "action": "get_info", "params": {}},
    {"keywords": ["account status", "connection status", "connected"],
     "domain": "account", "action": "get_status", "params": {}},
    {"keywords": ["list accounts", "all accounts", "connected accounts"],
     "domain": "account", "action": "list_accounts", "params": {}},

    # ── Template ──────────────────────────────────────────────
    {"keywords": ["list templates", "show templates", "approved templates",
                  "templates list", "get templates", "view templates"],
     "domain": "template", "action": "list", "params": {}},
    {"keywords": ["create template", "new template", "make template",
                  "add template", "build template"],
     "domain": "template", "action": "create", "params": {}},
    {"keywords": ["send template"],
     "domain": "template", "action": "send", "params": {}},
    {"keywords": ["sync templates", "refresh templates"],
     "domain": "template", "action": "sync", "params": {}},
    {"keywords": ["delete template", "remove template"],
     "domain": "template", "action": "delete", "params": {}},

    # ── Drip ──────────────────────────────────────────────────
    {"keywords": ["list drip", "show drip", "drip campaigns",
                  "view drip", "get drip"],
     "domain": "drip", "action": "list", "params": {}},
    {"keywords": ["create drip", "create drip campaign", "new drip",
                  "new drip campaign", "new campaign", "create campaign",
                  "start drip", "make drip", "build drip campaign",
                  "setup drip", "set up drip"],
     "domain": "drip", "action": "create", "params": {}},
    {"keywords": ["update drip", "edit drip", "modify campaign",
                  "update campaign", "edit campaign"],
     "domain": "drip", "action": "update", "params": {}},
    {"keywords": ["delete drip", "remove drip", "delete campaign",
                  "remove campaign"],
     "domain": "drip", "action": "delete", "params": {}},
    {"keywords": ["enroll", "add drip", "enroll contacts",
                  "enrol contacts"],
     "domain": "drip", "action": "enroll", "params": {}},

    # ── Bulk ──────────────────────────────────────────────────
    {"keywords": ["list bulk", "show bulk campaigns", "bulk campaigns",
                  "view bulk"],
     "domain": "bulk", "action": "list", "params": {}},
    {"keywords": ["create bulk", "create bulk campaign", "new bulk",
                  "new bulk campaign", "bulk message", "send bulk message"],
     "domain": "bulk", "action": "create", "params": {}},
    {"keywords": ["send bulk", "start bulk", "launch bulk"],
     "domain": "bulk", "action": "send", "params": {}},
    {"keywords": ["schedule bulk", "schedule campaign"],
     "domain": "bulk", "action": "schedule", "params": {}},

    # ── Automation ────────────────────────────────────────────
    {"keywords": ["list automation", "show automation rules",
                  "automation rules", "view automation"],
     "domain": "automation", "action": "list", "params": {}},
    {"keywords": ["welcome message", "setup welcome", "create welcome",
                  "set up welcome"],
     "domain": "automation", "action": "create_welcome", "params": {}},
    {"keywords": ["away message", "setup away", "create away",
                  "out office", "set up away"],
     "domain": "automation", "action": "create_away", "params": {}},
    {"keywords": ["keyword trigger", "keyword auto", "create keyword",
                  "keyword response"],
     "domain": "automation", "action": "create_keyword", "params": {}},
    {"keywords": ["toggle automation", "enable automation",
                  "disable automation", "turn on automation",
                  "turn off automation"],
     "domain": "automation", "action": "toggle", "params": {}},

    # ── Messaging ─────────────────────────────────────────────
    {"keywords": ["send text", "send message", "message"],
     "domain": "messaging", "action": "send_text", "params": {}},
    {"keywords": ["send template"],
     "domain": "messaging", "action": "send_template", "params": {}},

    # ── Analytics ─────────────────────────────────────────────
    {"keywords": ["analytics summary", "show analytics",
                  "dashboard analytics", "stats", "analytics",
                  "view analytics"],
     "domain": "analytics", "action": "summary", "params": {}},
    {"keywords": ["analytics trends", "message trends", "trends"],
     "domain": "analytics", "action": "trends", "params": {}},
    {"keywords": ["export analytics", "export data",
                  "download analytics"],
     "domain": "analytics", "action": "export", "params": {}},
]


def _keyword_fallback(text: str) -> AgentIntent:
    """
    Keyword match after stop-word normalisation.

    Both the input and every keyword go through _normalise()
    so "create a drip campaign" matches keyword "create drip campaign".
    """
    norm = _normalise(text)
    logger.debug("Keyword fallback — normalised: '%s'", norm)

    best_match = None
    best_score = 0

    for entry in _KEYWORD_MAP:
        for kw in entry["keywords"]:
            norm_kw = _normalise(kw)
            if norm_kw in norm:
                score = len(norm_kw)
                if score > best_score:
                    best_score = score
                    best_match = entry
                    logger.debug("  ✓ matched kw='%s' (norm='%s') score=%d", kw, norm_kw, score)

    if best_match:
        logger.info(
            "Keyword fallback matched: domain=%s action=%s (score=%d)",
            best_match["domain"], best_match["action"], best_score,
        )
        return AgentIntent(
            action=best_match["action"],
            domain=best_match["domain"],
            params=dict(best_match.get("params", {})),
            confidence=0.7,
            raw_text=text,
        )

    logger.info("Keyword fallback: no match for '%s'", text)
    return AgentIntent(raw_text=text, confidence=0.0)


# ============================================================
# Gemini-based parser
# ============================================================

class IntentParser:
    """
    Uses Gemini to extract structured intent from natural language.
    Falls back to keyword matching if Gemini is unavailable.
    """

    def __init__(self, workspace_id: Optional[str] = None):
        self._workspace_id = workspace_id
        self._model = None
        self._client = None        # google.genai.Client (newer SDK)
        self._use_new_sdk = False
        self._init_gemini(workspace_id)

    def _init_gemini(self, workspace_id: Optional[str] = None):
        # Per-tenant Gemini key (tenant brings their own AI billing). The resolver
        # falls back to the global GEMINI_API_KEY env, so for T0000 / unset /
        # no-workspace this is byte-identical to reading the env var directly.
        from tenant.integration import get_tenant_ai_config
        cfg = get_tenant_ai_config(workspace_id=workspace_id)
        api_key = cfg.gemini_api_key
        if not api_key:
            logger.warning("IntentParser: No GEMINI_API_KEY found — using keyword fallback")
            return

        # Try older google-generativeai SDK first (matches template_rewriter.py)
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel("gemini-3.1-flash-lite")
            logger.info("IntentParser: Gemini model initialised (google-generativeai)")
            return
        except Exception as exc:
            logger.debug("IntentParser: google.generativeai failed (%s), trying google.genai", exc)

        # Fallback: newer google-genai SDK
        try:
            from google import genai as genai_new
            self._client = genai_new.Client(api_key=api_key)
            self._use_new_sdk = True
            logger.info("IntentParser: Gemini model initialised (google.genai Client)")
        except Exception as exc:
            logger.warning("IntentParser: both Gemini SDKs failed (%s) — using keyword fallback", exc)

    def _build_system_prompt(self, action_schema: list) -> str:
        schema_json = json.dumps(action_schema, indent=2)
        return f"""You are SocioChat's intent parser. Given a user message, extract the structured intent.

AVAILABLE ACTIONS:
{schema_json}

RULES:
1. Return ONLY valid JSON — no markdown, no explanation.
2. Pick the best matching (domain, action) from the schema above.
3. Extract any parameter values mentioned in the message into "params".
4. List any required params that are NOT mentioned in "missing_params".
5. Set "confidence" between 0.0 and 1.0.
6. If nothing matches, return domain="" action="" confidence=0.

OUTPUT FORMAT (strict JSON):
{{
  "domain": "<domain>",
  "action": "<action>",
  "params": {{}},
  "missing_params": [],
  "confidence": 0.0
}}
"""

    def parse(
        self,
        message: str,
        action_schema: list,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        workspace_id: Optional[str] = None,
    ) -> AgentIntent:
        """
        Parse a user message into a structured AgentIntent.

        Args:
            message: The user's natural language input.
            action_schema: Output of action_registry.get_schema_for_intent_parser().
            conversation_history: Recent messages for multi-turn context.
            workspace_id: When supplied (and different from this parser's own
                workspace), the tenant's own Gemini key is used for this call so
                the tenant is billed on their AI quota. Omitting it preserves the
                existing global-env behavior exactly.

        Returns:
            AgentIntent dataclass.
        """
        # If a caller threads in a workspace_id that differs from how this parser
        # was built, build a per-workspace parser so the tenant's own Gemini key
        # is used. The resolver falls back to the global env key, so a no-override
        # tenant (or T0000) yields exactly the same client as before.
        if workspace_id is not None and workspace_id != self._workspace_id:
            try:
                return IntentParser(workspace_id=workspace_id).parse(
                    message,
                    action_schema=action_schema,
                    conversation_history=conversation_history,
                )
            except Exception as exc:
                logger.warning(
                    "IntentParser: per-workspace parser failed (%s) — using default parser",
                    exc,
                )

        if not self._model and not self._client:
            logger.info("IntentParser.parse: no Gemini model available, using keyword fallback")
            return _keyword_fallback(message)

        try:
            system_prompt = self._build_system_prompt(action_schema)
            logger.debug("IntentParser.parse: calling Gemini with %d actions in schema", len(action_schema))

            # Build conversation context
            context_lines = []
            if conversation_history:
                for msg in conversation_history[-6:]:  # last 3 pairs
                    role = "User" if msg["role"] == "user" else "Agent"
                    context_lines.append(f"{role}: {msg['text']}")

            user_prompt = ""
            if context_lines:
                user_prompt += "Recent conversation:\n" + "\n".join(context_lines) + "\n\n"
            user_prompt += f"Current user message: {message}\n\nExtract the intent as JSON:"

            raw = ""
            if self._use_new_sdk and self._client:
                # Newer google-genai SDK
                response = self._client.models.generate_content(
                    model="gemini-3.1-flash-lite",
                    contents=system_prompt + "\n\n" + user_prompt,
                )
                raw = response.text.strip()
            else:
                # Older google-generativeai SDK
                response = self._model.generate_content(
                    [system_prompt, user_prompt],
                    generation_config={
                        "temperature": 0.1,
                        "max_output_tokens": 512,
                    },
                )
                raw = response.text.strip()

            # ── AI-usage metering (fail-soft, single post-merge point so the
            #    new-SDK and legacy-SDK branches are never double-counted) ──
            try:
                from subscription.service import record_ai_usage, resolve_workspace_owner
                _meter_model = "gemini-3.1-flash-lite"
                _meter_owner, _meter_ws = (None, None)
                if self._workspace_id:
                    _meter_owner, _meter_ws = resolve_workspace_owner(self._workspace_id)
                _in_tok = _out_tok = 0
                _um = getattr(response, "usage_metadata", None)
                if _um is not None:
                    _in_tok = getattr(_um, "prompt_token_count", 0) or 0
                    _out_tok = getattr(_um, "candidates_token_count", 0) or 0
                record_ai_usage(
                    _meter_owner,
                    _meter_ws,
                    "agent_intent",
                    _meter_model,
                    input_tokens=_in_tok,
                    output_tokens=_out_tok,
                    route_path="agent_backend/intent_parser.py:IntentParser.parse",
                    _commit=True,
                )
            except Exception:
                pass

            logger.debug("IntentParser.parse: Gemini raw response: %s", raw[:300])

            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = re.sub(r"^```(?:json)?\s*", "", raw)
                raw = re.sub(r"\s*```$", "", raw)

            parsed = json.loads(raw)

            intent = AgentIntent(
                domain=parsed.get("domain", ""),
                action=parsed.get("action", ""),
                params=parsed.get("params", {}),
                missing_params=parsed.get("missing_params", []),
                confidence=float(parsed.get("confidence", 0.0)),
                raw_text=message,
            )
            logger.info(
                "IntentParser.parse: Gemini result domain=%s action=%s confidence=%.2f",
                intent.domain, intent.action, intent.confidence,
            )
            return intent

        except Exception as exc:
            logger.warning("Gemini parse failed (%s) — falling back to keywords", exc)
            return _keyword_fallback(message)


# Module-level singleton
intent_parser = IntentParser()
