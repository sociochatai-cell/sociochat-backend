"""
WhatsApp AI Chatbot
===================

AI-powered conversational responses for WhatsApp automation.
Production-grade implementation with RAG integration.

Key Design Principles:
- FAIL-SAFE: AI errors never break message flow
- WORKSPACE-ISOLATED: RAG context scoped per workspace
- CONCISE: Responses optimized for WhatsApp (short, no markdown)
- MULTI-LANGUAGE: Detects and responds in user's language

Uses Google Generative AI for:
- Intent Classification
- Response Generation (with RAG context)
"""

import os
import re
import json
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from pathlib import Path

# New GenAI SDK
from google import genai
from google.genai.types import HttpOptions, GenerateContentConfig, Tool, FunctionDeclaration, Part, Content

logger = logging.getLogger(__name__)


def _ai_debug_enabled() -> bool:
    return os.getenv("WHATSAPP_AI_DEBUG", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

# ============================================================
# Configuration
# ============================================================

# Model for chat responses (GA June 2026)
DEFAULT_MODEL = (
    os.environ.get("TEXT_MODEL")
    or os.environ.get("GEMINI_MODEL")
    or "gemini-3.5-flash"
)

# Legacy model names — map to current GA models
_MODEL_ALIASES = {
    "gemini-1.5-flash": "gemini-3.1-flash-lite",
    "gemini-1.5-pro": "gemini-3.1-pro-preview",
    "gemini-pro": "gemini-3.5-flash",
    "gemini-2.0-flash": "gemini-3.5-flash",
    "gemini-2.0-flash-001": "gemini-3.5-flash",
    "gemini-2.5-flash": "gemini-3.5-flash",
    "gemini-3-flash-preview": "gemini-3.5-flash",
}


def _resolve_generation_model(candidate: Optional[str]) -> str:
    model = str(candidate or "").strip()
    if not model:
        return DEFAULT_MODEL
    return _MODEL_ALIASES.get(model, model)

# RAG configuration
RAG_INDEX_BASE_DIR = Path(os.environ.get("KNOWLEDGE_INDEX_DIR", "faiss_indexes"))
RAG_CONFIDENCE_THRESHOLD = 0.5
RAG_TOP_K = 5

# Pure greetings/acks — safe to skip RAG. Project names (Puraniks, Mahindra) must NOT be skipped.
_RAG_SKIP_CHITCHAT = frozenset({
    "hi", "hello", "hey", "hola", "namaste", "ok", "okay", "yes", "no", "yeah", "yep", "nope",
    "thanks", "thank", "thankyou", "ty", "thx", "bye", "goodbye", "good", "fine", "cool", "hii",
})


def _should_skip_rag_for_message(message: str) -> bool:
    """Skip RAG only for pure greetings/acks — never for one-word entity/project lookups."""
    text = (message or "").strip()
    if not text:
        return True
    tokens = [t.strip(".,!?").lower() for t in text.split() if t.strip(".,!?")]
    if not tokens:
        return True
    if len(tokens) <= 2 and all(t in _RAG_SKIP_CHITCHAT for t in tokens):
        return True
    return False


def _effective_rag_threshold(message: str, configured: float) -> float:
    """Short keyword queries (project names, locations) need a lower retrieval bar."""
    tokens = [t for t in (message or "").split() if t.strip()]
    if len(tokens) <= 3 and len((message or "").strip()) < 64:
        short_floor = float(os.getenv("WHATSAPP_RAG_SHORT_QUERY_THRESHOLD", "0.20"))
        return min(configured, short_floor)
    return configured

# Safety settings not needed for new SDK in same format
# We'll configure them in the call if needed

# Default system prompt - optimized for WhatsApp business conversations
# Allow full WhatsApp bubbles (~4096 chars); model output cap is max_output_tokens
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_FALLBACK_MESSAGE = "I'm sorry, I couldn't process your request. A team member will assist you soon."

KB_REFUSAL_MESSAGE = (
    "I can only answer questions about our business using our knowledge base. "
    "I don't have information about that. Please ask about our products, services, or policies."
)


def _rag_min_answer_score() -> float:
    try:
        return max(0.0, min(float(os.getenv("RAG_MIN_ANSWER_SCORE", "0.20")), 1.0))
    except (TypeError, ValueError):
        return 0.20

# Short, anaphoric follow-ups ("why?", "what do you mean?", "and?") don't independently match
# the knowledge base, but ARE answerable from the conversation so far. We detect them so the AI
# answers from context instead of hard-refusing with the KB message.
_FOLLOWUP_EXACT = {
    "why", "y", "how", "how so", "how come", "what", "wat", "and", "so", "but why",
    "really", "ok", "okay", "k", "hmm", "huh", "meaning", "more", "go on", "continue",
    "explain", "elaborate", "clarify", "tell me more", "what do you mean",
    "what does that mean", "why so", "why not", "what for", "what did you verify",
    "and then", "then what", "such as", "like what", "for example", "why is that",
}
# Multi-word leaders kept deliberately tight: "what"/"how"/"which" are EXCLUDED here (they form
# standalone questions like "what is bitcoin" / "how does X work" that should go through normal
# RAG, not the follow-up bypass). Their bare single-word forms ("What?", "How?") live in
# _FOLLOWUP_EXACT. The leaders below are almost always anaphoric ("why this plan", "explain that").
_FOLLOWUP_LEADERS = {
    "why", "explain", "elaborate", "clarify", "and", "so", "tell", "meaning",
}


def _normalize_followup(message: str) -> str:
    return (message or "").strip().lower().rstrip("?.!… ").strip()


def _is_contextual_followup(message: str) -> bool:
    """True for short anaphoric follow-ups that only make sense against prior turns."""
    t = _normalize_followup(message)
    if not t:
        return False
    if t in _FOLLOWUP_EXACT:
        return True
    words = t.split()
    # Short question/elaboration with no standalone subject — e.g. "why this plan", "what for".
    if 0 < len(words) <= 4 and words[0] in _FOLLOWUP_LEADERS:
        return True
    return False


def _last_user_text(context: Optional[List[Dict[str, str]]]) -> str:
    for item in reversed(context or []):
        if isinstance(item, dict) and item.get("role") == "user":
            txt = (item.get("text") or "").strip()
            if txt:
                return txt
    return ""


DEFAULT_SYSTEM_PROMPT = """You are a helpful WhatsApp assistant for a business.

COMMUNICATION STYLE:
- Be polite, professional, and clear
- Aim for 2–6 sentences when the question needs detail; stay under ~3500 characters (WhatsApp limit is 4096)
- Use simple language, avoid jargon
- Be friendly but professional
- If you don't know something, admit it honestly

FORMATTING RULES (CRITICAL):
- NO markdown: no **, *, _, `, ##, - bullets
- Plain text only
- No emojis unless the customer uses them first
- Use commas for lists, not bullet points

LANGUAGE MATCHING (CRITICAL):
- Always match the customer's language
- If they write in Hindi → reply in Hindi
- If they write in Hinglish → reply in Hinglish  
- If they write in Telugu → reply in Telugu
- If they write in Tinglish → reply in Tinglish
- If they write in English → reply in English

BEHAVIOR:
- For complex issues, suggest speaking with a human agent
- Never share sensitive information like passwords or payment details
- If asked about something not in your knowledge, say you'll check and get back"""


# ============================================================
# GenAI Configuration
# ============================================================

_genai_client = None
_genai_client_mode: Optional[str] = None
# Per-tenant API-key clients, cached by tenant Gemini key so a tenant using their
# own AI billing reuses one client instead of rebuilding it on every call.
_genai_clients_by_key: Dict[str, Any] = {}


def _gemini_api_key() -> str:
    return (
        os.environ.get("GOOGLE_GENAI_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    ).strip()


def _use_vertex_ai() -> bool:
    return os.environ.get("GEMINI_USE_VERTEX", "").lower() in ("1", "true", "yes")


def _get_tenant_genai_client(workspace_id):
    """Return a GenAI client built from the tenant's own Gemini key, or None.

    Falls back to the shared global client when the tenant has no AI override
    (so T0000 / unconfigured tenants are byte-identical to before).
    """
    try:
        from tenant.integration import get_tenant_ai_config
        cfg = get_tenant_ai_config(workspace_id=workspace_id)
    except Exception as e:
        logger.warning(f"Tenant AI config resolution failed ({e}); using global client")
        return None

    # No tenant override → use the existing shared/global client path unchanged.
    if not cfg.is_custom or not cfg.gemini_api_key:
        return None

    api_key = cfg.gemini_api_key
    cached = _genai_clients_by_key.get(api_key)
    if cached:
        return cached
    try:
        logger.info("Initializing GenAI Client (tenant API key mode)")
        client = genai.Client(api_key=api_key)
        _genai_clients_by_key[api_key] = client
        return client
    except Exception as e:
        logger.error(f"Tenant GenAI Client init failed (API key mode): {e}")
        return None


def get_genai_runtime_status() -> Dict[str, Any]:
    """Lightweight runtime check used by fast_router (placeholder gating)."""
    if _gemini_api_key() and not _use_vertex_ai():
        return {"available": True, "mode": "api_key"}
    client = get_genai_client()
    return {
        "available": client is not None,
        "mode": _genai_client_mode or ("vertex" if client else "unconfigured"),
    }


def get_genai_client(workspace_id=None):
    """Get or initialize the GenAI client (API key or Vertex — same logic as rag_engine).

    When ``workspace_id`` is supplied and that tenant has configured their own
    Gemini key, a per-tenant client is returned so the tenant is billed on their
    own AI quota. With no workspace (or no tenant override) the shared global
    client is used — byte-identical to before.
    """
    global _genai_client, _genai_client_mode

    if workspace_id:
        tenant_client = _get_tenant_genai_client(workspace_id)
        if tenant_client is not None:
            return tenant_client

    if _genai_client:
        return _genai_client

    api_key = _gemini_api_key()
    if api_key and not _use_vertex_ai():
        try:
            _genai_client = genai.Client(api_key=api_key)
            _genai_client_mode = "api_key"
            logger.info("GenAI Client initialized (API key mode)")
            return _genai_client
        except Exception as e:
            logger.error(f"GenAI API key client init failed: {e}")

    project = (
        os.environ.get("GCP_PROJECT")
        or os.environ.get("PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or "angular-sorter-473216-k8"
    )
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"

    try:
        logger.info(f"Initializing Vertex AI Client: project={project}, location={location}")
        _genai_client = genai.Client(
            http_options=HttpOptions(api_version="v1"),
            project=project,
            location=location,
            vertexai=True,
        )
        _genai_client_mode = "vertex"
        logger.info("GenAI Client initialized (Vertex mode)")
        return _genai_client
    except Exception as e:
        logger.error(f"GenAI Client init failed: {e}")
        return None


# ============================================================
# Data Classes
# ============================================================

@dataclass
class ChatResponse:
    """Result of AI chat response generation."""
    message: str
    success: bool = True
    error: Optional[str] = None
    tokens_used: int = 0
    model_used: str = DEFAULT_MODEL
    response_time_ms: int = 0
    used_rag: bool = False
    rag_chunks: int = 0
    low_rag_confidence: bool = False
    rag_max_score: float = 0.0
    escalate_to_human: bool = False
    escalation_reason: str = ""
    # PHASE 3: interactive Meta Cloud API payloads (buttons / product_list / product)
    # the agent decided to send AFTER the text reply. Each item is a raw Meta
    # `interactive` object to hand to service.send_interactive_passthrough(). Empty
    # for the legacy path and whenever the agent sends no visual content.
    interactive_messages: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message": self.message,
            "success": self.success,
            "error": self.error,
            "tokens_used": self.tokens_used,
            "model_used": self.model_used,
            "response_time_ms": self.response_time_ms,
            "used_rag": self.used_rag,
            "rag_chunks": self.rag_chunks,
            "low_rag_confidence": self.low_rag_confidence,
            "rag_max_score": self.rag_max_score,
            "escalate_to_human": self.escalate_to_human,
            "escalation_reason": self.escalation_reason,
            "interactive_messages": self.interactive_messages,
        }


@dataclass
class AIConfig:
    """AI configuration for an account."""
    enabled: bool = False
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = 0.7
    fallback_message: str = DEFAULT_FALLBACK_MESSAGE
    context_messages: int = 5
    # RAG Configuration
    use_rag: bool = True
    rag_top_k: int = 5
    rag_confidence_threshold: float = 0.5
    workspace_id: Optional[str] = None
    knowledge_base_id: Optional[str] = None
    # Optional one-line description of a paused interactive flow, so the model can answer
    # the off-script question in context and not derail the flow.
    flow_context: Optional[str] = None
    # PHASE 4: live conversation context, populated by the caller (automation_engine)
    # so agent-mode transactional tools (request_payment, escalate_to_human,
    # capture_lead, book_appointment) can act on the real chat. None in the legacy path.
    conversation_id: Optional[int] = None
    customer_phone: Optional[str] = None


def get_bot_config(account: Any) -> Dict[str, Any]:
    """
    Production config loader: prefer decoupled whatsapp_bot_settings, then legacy account columns.
    ai_enabled is tri-state: None = no account-level gate (defer to automation rule), True/False = explicit.
    """
    from .models import WhatsAppBotSettings

    settings = getattr(account, "bot_settings", None)
    if settings is None and getattr(account, "id", None):
        try:
            settings = WhatsAppBotSettings.query.filter_by(account_id=account.id).first()
        except Exception:
            settings = None

    def pick(col: str) -> Any:
        if settings is not None:
            v = getattr(settings, col, None)
            if v is not None:
                return v
        return getattr(account, col, None)

    if settings is not None:
        gate = settings.ai_enabled
    elif getattr(account, "ai_enabled", None) is not None:
        gate = account.ai_enabled
    else:
        gate = None

    return {
        "ai_enabled": gate,
        "ai_model": pick("ai_model"),
        "temperature": pick("temperature"),
        "max_tokens": pick("max_tokens"),
        "prompt": pick("prompt_override"),
        "kb_id": pick("knowledge_base_id"),
    }


def prepare_automation_ai(
    account: Any,
    response_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Merge automation rule JSON with bot_settings / legacy account AI fields.

    Returns:
        { "run": bool, "fallback_message": str, "config": dict | None }
        When run is False, caller should send fallback_message as text (slice-level AI disabled).
    """
    rc = response_config or {}
    inbound_wamid = rc.get("inbound_wamid")
    fallback = normalize_fallback_message(
        rc.get("fallback_message", DEFAULT_FALLBACK_MESSAGE)
    )
    account_id = getattr(account, "id", None)
    workspace_id = getattr(account, "workspace_id", None)

    try:
        from .trace_debug import trace_event
    except Exception:
        trace_event = None

    if account_id:
        from .capabilities import ai_capability_check

        ent = ai_capability_check(account_id)
        if not ent.ok:
            if trace_event:
                trace_event(
                    stage="ai.prepare",
                    status="blocked",
                    wamid=inbound_wamid,
                    account_id=account_id,
                    details={
                        "reason": "capability_check_failed",
                        "message": ent.message,
                        "workspace_id": workspace_id,
                    },
                )
            return {
                "run": False,
                "fallback_message": normalize_fallback_message(ent.message or fallback),
                "config": None,
                "reason": "capability_check_failed",
            }

    bot = get_bot_config(account)

    if bot.get("ai_enabled") is False:
        if trace_event:
            trace_event(
                stage="ai.prepare",
                status="blocked",
                wamid=inbound_wamid,
                account_id=account_id,
                details={"reason": "ai_disabled", "workspace_id": workspace_id},
            )
        return {
            "run": False,
            "fallback_message": fallback,
            "config": None,
            "reason": "ai_disabled",
        }

    try:
        _cfg_max_tokens = int(rc.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS))
    except (TypeError, ValueError):
        _cfg_max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    if _cfg_max_tokens < 768:
        _cfg_max_tokens = DEFAULT_MAX_OUTPUT_TOKENS
    if bot.get("max_tokens") is not None:
        try:
            _cfg_max_tokens = int(bot["max_tokens"])
        except (TypeError, ValueError):
            pass
    if _cfg_max_tokens < 768:
        _cfg_max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

    temp = rc.get("temperature", 0.7)
    try:
        temp = float(temp)
    except (TypeError, ValueError):
        temp = 0.7
    if bot.get("temperature") is not None:
        try:
            temp = float(bot["temperature"])
        except (TypeError, ValueError):
            pass

    system_prompt = (rc.get("system_prompt") or "").strip()
    if bot.get("prompt"):
        system_prompt = (bot["prompt"] or "").strip()
    if not system_prompt:
        system_prompt = DEFAULT_SYSTEM_PROMPT

    model = _resolve_generation_model(
        rc.get("model") or bot.get("ai_model") or DEFAULT_MODEL
    )

    cfg = {
        "enabled": True,
        "system_prompt": system_prompt,
        "fallback_message": normalize_fallback_message(fallback),
        "max_tokens": _cfg_max_tokens,
        "temperature": temp,
        "model": model,
        # Past-conversation memory window (last N messages). Defaults to 20.
        "context_messages": rc.get("context_messages", 20),
        "use_rag": rc.get("use_rag", True),
        "rag_top_k": rc.get("rag_top_k", 5),
        "rag_confidence_threshold": rc.get("rag_confidence_threshold", 0.5),
        "workspace_id": getattr(account, "workspace_id", None),
        "knowledge_base_id": bot.get("kb_id"),
        # paused-flow awareness; populated by caller once conversation_id is known
        "flow_context": None,
    }
    if getattr(account, "id", None):
        try:
            from shared_models import db as _db
            from .warmup_enforcement import ai_throttle_params

            cap_tokens, cap_temp = ai_throttle_params(int(account.id), _db.session)
            cfg["max_tokens"] = min(int(cfg["max_tokens"]), int(cap_tokens))
            cfg["temperature"] = min(float(cfg["temperature"]), float(cap_temp))
        except Exception:
            pass
    if trace_event:
        trace_event(
            stage="ai.prepare",
            status="ok",
            wamid=inbound_wamid,
            account_id=account_id,
            details={
                "reason": "ready",
                "model": model,
                "use_rag": bool(cfg.get("use_rag")),
                "knowledge_base_id": cfg.get("knowledge_base_id"),
                "workspace_id": workspace_id,
            },
        )
    return {
        "run": True,
        "fallback_message": normalize_fallback_message(fallback),
        "config": cfg,
        "reason": "ready",
    }


def normalize_fallback_message(value: Optional[str]) -> str:
    """Auto-fix clearly broken typo variants in fallback text."""
    text = str(value or "").strip()
    if not text:
        return DEFAULT_FALLBACK_MESSAGE

    lower = text.lower()
    broken_markers = ("condm", "prcess", "couldnt pr", "couldn't pr")
    if any(marker in lower for marker in broken_markers):
        return DEFAULT_FALLBACK_MESSAGE
    return text


@dataclass
class IntentResult:
    """Result of intent classification."""
    intent: str
    confidence: float = 1.0
    success: bool = True
    error: Optional[str] = None
    response_time_ms: int = 0
    entities: Dict[str, Any] = field(default_factory=dict)
    language: str = "en"
    missing_params: List[str] = field(default_factory=list)
    clarification_needed: bool = False
    friendly_message: str = ""
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "confidence": self.confidence,
            "success": self.success,
            "error": self.error,
            "response_time_ms": self.response_time_ms,
            "entities": self.entities,
            "language": self.language,
            "missing_params": self.missing_params,
            "clarification_needed": self.clarification_needed,
            "friendly_message": self.friendly_message,
        }


# ============================================================
# Intent Classification
# ============================================================

INTENT_CLASSIFICATION_PROMPT = """Analyze the message and output a JSON object:
{{
    "intent": "<category>",
    "entities": {{"key": "value"}},
    "language": "<detected language code>",
    "friendly_message": "<brief acknowledgment>"
}}

INTENT CATEGORIES:
- greeting: Hello, hi, good morning
- support: Technical help, issues, problems
- sales: Pricing, buy, purchase
- info: General questions, features
- complaint: Unhappy, refund request
- appointment: Schedule, book, demo
- order_status: Where is my order, tracking
- payment: Payment issues, invoice
- faq: Common questions
- other: Unclear

MESSAGE: "{message}"

JSON:"""

INTENT_TYPES = ["greeting", "support", "sales", "info", "complaint", "appointment", "order_status", "payment", "faq", "other"]

# Short greeting-only messages (fast_router instant reply, no Gemini call)
_GREETING_ONLY_PHRASES = frozenset({
    "hi",
    "hello",
    "hey",
    "hola",
    "namaste",
    "hey there",
    "hi there",
    "good morning",
    "good afternoon",
    "good evening",
    "good night",
    "howdy",
    "sup",
    "yo",
})


def _build_fast_greeting_reply(message: str) -> Optional[str]:
    """
    Return a canned reply for very short greeting-only messages.

    Returns None when the message needs full AI (questions, requests, etc.).
    Used by fast_router to skip Gemini for trivial hellos.
    """
    text = (message or "").strip()
    if not text or len(text) > 48:
        return None

    if "?" in text:
        return None

    normalized = re.sub(r"\s+", " ", text.lower()).strip(" !?.,")
    if not normalized:
        return None

    if normalized in _GREETING_ONLY_PHRASES:
        return "Hello! How can I help you today?"

    words = normalized.split()
    if len(words) > 3:
        return None

    if words and words[0] in {"hi", "hello", "hey", "namaste"} and len(words) <= 2:
        return "Hello! How can I help you today?"

    return None


def classify_intent(
    message: str,
    model_name: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> IntentResult:
    """Classify the intent of a customer message. FAIL-SAFE.

    Pass ``workspace_id`` to bill the tenant's own Gemini key; omitting it uses
    the shared global client (byte-identical to before).
    """
    import time
    start_time = time.time()

    try:
        client = get_genai_client(workspace_id=workspace_id)
        if not client:
            return IntentResult(intent="other", success=False, error="API not configured")
        
        prompt = INTENT_CLASSIFICATION_PROMPT.format(message=message[:300])
        
        from core.genai_bridge import generate_text
        response = generate_text(
            model=model_name or DEFAULT_MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=256,
                temperature=0.1
            ),
            gemini_client=client,
            workspace_id=workspace_id,
            feature="ai_intent",
        )

        elapsed_ms = int((time.time() - start_time) * 1000)

        if not response.text:
            return IntentResult(intent="other", success=False, error="Empty response", response_time_ms=elapsed_ms)

        # Meter AI usage (fail-soft; must never break classification)
        try:
            from subscription.service import record_ai_usage, resolve_workspace_owner
            _uid, _wid = resolve_workspace_owner(workspace_id)
            record_ai_usage(_uid, _wid, "ai_intent", model_name or DEFAULT_MODEL)
        except Exception:
            pass

        text = response.text.strip()
        if "```" in text:
            text = re.sub(r'```json\s*|\s*```', '', text)
        
        try:
            result = json.loads(text)
        except:
            match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if match:
                result = json.loads(match.group())
            else:
                return IntentResult(intent="other", confidence=0.5, response_time_ms=elapsed_ms)
        
        intent = result.get("intent", "other").lower()
        if intent not in INTENT_TYPES:
            intent = "other"
        
        return IntentResult(
            intent=intent,
            confidence=result.get("confidence", 0.9),
            entities=result.get("entities", {}),
            language=result.get("language", "en"),
            friendly_message=result.get("friendly_message", ""),
            success=True,
            response_time_ms=elapsed_ms
        )
        
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        logger.exception(f"Intent classification failed: {e}")
        return IntentResult(intent="other", success=False, error=str(e), response_time_ms=elapsed_ms)


# ============================================================
# RAG Integration
# ============================================================

def get_rag_module():
    """Import RAG module safely."""
    try:
        import sys
        parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        import rag
        return rag
    except ImportError as e:
        logger.warning(f"RAG module not available: {e}")
        return None


def get_rag_context(query: str, workspace_id: int, top_k: int = 5, threshold: float = 0.25) -> Tuple[List[Dict], bool]:
    """
    Retrieve relevant context from knowledge base using Qdrant Cloud.
    Uses workspace_id for multi-tenant isolation.

    FAIL-SAFE: Returns empty on any error.
    """
    if not workspace_id:
        return [], False

    rag = get_rag_module()
    if not rag:
        return [], False

    try:
        results, stats = rag.retrieve_with_context_window(
            query=query,
            workspace_id=int(workspace_id),
            top_k=top_k,
            score_threshold=threshold,
        )

        if not results:
            # Second-pass retrieval: chunk exists but score may be slightly under cutoff
            # for paraphrased user queries.
            try:
                relaxed_threshold = float(os.getenv("WHATSAPP_RAG_FALLBACK_THRESHOLD", "0.05"))
            except (TypeError, ValueError):
                relaxed_threshold = 0.05
            relaxed_threshold = max(0.0, min(relaxed_threshold, 1.0))
            if relaxed_threshold < threshold:
                retry_results, retry_stats = rag.retrieve_with_context_window(
                    query=query,
                    workspace_id=int(workspace_id),
                    top_k=top_k,
                    score_threshold=relaxed_threshold,
                )
                if retry_results:
                    results = retry_results
                    stats = {
                        **(stats or {}),
                        **{f"retry_{k}": v for k, v in (retry_stats or {}).items()},
                        "fallback_threshold": relaxed_threshold,
                        "fallback_used": True,
                    }

        if not results:
            return [], False

        logger.info(
            "RAG retrieved %d chunks for workspace_id=%s, timing=%s",
            len(results),
            workspace_id,
            stats,
        )

        top_score = results[0].get("score", 0)
        return results, top_score >= threshold

    except Exception as e:
        logger.warning(f"RAG retrieval failed: {e}")
        return [], False


def build_rag_enhanced_message(user_message: str, rag_chunks: List[Dict]) -> str:
    """
    Build message with RAG context and multilingual script-matching rules.
    Includes strict no-hallucination instructions.
    """
    if not rag_chunks:
        return user_message
    
    context_parts = []
    try:
        min_context_score = float(os.getenv("WHATSAPP_RAG_CONTEXT_MIN_SCORE", "0.08"))
    except (TypeError, ValueError):
        min_context_score = 0.08
    min_context_score = max(0.0, min(min_context_score, 1.0))

    for i, c in enumerate(rag_chunks):
        text = c.get('text', '')
        source = c.get('source', 'Unknown')
        score = c.get('score', 0)
        if text:
            # Include chunks above a modest relevance floor.
            if score >= min_context_score:
                context_parts.append(f"[{source}]:\n{text}")

    if not context_parts:
        return user_message
    
    # Limit context to avoid overwhelming the model
    context_str = "\n---\n".join(context_parts[:5])  # Max 5 chunks
    
    return f"""### KNOWLEDGE BASE (Use ONLY this information):
{context_str}

### CUSTOMER QUESTION: {user_message}

### STRICT RULES:
1. Answer STRICTLY from the knowledge above. Do NOT use general knowledge, coding tutorials, or outside facts.
2. If the knowledge does not answer the question, reply exactly:
"{KB_REFUSAL_MESSAGE}"
3. Maximum about 120 words unless the question needs a bit more for clarity
4. NO emojis, NO markdown formatting
5. Plain text only
6. CRITICAL - Match user's language and script:
   - If user writes in English → Reply in English
   - If user writes in Hindi script (देवनागरी) → Reply in Hindi
   - If user writes in Hinglish (kya hai, aap, kitna) → Reply in Hinglish
   - If user writes in Telugu script (తెలుగు) → Reply in Telugu  
   - If user writes in Tinglish (emi, ela, meeru) → Reply in Tinglish
7. Be friendly and professional

RESPONSE:"""


# ============================================================
# AI Chatbot Class
# ============================================================

# =====================================================================
# PHASE 1 — Agent brain (function-calling). SEPARATE from the chatbot
# on/off toggle: this only chooses WHICH brain runs when the bot is ON.
# Gated by WHATSAPP_AI_AGENT_MODE (default OFF) → legacy pure-RAG path is
# used unless explicitly enabled. Per-workspace override lands in a later phase.
# =====================================================================
def _ai_agent_mode_enabled(workspace_id: Optional[Any] = None) -> bool:
    """NEW advanced-agent brain gate — SEPARATE from the chatbot on/off toggle.
    ON when the global env flag is set OR the workspace's WhatsApp account has
    ai_agent_mode=True (the per-workspace UI toggle). FAIL-SAFE: any DB error →
    env-only result, so behavior never breaks."""
    raw = (os.getenv("WHATSAPP_AI_AGENT_MODE", "false") or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if workspace_id:
        try:
            from .models import WhatsAppAccount
            acc = (
                WhatsAppAccount.query
                .filter_by(workspace_id=str(workspace_id))
                .filter(WhatsAppAccount.ai_agent_mode.is_(True))
                .first()
            )
            if acc is not None:
                return True
        except Exception:
            pass
    return False


AGENT_SYSTEM_ADDENDUM = """

You are an AI assistant for this business on WhatsApp. You may call tools to help the customer.
- Call `search_knowledge_base` AT MOST ONCE per customer question to look up FAQs, policies, products and services.
- If it returns results, base your answer on them; NEVER invent prices, policies, or product details.
- If it returns NO results, do NOT call it again — answer helpfully from the conversation, or say a team member will assist shortly.
- When the customer wants to SEE, browse or buy products / see the catalog, call `send_products` to send visual product cards (do NOT just list them as text). When offering clear choices or a next step, call `send_buttons` (max 3). These visual messages are sent ALONGSIDE your text reply — so still write a short friendly text reply too.
- When the customer has AGREED to buy and the amount is known, confirm the amount, then call `request_payment` to send them a secure payment link. Pass the EXACT amount the customer stated or the exact product/cart price — copy the number precisely, never round it, add to it, or change it. If the customer says "1 rupee", the amount is exactly 1.
- When the customer asks for a human/agent, is upset, or has a request you cannot handle, call `escalate_to_human` and tell them a team member will follow up shortly.
- When the customer tells you their name, email, or company, call `capture_lead` and pass those EXACT values (from their message), plus their interest. Address the customer by the name they just gave — not any older saved name.
- To send an approved TEMPLATE you MUST call `send_template` — never just type the template's wording as a normal text message. Call `list_templates` first to see the exact names and how many {{n}} variables each needs, then `send_template` with the values in order. Good times to send a template: a welcome/greeting template when the customer says hi/hello or it's first contact, and structured content like an order/booking confirmation, a reminder, or an offer. Do NOT send a template that doesn't fit the moment, and never invent a template name. For everything else, reply with normal text/interactive.
- Always finish your turn with a plain-text reply to the customer (no markdown), match the customer's language, keep it concise for WhatsApp."""


def _agent_guardrails(workspace_id: Optional[Any]) -> Dict[str, Any]:
    """PHASE 6: per-workspace guardrails from the WhatsApp account row.
    Returns {'disabled': set[str], 'max_payment': int|None}. FAIL-SAFE: on any
    error returns empty guardrails (nothing disabled), never raising."""
    disabled: set = set()
    max_payment: Optional[int] = None
    try:
        if workspace_id:
            from .models import WhatsAppAccount
            acc = (WhatsAppAccount.query
                   .filter_by(workspace_id=str(workspace_id), is_active=True)
                   .first())
            if acc is not None:
                raw = getattr(acc, "ai_agent_disabled_tools", None) or ""
                disabled = {t.strip() for t in str(raw).split(",") if t.strip()}
                mp = getattr(acc, "ai_agent_max_payment", None)
                if mp is not None:
                    max_payment = int(mp)
    except Exception:
        pass
    return {"disabled": disabled, "max_payment": max_payment}


def _agent_tools(disabled: Optional[set] = None) -> List[Tool]:
    """Tool declarations exposed to the model. PHASE 6: filters out any tool named
    in `disabled` (per-workspace guardrail) so the model never sees turned-off tools."""
    disabled = disabled or set()
    decls = [
            FunctionDeclaration(
                name="search_knowledge_base",
                description=(
                    "Search the business knowledge base (FAQs, policies, products, services) "
                    "for information to answer the customer's question."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "What to look up — usually the customer's question or a keyword.",
                        }
                    },
                    "required": ["query"],
                },
            ),
            # PHASE 2 — workspace-knowledge tools.
            FunctionDeclaration(
                name="get_business_info",
                description=(
                    "Get THIS business's own profile — name, industry/type, description, website, "
                    "city and country. Use when the customer asks who you are, what the business does, "
                    "where it is located, or for general company info."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="list_products",
                description=(
                    "List the business's products/services from their connected WhatsApp catalog "
                    "(name, price, description, availability). Use when the customer asks what you "
                    "sell, your products or services, prices, or to see the catalog."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            # PHASE 3 — visual/interactive sends (fully dynamic, no template approval,
            # valid inside the 24h customer-service window).
            FunctionDeclaration(
                name="send_products",
                description=(
                    "Send the customer an interactive PRODUCT CARD message from the connected "
                    "WhatsApp catalog (tappable cards with image, name and price). Use this — "
                    "instead of just listing text — whenever the customer wants to see, browse "
                    "or buy products, or asks to see the catalog. Optionally pass product_names "
                    "to feature specific items; omit to show the whole catalog. Always also give "
                    "a short friendly text reply; the cards are sent alongside it."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "product_names": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional. Names of specific products to feature. Omit for all.",
                        },
                        "body_text": {
                            "type": "string",
                            "description": "Short message shown above the product cards (e.g. 'Here are our products 🛍️').",
                        },
                    },
                },
            ),
            FunctionDeclaration(
                name="send_buttons",
                description=(
                    "Send the customer up to 3 tappable REPLY BUTTONS so they can pick an option "
                    "with one tap (e.g. 'Talk to a human', 'See prices', 'Book a call'). Use when "
                    "offering clear choices or a next step. Always also give a short text reply."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "body_text": {
                            "type": "string",
                            "description": "The question/prompt shown above the buttons.",
                        },
                        "buttons": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "1 to 3 short button labels (max ~20 chars each).",
                        },
                    },
                    "required": ["body_text", "buttons"],
                },
            ),
            # PHASE 4 — transactions & handoff.
            FunctionDeclaration(
                name="request_payment",
                description=(
                    "Create a secure PayU payment link for an amount and send it to the customer "
                    "in this chat. Use ONLY when the customer has agreed to buy / pay and an amount "
                    "is known (e.g. after they pick a product or confirm a price). Always confirm the "
                    "amount with the customer first. After sending, tell them the payment link is on its way."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "number",
                            "description": "Exact total to charge in INR — copy the number the customer stated or the exact product/cart price. Never round or change it (customer says '1 rupee' → 1).",
                        },
                        "product_info": {
                            "type": "string",
                            "description": "Short description of what is being paid for (e.g. 'Sociovia Meta automation').",
                        },
                        "customer_name": {
                            "type": "string",
                            "description": "Optional customer name if known from the conversation.",
                        },
                        "customer_email": {
                            "type": "string",
                            "description": "Optional customer email if known from the conversation.",
                        },
                    },
                    "required": ["amount", "product_info"],
                },
            ),
            FunctionDeclaration(
                name="escalate_to_human",
                description=(
                    "Hand this conversation over to a human team member and flag it for attention in "
                    "the business inbox. Use when the customer explicitly asks for a human/agent, is "
                    "upset, or has a request you cannot handle. After calling, tell the customer a team "
                    "member will get back to them shortly."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Brief reason for the handoff (e.g. 'customer requested human', 'complaint').",
                        },
                    },
                },
            ),
            FunctionDeclaration(
                name="capture_lead",
                description=(
                    "Save this customer as a lead in the business CRM so the team can follow up. "
                    "Use once you learn who they are or what they want. IMPORTANT: whenever the customer "
                    "states their name, email, or company in the chat, you MUST pass those exact values "
                    "in this call — copy them from the customer's own message, even if a different saved "
                    "name already exists. Do not leave name/email/company blank when the customer just "
                    "gave them. The customer's phone number is captured automatically."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Customer's name if known."},
                        "email": {"type": "string", "description": "Customer's email if shared."},
                        "company": {"type": "string", "description": "Customer's company if mentioned."},
                        "interest": {
                            "type": "string",
                            "description": "Short note on what they want / are interested in (e.g. 'Meta ads automation').",
                        },
                    },
                },
            ),
            FunctionDeclaration(
                name="book_appointment",
                description=(
                    "Record an appointment / meeting / call the customer wants to book. Use whenever the "
                    "customer indicates a day and time, in ANY wording or order (e.g. 'tomorrow at 4pm', "
                    "'next Monday 11', '3pm on the 10th', 'Sept 10 afternoon'). Resolve it against today's "
                    "date (given in your instructions). Confirm the final date and time back to them."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "date": {"type": "string", "description": "Appointment date, preferably YYYY-MM-DD (other formats are accepted and normalized)."},
                        "time": {"type": "string", "description": "Appointment time, preferably 24h HH:MM (e.g. '3pm' is also accepted)."},
                        "service": {"type": "string", "description": "What the appointment is for (e.g. 'demo call')."},
                        "customer_name": {"type": "string", "description": "Customer's name if known."},
                    },
                    "required": ["date", "time"],
                },
            ),
            # PHASE 5 — approved WhatsApp templates (structured / re-engagement content).
            FunctionDeclaration(
                name="list_templates",
                description=(
                    "List the business's APPROVED WhatsApp message templates (name, category, the "
                    "body text and how many {{n}} variables each needs). Call this BEFORE send_template "
                    "so you know which templates exist and what variables to fill. Templates are pre-approved "
                    "formats used for structured messages like confirmations, reminders and offers."
                ),
                parameters={"type": "object", "properties": {}},
            ),
            FunctionDeclaration(
                name="send_template",
                description=(
                    "Send one of the business's APPROVED templates to the customer, filling its {{1}}, {{2}}… "
                    "body variables in order. Use for structured/approved content (order or booking confirmation, "
                    "reminder, offer) — especially to re-engage a customer. Only use a template name returned by "
                    "list_templates, and provide exactly the number of body values it needs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "template_name": {"type": "string", "description": "Exact template name from list_templates."},
                        "body_params": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Values for the body {{1}},{{2}}… variables, in order. Empty if the template has none.",
                        },
                        "language": {"type": "string", "description": "Template language code (e.g. 'en_US'); omit to use the template's default."},
                    },
                    "required": ["template_name"],
                },
            ),
        ]
    kept = [d for d in decls if getattr(d, "name", None) not in disabled]
    return [Tool(function_declarations=kept)] if kept else []


class WhatsAppAIChatbot:
    """AI-powered chatbot for WhatsApp conversations."""
    
    def __init__(self, config: Optional[AIConfig] = None):
        self.config = config or AIConfig()
        self.client = None
        self._initialized = False
        self._init_error: Optional[str] = None
        self._initialize()
    
    def _initialize(self):
        """Initialize GenAI Vertex Client."""
        try:
            # Use the tenant's own Gemini key when this chatbot is scoped to a
            # workspace; falls back to the global client when unset/no override.
            self.client = get_genai_client(workspace_id=self.config.workspace_id)
            if not self.client:
                self._init_error = "API not configured"
                return
            
            self._initialized = True
            logger.info(f"AI Chatbot initialized: model={self.config.model}")
            
        except Exception as e:
            self._init_error = str(e)
            logger.exception(f"AI Chatbot init failed: {e}")
    
    def is_available(self) -> bool:
        return self._initialized and self.client is not None

    def get_tool_log(self) -> List[Dict[str, Any]]:
        """Return the list of tool calls made during the last agentic run."""
        return getattr(self, "_agent_tool_log", [])

    @staticmethod
    def _build_contents(context: Optional[List[Dict[str, str]]], current_message: str):
        """Build a Gemini `contents` list from prior conversation turns + the current message.
        Gemini requires the first turn to be from the user and roles to alternate, so leading
        'model' turns are dropped and consecutive same-role turns are merged. Always ends with
        the current user message (which may include RAG wrapping)."""
        turns: List[Dict[str, Any]] = []
        for item in (context or []):
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            g_role = "model" if item.get("role") == "model" else "user"
            if not turns and g_role != "user":
                continue  # Gemini: first turn must be 'user'
            if turns and turns[-1]["role"] == g_role:
                turns[-1]["parts"][0]["text"] += "\n" + text
            else:
                turns.append({"role": g_role, "parts": [{"text": text}]})
        if turns and turns[-1]["role"] == "user":
            turns[-1]["parts"][0]["text"] += "\n" + current_message
        else:
            turns.append({"role": "user", "parts": [{"text": current_message}]})
        return turns

    def _ensure_guardrails(self) -> Dict[str, Any]:
        """Load PHASE 6 guardrails on demand (so direct tool calls are also protected,
        not just calls routed through _generate_agentic)."""
        g = getattr(self, "_guardrails", None)
        if not g:
            g = _agent_guardrails(self.config.workspace_id)
            self._guardrails = g
        return g

    def _execute_agent_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Run one agent tool and return a JSON-serializable result. FAIL-SAFE: never
        raises. PHASE 6: enforces per-workspace tool disables + a per-turn call cap,
        and emits an analytics log line for every tool call."""
        # PHASE 6 guardrails (defense in depth — disabled tools are also hidden from
        # the model, but block here too in case one slips through).
        guard = self._ensure_guardrails()
        if name in (guard.get("disabled") or set()):
            logger.info("[ai_chatbot][agent][tool] ws=%s conv=%s tool=%s BLOCKED=disabled",
                        self.config.workspace_id, self.config.conversation_id, name)
            return {"error": "tool_disabled",
                    "note": f"The {name} action is turned off for this business."}
        self._agent_toolcalls = getattr(self, "_agent_toolcalls", 0) + 1
        if self._agent_toolcalls > getattr(self, "_agent_max_toolcalls", 8):
            logger.warning("[ai_chatbot][agent][tool] ws=%s conv=%s tool=%s BLOCKED=rate_cap n=%s",
                           self.config.workspace_id, self.config.conversation_id, name, self._agent_toolcalls)
            return {"error": "rate_limited",
                    "note": "Too many actions in one turn — please continue in text."}
        logger.info("[ai_chatbot][agent][tool] ws=%s conv=%s tool=%s call#%s",
                    self.config.workspace_id, self.config.conversation_id, name, self._agent_toolcalls)
        try:
            if name == "search_knowledge_base":
                q = (args.get("query") or "").strip()
                chunks, _hc = get_rag_context(
                    query=q,
                    workspace_id=self.config.workspace_id,
                    top_k=self.config.rag_top_k,
                    threshold=self.config.rag_confidence_threshold,
                )
                chunks = chunks or []
                texts: List[str] = []
                for c in chunks[:5]:
                    t = c.get("text") or c.get("content") or c.get("chunk") or ""
                    if t:
                        texts.append(t)
                mx = max((float(c.get("score", 0) or 0) for c in chunks), default=0.0)
                out: Dict[str, Any] = {"results": texts, "count": len(texts), "max_score": mx}
                if not texts:
                    out["note"] = ("No knowledge-base entries matched. Do NOT search again — "
                                   "answer the customer directly or offer to connect a team member.")
                return out
            if name == "get_business_info":
                from app import db as _db
                from sqlalchemy import text as _text
                row = _db.session.execute(
                    _text(
                        "select business_name, business_type, industry, description, website, "
                        "city, country, usp from workspaces2 where id::text = :wid limit 1"
                    ),
                    {"wid": str(self.config.workspace_id)},
                ).mappings().first()
                if not row:
                    return {"found": False, "note": "No business profile on file for this workspace."}
                info = {k: v for k, v in dict(row).items() if v}
                return {"found": True, "business": info}
            if name == "list_products":
                import requests as _rq
                from .models import WhatsAppAccount as _WA
                from .encryption import decrypt_token as _dt
                acc = _WA.query.filter_by(workspace_id=str(self.config.workspace_id), is_active=True).first()
                if not acc or not acc.access_token_encrypted or not acc.waba_id:
                    return {"found": False, "note": "No connected WhatsApp catalog for this business."}
                tok = _dt(acc.access_token_encrypted)
                if not tok:
                    return {"found": False, "note": "Catalog is temporarily unavailable."}
                api = os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION") or "v23.0"
                base = "https://graph.facebook.com/" + api
                auth = {"Authorization": "Bearer " + tok}
                cr = _rq.get(base + "/" + str(acc.waba_id) + "/product_catalogs",
                             params={"fields": "id,name,product_count"}, headers=auth, timeout=6).json()
                cats = cr.get("data") or []
                cat = next((c for c in cats if (c.get("product_count") or 0) > 0), cats[0] if cats else None)
                if not cat:
                    return {"found": False, "note": "No product catalog is connected yet."}
                pr = _rq.get(base + "/" + str(cat["id"]) + "/products",
                             params={"fields": "name,price,description,availability", "limit": 20},
                             headers=auth, timeout=6).json()
                prods = []
                for p in (pr.get("data") or [])[:20]:
                    prods.append({k: p.get(k) for k in ("name", "price", "description", "availability") if p.get(k)})
                if not prods:
                    return {"found": False, "note": "The catalog has no products listed yet."}
                return {"found": True, "catalog": cat.get("name"), "count": len(prods), "products": prods}
            if name == "send_products":
                return self._tool_send_products(args)
            if name == "send_buttons":
                return self._tool_send_buttons(args)
            if name == "request_payment":
                return self._tool_request_payment(args)
            if name == "escalate_to_human":
                return self._tool_escalate_to_human(args)
            if name == "capture_lead":
                return self._tool_capture_lead(args)
            if name == "book_appointment":
                return self._tool_book_appointment(args)
            if name == "list_templates":
                return self._tool_list_templates(args)
            if name == "send_template":
                return self._tool_send_template(args)
            return {"error": f"unknown_tool:{name}"}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] tool %s failed: %s", name, e)
            return {"error": str(e), "results": [], "count": 0}

    def _service_for_workspace(self):
        """Build a WhatsAppService bound to this workspace's active account (decrypted
        token). Returns (service, account) or (None, note)."""
        try:
            from .models import WhatsAppAccount
            from .encryption import decrypt_token as _dt
            from .services import WhatsAppService
            acc = WhatsAppAccount.query.filter_by(
                workspace_id=str(self.config.workspace_id), is_active=True
            ).first()
            if not acc or not acc.access_token_encrypted:
                return None, "No active WhatsApp account for this business."
            tok = _dt(acc.access_token_encrypted)
            if not tok:
                return None, "WhatsApp account token is unavailable."
            service = WhatsAppService(
                access_token=tok,
                phone_number_id=acc.phone_number_id,
                waba_id=acc.waba_id,
                workspace_id=acc.workspace_id,
            )
            return service, acc
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] service build failed: %s", e)
            return None, "Messaging is temporarily unavailable."

    def _tool_list_templates(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 5: list the workspace's APPROVED templates so the model can pick one."""
        try:
            from .models import WhatsAppAccount, WhatsAppTemplate
            from sqlalchemy import or_ as _or
            acc = WhatsAppAccount.query.filter_by(
                workspace_id=str(self.config.workspace_id), is_active=True
            ).first()
            if not acc:
                return {"found": False, "note": "No active WhatsApp account for this business."}
            q = WhatsAppTemplate.query.filter_by(account_id=acc.id).filter(
                WhatsAppTemplate.is_archived.isnot(True)
            ).filter(
                _or(WhatsAppTemplate.meta_status == "APPROVED", WhatsAppTemplate.status == "APPROVED")
            )
            out = []
            for t in q.limit(30).all():
                out.append({
                    "name": t.name,
                    "language": t.language,
                    "category": t.category,
                    "body": (t.body_text or "")[:300],
                    "variable_count": t.variable_count or 0,
                })
            if not out:
                return {"found": False, "note": "No approved templates are available for this business."}
            return {"found": True, "count": len(out), "templates": out}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] list_templates failed: %s", e)
            return {"found": False, "note": "Could not load templates."}

    def _tool_send_template(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 5: send an approved template to the customer, filling body variables."""
        template_name = (args.get("template_name") or "").strip()
        if not template_name:
            return {"ok": False, "note": "template_name is required."}
        phone = self.config.customer_phone
        if not phone:
            return {"ok": False, "note": "No customer phone in context; cannot send a template."}
        try:
            from .models import WhatsAppAccount, WhatsAppTemplate
            acc = WhatsAppAccount.query.filter_by(
                workspace_id=str(self.config.workspace_id), is_active=True
            ).first()
            if not acc:
                return {"ok": False, "note": "No active WhatsApp account for this business."}
            # Resolve the template + its language, and validate it is approved.
            trec = WhatsAppTemplate.query.filter_by(account_id=acc.id, name=template_name)
            language = (args.get("language") or "").strip()
            if language:
                trec = trec.filter_by(language=language)
            trec = trec.first()
            if not trec:
                return {"ok": False, "note": f"No template named '{template_name}' found."}
            if not (trec.meta_status == "APPROVED" or trec.status == "APPROVED"):
                return {"ok": False, "note": f"Template '{template_name}' is not approved and cannot be sent."}
            lang = language or trec.language or "en_US"
            body_params = [str(v) for v in (args.get("body_params") or [])]
            components = None
            if body_params:
                components = [{"type": "body", "parameters": [{"type": "text", "text": v} for v in body_params]}]
            service, note = self._service_for_workspace()
            if not service:
                return {"ok": False, "note": note}
            res = service.send_template(
                to=phone,
                template_name=template_name,
                language_code=lang,
                components=components,
                conversation_id=self.config.conversation_id,
            )
            ok = bool(res and res.get("success"))
            return {"ok": ok, "template": template_name,
                    "note": ("Template sent to the customer." if ok
                             else f"Template send failed: {(res or {}).get('error')}")}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] send_template failed: %s", e)
            return {"ok": False, "note": "Could not send the template right now."}

    def _tool_capture_lead(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 4: create/update a CRM lead for this customer. Wraps the shared
        `leads` table via crm_lead_models.CrmLead. Dedups by workspace+phone."""
        phone = self.config.customer_phone
        if not self.config.workspace_id:
            return {"ok": False, "note": "No workspace context."}
        try:
            from app import db
            from .crm_lead_models import CrmLead
            ws = int(self.config.workspace_id)
            name = (args.get("name") or "").strip() or "WhatsApp Lead"
            # Emails never contain spaces; models sometimes transcribe them with
            # stray spaces / capitals (e.g. "Sharan 1114 411@Gmail.com"). Normalize.
            email = (args.get("email") or "").strip()
            if email:
                email = email.replace(" ", "").lower()
                if "@" not in email or "." not in email.split("@")[-1]:
                    email = None  # not a usable email — drop rather than store garbage
            else:
                email = None
            company = (args.get("company") or "").strip() or None
            interest = (args.get("interest") or "").strip()
            source = ("whatsapp_ai_agent" + (f": {interest}" if interest else ""))[:128]
            conv_id = self.config.conversation_id
            existing = None
            if phone:
                existing = CrmLead.query.filter_by(workspace_id=ws, phone=phone).first()
            from datetime import datetime as _dt
            if existing:
                if args.get("name"):
                    existing.name = name
                if email:
                    existing.email = email
                if company:
                    existing.company = company
                if interest:
                    existing.source = source
                existing.last_interaction_at = _dt.utcnow()
                db.session.commit()
                return {"ok": True, "updated": True, "note": "Existing lead updated in the CRM."}
            lead = CrmLead(
                workspace_id=ws, name=name, email=email, phone=phone,
                company=company, status="new", source=source,
                lead_type="whatsapp", conversation_id=conv_id,
                last_interaction_at=_dt.utcnow(),
            )
            db.session.add(lead)
            db.session.commit()
            return {"ok": True, "created": True, "note": "Lead saved to the CRM for follow-up."}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] capture_lead failed: %s", e)
            try:
                from app import db as _db2
                _db2.session.rollback()
            except Exception:
                pass
            return {"ok": False, "note": "Could not save the lead right now."}

    def _tool_book_appointment(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 4: record an appointment in whatsapp_form_bookings. Requires a
        resolvable account for this workspace and a date + time from the customer."""
        raw_date = (args.get("date") or "").strip()
        raw_time = (args.get("time") or "").strip()
        if not raw_date or not raw_time:
            return {"ok": False, "note": "A date and time are required to book."}
        phone = self.config.customer_phone
        if not phone:
            return {"ok": False, "note": "No customer phone in context; cannot book."}
        # Normalize loose/relative date+time into YYYY-MM-DD + HH:MM. The model is
        # told today's date, but customers (and the model) may still pass odd formats
        # ("10 Sept", "3 pm", "09/10/2026") — parse them defensively with dateutil.
        date, time_ = raw_date, raw_time
        # Zero the minute/second in the default so a time like "3 pm" fills minutes
        # as :00 (not the current minute), while a missing date still defaults to today.
        try:
            from dateutil import parser as _dp
            base = datetime.now().replace(minute=0, second=0, microsecond=0)
            dt_combined = _dp.parse(f"{raw_date} {raw_time}", default=base, dayfirst=False, fuzzy=True)
            date = dt_combined.strftime("%Y-%m-%d")
            time_ = dt_combined.strftime("%H:%M")
        except Exception:
            # Fall back to parsing them separately; keep raw values if that also fails.
            try:
                from dateutil import parser as _dp
                base = datetime.now().replace(minute=0, second=0, microsecond=0)
                d = _dp.parse(raw_date, default=base, fuzzy=True)
                date = d.strftime("%Y-%m-%d")
            except Exception:
                pass
            try:
                from dateutil import parser as _dp
                base = datetime.now().replace(minute=0, second=0, microsecond=0)
                t = _dp.parse(raw_time, default=base, fuzzy=True)
                time_ = t.strftime("%H:%M")
            except Exception:
                pass
        try:
            from app import db
            from .models import WhatsAppAccount
            from .flow_os_models import WhatsAppFormBooking
            acc = WhatsAppAccount.query.filter_by(
                workspace_id=str(self.config.workspace_id), is_active=True
            ).first()
            if not acc:
                return {"ok": False, "note": "No active WhatsApp account for this business."}
            booking = WhatsAppFormBooking(
                account_id=acc.id,
                conversation_id=self.config.conversation_id,
                wa_id=phone,
                customer_name=(args.get("customer_name") or None),
                booking_date=date[:10],
                booking_time=time_[:8],
                service_type=(args.get("service") or None),
                status="confirmed",
            )
            db.session.add(booking)
            db.session.commit()
            return {"ok": True, "booking_id": booking.id, "date": date, "time": time_,
                    "note": "Appointment recorded. Confirm the date and time back to the customer."}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] book_appointment failed: %s", e)
            try:
                from app import db as _db2
                _db2.session.rollback()
            except Exception:
                pass
            return {"ok": False, "note": "Could not record the appointment right now."}

    def _tool_request_payment(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 4: create a PayU payment link for the given amount and send it into
        the chat. Wraps commerce_pay.orders.create_order_and_send. Requires the
        workspace to have a WorkspacePaymentConfig (PayU) and a live conversation."""
        try:
            amount = float(args.get("amount") or 0)
        except Exception:
            amount = 0.0
        if amount <= 0:
            return {"ok": False, "note": "A positive amount is required to request payment."}
        # PHASE 6: hard ceiling on a single payment link — per-workspace override, else
        # global env WHATSAPP_AI_MAX_PAYMENT (default 200000 INR). Blocks runaway charges.
        try:
            env_cap = int(os.getenv("WHATSAPP_AI_MAX_PAYMENT", "200000") or 200000)
        except Exception:
            env_cap = 200000
        cap = self._ensure_guardrails().get("max_payment") or env_cap
        if amount > cap:
            logger.warning("[ai_chatbot][agent] request_payment BLOCKED amount=%s > cap=%s ws=%s",
                           amount, cap, self.config.workspace_id)
            return {"ok": False, "note": f"Amount ₹{amount:.0f} exceeds the allowed limit (₹{cap}). "
                    "A team member will help with larger payments."}
        phone = self.config.customer_phone
        conv_id = self.config.conversation_id
        if not phone:
            return {"ok": False, "note": "No customer phone in context; cannot send a payment link."}
        try:
            from .commerce_pay.models import WorkspacePaymentConfig
            from .commerce_pay import orders as _orders
            cfg_row = WorkspacePaymentConfig.query.filter_by(
                workspace_id=int(self.config.workspace_id)
            ).first()
            if not cfg_row:
                return {"ok": False, "note": "Online payments are not set up for this business yet."}
            order, link, sent = _orders.create_order_and_send(
                workspace_id=int(self.config.workspace_id),
                cfg_row=cfg_row,
                phone=phone,
                amount=amount,
                productinfo=(args.get("product_info") or "Order"),
                host_url="",  # falls back to COMMERCE_PUBLIC_BASE_URL / APP_BASE_URL
                conversation_id=conv_id,
                customer_name=(args.get("customer_name") or None),
                customer_email=(args.get("customer_email") or None),
                origin="ai_agent",
            )
            return {"ok": True, "sent": bool(sent), "amount": round(amount, 2),
                    "link": link,
                    "note": "Payment link created and sent to the customer in this chat."}
        except Exception as e:  # noqa: BLE001
            logger.warning("[ai_chatbot][agent] request_payment failed: %s", e)
            return {"ok": False, "note": "Could not create the payment link right now."}

    def _tool_escalate_to_human(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 4: flag this conversation for a human in the inbox and mark the
        ChatResponse so the automation layer escalates. Wraps
        human_escalation.mark_conversation_human_required."""
        reason = (args.get("reason") or "customer requested human").strip()[:200]
        self._agent_escalate = True
        self._agent_escalate_reason = reason
        conv_id = self.config.conversation_id
        if conv_id:
            try:
                from .human_escalation import mark_conversation_human_required
                mark_conversation_human_required(int(conv_id), reason)
            except Exception as e:  # noqa: BLE001
                logger.warning("[ai_chatbot][agent] escalate mark failed: %s", e)
        return {"ok": True, "note": "Conversation flagged for a human team member."}

    def _catalog_for_send(self):
        """Resolve (waba_id-linked) catalog_id + token for the connected account.
        Returns (catalog_id:str, token:str, products:list[dict with retailer_id,name]) or (None, note)."""
        import requests as _rq
        from .models import WhatsAppAccount as _WA
        from .encryption import decrypt_token as _dt
        acc = _WA.query.filter_by(workspace_id=str(self.config.workspace_id), is_active=True).first()
        if not acc or not acc.access_token_encrypted or not acc.waba_id:
            return None, "No connected WhatsApp catalog for this business."
        tok = _dt(acc.access_token_encrypted)
        if not tok:
            return None, "Catalog is temporarily unavailable."
        api = os.getenv("WHATSAPP_API_VERSION") or os.getenv("FB_API_VERSION") or "v23.0"
        base = "https://graph.facebook.com/" + api
        auth = {"Authorization": "Bearer " + tok}
        cr = _rq.get(base + "/" + str(acc.waba_id) + "/product_catalogs",
                     params={"fields": "id,name,product_count"}, headers=auth, timeout=6).json()
        cats = cr.get("data") or []
        cat = next((c for c in cats if (c.get("product_count") or 0) > 0), cats[0] if cats else None)
        if not cat:
            return None, "No product catalog is connected yet."
        pr = _rq.get(base + "/" + str(cat["id"]) + "/products",
                     params={"fields": "retailer_id,name,price,availability", "limit": 30},
                     headers=auth, timeout=6).json()
        prods = [p for p in (pr.get("data") or []) if p.get("retailer_id")]
        return {"catalog_id": str(cat["id"]), "catalog_name": cat.get("name"), "products": prods}, None

    def _tool_send_products(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 3: queue an interactive product_list (or single product) message.
        Resolves the connected catalog, matches optional product_names to retailer_ids,
        and pushes a Meta `interactive` object onto self._agent_interactive."""
        info, note = self._catalog_for_send()
        if not info:
            return {"queued": False, "note": note or "Catalog unavailable."}
        prods = info["products"]
        if not prods:
            return {"queued": False, "note": "The catalog has no products to send."}
        wanted = [str(n).strip().lower() for n in (args.get("product_names") or []) if str(n).strip()]
        if wanted:
            sel = [p for p in prods if (p.get("name") or "").strip().lower() in wanted]
            # fall back to substring match, else all
            if not sel:
                sel = [p for p in prods if any(w in (p.get("name") or "").lower() for w in wanted)]
            if not sel:
                sel = prods
        else:
            sel = prods
        sel = sel[:30]
        body_text = (args.get("body_text") or "Here are our products 🛍️").strip()[:1024]
        catalog_id = info["catalog_id"]
        if len(sel) == 1:
            interactive = {
                "type": "product",
                "body": {"text": body_text},
                "action": {
                    "catalog_id": catalog_id,
                    "product_retailer_id": sel[0]["retailer_id"],
                },
            }
        else:
            interactive = {
                "type": "product_list",
                "header": {"type": "text", "text": (info.get("catalog_name") or "Our Products")[:60]},
                "body": {"text": body_text},
                "action": {
                    "catalog_id": catalog_id,
                    "sections": [{
                        "title": (info.get("catalog_name") or "Products")[:24],
                        "product_items": [{"product_retailer_id": p["retailer_id"]} for p in sel],
                    }],
                },
            }
        self._agent_interactive.append(interactive)
        return {"queued": True, "kind": interactive["type"],
                "sent_count": len(sel),
                "note": "Product cards will be sent to the customer alongside your text reply."}

    def _tool_send_buttons(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """PHASE 3: queue an interactive reply-buttons message (max 3)."""
        body_text = (args.get("body_text") or "").strip()
        labels = [str(b).strip() for b in (args.get("buttons") or []) if str(b).strip()]
        if not body_text or not labels:
            return {"queued": False, "note": "body_text and at least one button are required."}
        buttons = []
        for i, lbl in enumerate(labels[:3]):
            buttons.append({"type": "reply", "reply": {"id": f"btn_{i+1}", "title": lbl[:20]}})
        interactive = {
            "type": "button",
            "body": {"text": body_text[:1024]},
            "action": {"buttons": buttons},
        }
        self._agent_interactive.append(interactive)
        return {"queued": True, "kind": "button", "count": len(buttons),
                "note": "Buttons will be sent to the customer alongside your text reply."}

    def _generate_agentic(self, message: str, context: Optional[List[Dict[str, str]]] = None) -> ChatResponse:
        """PHASE 1 agent brain (behind WHATSAPP_AI_AGENT_MODE, default OFF).
        A Gemini function-calling loop that currently exposes ONE tool
        (search_knowledge_base = the existing RAG). Additive — generate_response()
        wraps this in try/except and falls back to the legacy path on any error."""
        import time
        start_time = time.time()
        self._agent_interactive: List[Dict[str, Any]] = []  # PHASE 3 queue
        self._agent_escalate = False  # PHASE 4: set by escalate_to_human tool
        self._agent_escalate_reason = ""
        # PHASE 6 guardrails: per-workspace disabled tools + payment ceiling, plus a
        # per-turn tool-call cap so a misbehaving model can't spam actions.
        self._guardrails = _agent_guardrails(self.config.workspace_id)
        self._agent_toolcalls = 0
        self._agent_tool_log: List[Dict[str, Any]] = []
        try:
            self._agent_max_toolcalls = int(os.getenv("WHATSAPP_AI_MAX_TOOLCALLS", "8") or 8)
        except Exception:
            self._agent_max_toolcalls = 8
        system_prompt = (self.config.system_prompt or DEFAULT_SYSTEM_PROMPT) + AGENT_SYSTEM_ADDENDUM
        # PHASE 4 fix: give the model today's date so it can resolve relative/loose
        # dates ("tomorrow", "next Friday", "the 10th") for book_appointment.
        try:
            _now = datetime.now()
            system_prompt += (
                f"\n\nToday's date is {_now:%A, %d %B %Y} ({_now:%Y-%m-%d}). "
                "When the customer gives a date/time in ANY format or relative terms "
                "(e.g. 'tomorrow 4pm', 'next Monday', '10 Sept at 3'), work out the exact "
                "calendar date and pass book_appointment date as YYYY-MM-DD and time as 24h HH:MM."
            )
        except Exception:
            pass
        if getattr(self.config, "flow_context", None):
            system_prompt += "\n\nCONVERSATION FLOW CONTEXT:\n" + str(self.config.flow_context)
        contents = self._build_contents(context, message)  # list[dict]
        tools = _agent_tools(self._guardrails.get("disabled"))
        out_tokens = max(256, min(int(self.config.max_tokens or DEFAULT_MAX_OUTPUT_TOKENS), 8192))
        used_rag = False
        rag_chunks = 0
        max_score = 0.0
        from core.genai_bridge import generate_agentic, is_openai_mode, build_function_response_content
        _openai_mode = is_openai_mode()
        MAX_STEPS = 4
        for step in range(MAX_STEPS):
            # On the final step, drop tools so the model MUST return a text reply
            # (guarantees a real answer instead of "loop exhausted").
            force_final = step == (MAX_STEPS - 1)
            response = generate_agentic(
                model=self.config.model,
                contents=contents,
                config=GenerateContentConfig(
                    max_output_tokens=out_tokens,
                    temperature=self.config.temperature,
                    system_instruction=system_prompt,
                    tools=(None if (force_final or not tools) else tools),
                ),
                tools=(None if (force_final or not tools) else tools),
                gemini_client=self.client,
                workspace_id=self.config.workspace_id,
                feature="ai_agent",
            )
            cand = (getattr(response, "candidates", None) or [None])[0]
            parts = list(getattr(getattr(cand, "content", None), "parts", None) or [])
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]

            if not calls:
                text = self._clean_response((response.text or "").strip()) if getattr(response, "text", None) else ""
                elapsed = int((time.time() - start_time) * 1000)
                if not text:
                    return ChatResponse(
                        message=self.config.fallback_message, success=False,
                        escalate_to_human=True, escalation_reason="agent_empty_response",
                        model_used=self.config.model, response_time_ms=elapsed,
                        used_rag=used_rag, rag_chunks=rag_chunks, rag_max_score=max_score,
                    )
                logger.info("[ai_chatbot][agent] done step=%s used_rag=%s chunks=%s ms=%s",
                            step, used_rag, rag_chunks, elapsed)
                return ChatResponse(
                    message=text, success=True, model_used=self.config.model,
                    response_time_ms=elapsed, used_rag=used_rag,
                    rag_chunks=rag_chunks, rag_max_score=max_score,
                    interactive_messages=list(self._agent_interactive),
                    escalate_to_human=bool(self._agent_escalate),
                    escalation_reason=self._agent_escalate_reason,
                )

            # Model asked to call one or more tools. Append the model's response,
            # run the tools, then feed function responses back.
            if getattr(cand, "content", None) is not None:
                contents.append(cand.content)
            if _openai_mode:
                for fc in calls:
                    nm = fc.name
                    fargs = dict(fc.args or {})
                    result = self._execute_agent_tool(nm, fargs)
                    self._agent_tool_log.append({"name": nm, "args": fargs, "result": result, "timestamp": datetime.utcnow().isoformat()})
                    if nm == "search_knowledge_base":
                        used_rag = True
                        rag_chunks = max(rag_chunks, int(result.get("count", 0) or 0))
                        max_score = max(max_score, float(result.get("max_score", 0.0) or 0.0))
                    contents.append(build_function_response_content(nm, result))
            else:
                resp_parts: List[Any] = []
                for fc in calls:
                    nm = fc.name
                    fargs = dict(fc.args or {})
                    result = self._execute_agent_tool(nm, fargs)
                    self._agent_tool_log.append({"name": nm, "args": fargs, "result": result, "timestamp": datetime.utcnow().isoformat()})
                    if nm == "search_knowledge_base":
                        used_rag = True
                        rag_chunks = max(rag_chunks, int(result.get("count", 0) or 0))
                        max_score = max(max_score, float(result.get("max_score", 0.0) or 0.0))
                    resp_parts.append(Part.from_function_response(name=nm, response=result))
                contents.append(Content(role="user", parts=resp_parts))

        logger.warning("[ai_chatbot][agent] loop exhausted after %s steps", MAX_STEPS)
        return ChatResponse(
            message=self.config.fallback_message, success=False,
            escalate_to_human=True, escalation_reason="agent_loop_exhausted",
            model_used=self.config.model,
            response_time_ms=int((time.time() - start_time) * 1000),
            used_rag=used_rag, rag_chunks=rag_chunks, rag_max_score=max_score,
        )

    def generate_response(self, message: str, context: Optional[List[Dict[str, str]]] = None) -> ChatResponse:
        """
        Generate AI response with RAG integration. FAIL-SAFE.
        
        Enhanced behavior:
        - Uses system_prompt from config for Gemini instruction
        - When RAG is enabled but no high-confidence chunks match, continues with guarded general guidance (no KB excerpts)
        - With high-confidence RAG, answers are grounded in the knowledge base only
        """
        import time
        start_time = time.time()
        
        if not self.is_available():
            return ChatResponse(
                message=self.config.fallback_message,
                success=False,
                error=self._init_error,
                escalate_to_human=True,
                escalation_reason="ai_not_configured",
            )

        # PHASE 1 gate: advanced agent brain (separate OFF-by-default toggle,
        # WHATSAPP_AI_AGENT_MODE). When ON, run the function-calling loop; on ANY
        # error fall through to the legacy pure-RAG path below (never regress).
        if _ai_agent_mode_enabled(self.config.workspace_id) and self.config.workspace_id:
            try:
                return self._generate_agentic(message, context)
            except Exception as _agent_e:  # noqa: BLE001
                logger.exception("[ai_chatbot] agent mode error; falling back to legacy RAG: %s", _agent_e)

        try:
            if _ai_debug_enabled():
                logger.info(
                    "[ai_chatbot] start message_len=%s workspace_id=%s use_rag=%s model=%s",
                    len(message or ""),
                    self.config.workspace_id,
                    self.config.use_rag,
                    self.config.model,
                )
            # RAG Retrieval
            rag_chunks = []
            max_score = 0
            chitchat_mode = False
            min_answer_score = _rag_min_answer_score()

            if self.config.use_rag and self.config.workspace_id:
                if _should_skip_rag_for_message(message):
                    logger.debug("RAG skipped for chitchat: %r", (message or "")[:40])
                    chitchat_mode = True
                else:
                    retrieval_threshold = _effective_rag_threshold(
                        message, self.config.rag_confidence_threshold
                    )
                    rag_chunks, _high_conf = get_rag_context(
                        query=message,
                        workspace_id=self.config.workspace_id,
                        top_k=self.config.rag_top_k,
                        threshold=retrieval_threshold,
                    )

                    if rag_chunks:
                        max_score = max(c.get("score", 0) for c in rag_chunks)
                        rag_chunks = [c for c in rag_chunks if c.get("score", 0) >= min_answer_score]

                    logger.info(
                        "RAG result: chunks=%d, max_score=%.3f, threshold=%s, min_answer=%s",
                        len(rag_chunks),
                        max_score,
                        retrieval_threshold,
                        min_answer_score,
                    )

            if _ai_debug_enabled():
                logger.info(
                    "[ai_chatbot] retrieval_summary chunks=%s max_score=%.3f chitchat=%s",
                    len(rag_chunks),
                    max_score,
                    chitchat_mode,
                )

            # Short contextual follow-ups ("why?", "what do you mean?", "what did you verify?")
            # don't match the KB on their own. Expand retrieval with the previous user turn so we
            # can still ground the answer, and NEVER hard-refuse them when we have prior turns —
            # the model answers from the conversation so far (the system prompt keeps it on-business).
            is_followup = _is_contextual_followup(message)
            if (
                self.config.use_rag and self.config.workspace_id and not chitchat_mode
                and is_followup and not rag_chunks
            ):
                prev_user = _last_user_text(context)
                if prev_user:
                    expanded_query = f"{prev_user} {message}".strip()
                    try:
                        rag_chunks, _hc = get_rag_context(
                            query=expanded_query,
                            workspace_id=self.config.workspace_id,
                            top_k=self.config.rag_top_k,
                            threshold=_effective_rag_threshold(
                                expanded_query, self.config.rag_confidence_threshold
                            ),
                        )
                        if rag_chunks:
                            max_score = max(c.get("score", 0) for c in rag_chunks)
                            rag_chunks = [c for c in rag_chunks if c.get("score", 0) >= min_answer_score]
                        logger.info(
                            "RAG follow-up expansion: chunks=%d max_score=%.3f query=%r",
                            len(rag_chunks), max_score, expanded_query[:80],
                        )
                    except Exception as exc:
                        logger.warning("RAG follow-up expansion failed: %s", exc)

            # KB-only mode: refuse off-topic / low-confidence questions without calling the model.
            # Exception: contextual follow-ups WITH history fall through and are answered from the
            # conversation instead of being hard-refused.
            if (
                self.config.use_rag and not chitchat_mode
                and (not rag_chunks or max_score < min_answer_score)
                and not is_followup
            ):
                return ChatResponse(
                    message=KB_REFUSAL_MESSAGE,
                    success=True,
                    model_used=self.config.model,
                    response_time_ms=int((time.time() - start_time) * 1000),
                    used_rag=False,
                    rag_chunks=0,
                    low_rag_confidence=True,
                    rag_max_score=max_score,
                )

            # Build enhanced message with RAG context
            enhanced_message = message
            if rag_chunks:
                enhanced_message = build_rag_enhanced_message(message, rag_chunks)
            
            effective_system_prompt = self.config.system_prompt
            if not effective_system_prompt or effective_system_prompt == DEFAULT_SYSTEM_PROMPT:
                effective_system_prompt = DEFAULT_SYSTEM_PROMPT
            
            if self.config.use_rag and rag_chunks and not is_followup:
                effective_system_prompt += f"""

CRITICAL RULES (NEVER VIOLATE):
- Answer ONLY from the provided knowledge base context
- Do NOT use general knowledge, coding help, math, or facts outside the knowledge base
- If the answer is not in the knowledge base, reply exactly: "{KB_REFUSAL_MESSAGE}"
- NEVER invent prices, policies, features, or product details
- Plain text only, no markdown; match the customer's language"""
            elif self.config.use_rag and is_followup:
                # Short follow-up ("why?", "what?", "what did you verify?"): answer from BOTH the
                # knowledge base context AND the prior conversation. Critically, do NOT instruct the
                # model to parrot the KB refusal — that is what made vague follow-ups get deflected
                # with "I can only answer questions about our business…" even mid-conversation.
                effective_system_prompt += """

CRITICAL RULES (NEVER VIOLATE):
- This message is a short follow-up to the conversation above. Read the earlier turns and answer it directly and helpfully.
- Use the knowledge base context for facts when relevant; NEVER invent prices, policies, features, or product details.
- Do NOT deflect with a canned "I can only answer questions about our business" message — the customer is continuing the same conversation.
- If you genuinely cannot tell what they are referring to, ask one short clarifying question instead of refusing.
- Plain text only, no markdown; match the customer's language"""

            # Flow-awareness: if the user paused an interactive flow to ask this, tell the model
            # so it answers in context (and does not try to drive or repeat the flow itself).
            if getattr(self.config, "flow_context", None):
                effective_system_prompt += (
                    "\n\nCONVERSATION FLOW CONTEXT:\n" + str(self.config.flow_context) +
                    "\n- Answer the user's current question directly and helpfully using the conversation so far."
                    "\n- Do NOT re-ask or restate the flow's question yourself; the system handles resuming the flow."
                )

            # Wire prior conversation turns into the model call so it actually has memory of the
            # chat (previously `context` was fetched but never passed to the model — the model saw
            # only the single current message). Only switch to the multi-turn contents list when
            # there is real history; otherwise keep the original single-string call (no behavior
            # change for brand-new conversations).
            contents = enhanced_message
            _history = self._build_contents(context, enhanced_message)
            if len(_history) > 1:
                contents = _history

            out_tokens = max(256, min(int(self.config.max_tokens or DEFAULT_MAX_OUTPUT_TOKENS), 8192))
            from core.genai_bridge import generate_text
            response = generate_text(
                model=self.config.model,
                contents=contents,
                config=GenerateContentConfig(
                    max_output_tokens=out_tokens,
                    temperature=self.config.temperature,
                    system_instruction=effective_system_prompt,
                ),
                gemini_client=self.client,
                workspace_id=self.config.workspace_id,
                feature="ai_chat",
            )

            response_text = self._clean_response(response.text.strip()) if response.text else ""
            
            # Safety: If response is empty or too short, use fallback
            if not response_text or len(response_text) < 5:
                if _ai_debug_enabled():
                    logger.warning(
                        "[ai_chatbot] fallback empty_response len=%s",
                        len(response_text or ""),
                    )
                return ChatResponse(
                    message=self.config.fallback_message,
                    success=False,
                    error="Empty response from AI",
                    low_rag_confidence=not rag_chunks,
                    rag_max_score=max_score,
                    escalate_to_human=True,
                    escalation_reason="empty_ai_response",
                )
            
            # WhatsApp text body limit is 4096 characters
            if len(response_text) > 4000:
                response_text = response_text[:3997] + "..."

            # Meter AI usage (fail-soft; must never break generation)
            try:
                from subscription.service import record_ai_usage, resolve_workspace_owner
                _uid, _wid = resolve_workspace_owner(self.config.workspace_id)
                record_ai_usage(_uid, _wid, "ai_chatbot", self.config.model)
            except Exception:
                pass

            tokens = 0 # Usage metadata handling differs in new SDK
            elapsed_ms = int((time.time() - start_time) * 1000)

            from .human_escalation import should_escalate_to_human

            escalate, escalation_reason = should_escalate_to_human(
                success=True,
                reply_text=response_text,
                low_rag_confidence=not rag_chunks,
                has_rag_context=bool(rag_chunks),
            )
            if _ai_debug_enabled():
                logger.info(
                    "[ai_chatbot] decision success=%s escalate=%s reason=%s reply_preview=%r",
                    True,
                    escalate,
                    escalation_reason or "",
                    response_text[:160],
                )
            
            return ChatResponse(
                message=response_text,
                success=True,
                tokens_used=tokens,
                model_used=self.config.model,
                response_time_ms=elapsed_ms,
                used_rag=len(rag_chunks) > 0,
                rag_chunks=len(rag_chunks),
                low_rag_confidence=not rag_chunks,
                rag_max_score=max_score,
                escalate_to_human=escalate,
                escalation_reason=escalation_reason,
            )
            
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.exception(f"AI response failed: {e}")
            if _ai_debug_enabled():
                logger.warning(
                    "[ai_chatbot] fallback exception=%s elapsed_ms=%s",
                    type(e).__name__,
                    elapsed_ms,
                )
            return ChatResponse(
                message=self.config.fallback_message,
                success=False,
                error=str(e),
                response_time_ms=elapsed_ms,
                escalate_to_human=True,
                escalation_reason="ai_exception",
            )
    
    def _clean_response(self, text: str) -> str:
        """Remove markdown formatting."""
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'_(.+?)_', r'\1', text)
        text = re.sub(r'~~(.+?)~~', r'\1', text)
        text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
        text = re.sub(r'`(.+?)`', r'\1', text)
        text = re.sub(r'^#+\s*', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
        return text.strip()


# ============================================================
# Factory Functions
# ============================================================

def create_ai_chatbot(config_dict: Optional[Dict[str, Any]] = None) -> WhatsAppAIChatbot:
    """Factory function to create AI chatbot."""
    if config_dict:
        config = AIConfig(
            enabled=config_dict.get("enabled", False),
            system_prompt=config_dict.get("system_prompt", DEFAULT_SYSTEM_PROMPT),
            model=_resolve_generation_model(config_dict.get("model", DEFAULT_MODEL)),
            max_tokens=config_dict.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            temperature=config_dict.get("temperature", 0.7),
            fallback_message=normalize_fallback_message(
                config_dict.get("fallback_message", AIConfig.fallback_message)
            ),
            context_messages=config_dict.get("context_messages", 20),
            use_rag=config_dict.get("use_rag", True),
            rag_top_k=config_dict.get("rag_top_k", 5),
            rag_confidence_threshold=config_dict.get("rag_confidence_threshold", 0.5),
            workspace_id=config_dict.get("workspace_id"),
            knowledge_base_id=config_dict.get("knowledge_base_id"),
            flow_context=config_dict.get("flow_context"),
        )
    else:
        config = AIConfig()
    
    return WhatsAppAIChatbot(config=config)


def generate_ai_response(
    message: str,
    system_prompt: Optional[str] = None,
    context: Optional[List[Dict[str, str]]] = None,
    fallback_message: Optional[str] = None,
    workspace_id: Optional[str] = None,
    use_rag: bool = True,
) -> ChatResponse:
    """Convenience function to generate AI response."""
    config = AIConfig(
        enabled=True,
        system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
        fallback_message=normalize_fallback_message(
            fallback_message or AIConfig.fallback_message
        ),
        use_rag=use_rag,
        workspace_id=workspace_id,
    )
    
    chatbot = WhatsAppAIChatbot(config=config)
    return chatbot.generate_response(message=message, context=context)


# ============================================================
# Testing
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    print("Testing AI Chatbot...")
    
    print("\n1. Testing intent classification...")
    r = classify_intent("Hello, what services do you offer?")
    print(f"   Intent: {r.intent}, Success: {r.success}")
    
    print("\n2. Testing response generation...")
    resp = generate_ai_response("Hello!", system_prompt="You are a helpful assistant.")
    print(f"   Success: {resp.success}")
    print(f"   Message: {resp.message}")
    
    print("\nDone!")


# --- Compatibility shims for rag_engine test-RAG path (restored) ---
DEFAULT_HANDOFF_MESSAGE = DEFAULT_FALLBACK_MESSAGE


def build_business_system_prompt(*args, **kwargs):
    """Return the default system prompt. Kept for rag_engine.generate_answer_with_gemini."""
    return DEFAULT_SYSTEM_PROMPT
