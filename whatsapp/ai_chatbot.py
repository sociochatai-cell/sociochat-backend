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
from google.genai.types import HttpOptions, GenerateContentConfig

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


def _gemini_api_key() -> str:
    return (
        os.environ.get("GOOGLE_GENAI_API_KEY", "")
        or os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    ).strip()


def _use_vertex_ai() -> bool:
    return os.environ.get("GEMINI_USE_VERTEX", "").lower() in ("1", "true", "yes")


def get_genai_runtime_status() -> Dict[str, Any]:
    """Lightweight runtime check used by fast_router (placeholder gating)."""
    if _gemini_api_key() and not _use_vertex_ai():
        return {"available": True, "mode": "api_key"}
    client = get_genai_client()
    return {
        "available": client is not None,
        "mode": _genai_client_mode or ("vertex" if client else "unconfigured"),
    }


def get_genai_client():
    """Get or initialize the GenAI client (API key or Vertex — same logic as rag_engine)."""
    global _genai_client, _genai_client_mode

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


def classify_intent(message: str, model_name: Optional[str] = None) -> IntentResult:
    """Classify the intent of a customer message. FAIL-SAFE."""
    import time
    start_time = time.time()
    
    try:
        client = get_genai_client()
        if not client:
            return IntentResult(intent="other", success=False, error="API not configured")
        
        prompt = INTENT_CLASSIFICATION_PROMPT.format(message=message[:300])
        
        response = client.models.generate_content(
            model=model_name or DEFAULT_MODEL,
            contents=prompt,
            config=GenerateContentConfig(
                max_output_tokens=256,
                temperature=0.1
            )
        )
        
        elapsed_ms = int((time.time() - start_time) * 1000)
        
        if not response.text:
            return IntentResult(intent="other", success=False, error="Empty response", response_time_ms=elapsed_ms)
        
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
            self.client = get_genai_client()
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
            response = self.client.models.generate_content(
                model=self.config.model,
                contents=contents,
                config=GenerateContentConfig(
                    max_output_tokens=out_tokens,
                    temperature=self.config.temperature,
                    system_instruction=effective_system_prompt,
                )
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
