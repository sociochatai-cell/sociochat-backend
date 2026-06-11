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

# ============================================================
# Configuration
# ============================================================

# Model for chat responses (Gemini Flash family)
DEFAULT_MODEL = (
    os.environ.get("TEXT_MODEL")
    or os.environ.get("GEMINI_MODEL")
    or "gemini-2.0-flash"
)

_MODEL_ALIASES = {
    "gemini-1.5-flash": "gemini-2.0-flash",
    "gemini-1.5-pro": "gemini-2.0-flash",
    "gemini-pro": "gemini-2.0-flash",
    "gemini-2.0-flash-exp": "gemini-2.0-flash",
}


def _resolve_generation_model(candidate: Optional[str]) -> str:
    model = str(candidate or "").strip()
    if not model:
        return DEFAULT_MODEL
    return _MODEL_ALIASES.get(model, model)

# RAG configuration
RAG_INDEX_BASE_DIR = Path(os.environ.get("KNOWLEDGE_INDEX_DIR", "faiss_indexes"))
RAG_CONFIDENCE_THRESHOLD = float(os.getenv("WHATSAPP_RAG_CONFIDENCE_THRESHOLD", "0.25"))
RAG_TOP_K = int(os.getenv("WHATSAPP_RAG_TOP_K", "5"))
DEFAULT_MAX_OUTPUT_TOKENS = int(os.getenv("WHATSAPP_AI_MAX_TOKENS", "1024"))
MAX_RESPONSE_CHARS = int(os.getenv("WHATSAPP_AI_MAX_RESPONSE_CHARS", "8000"))

_RAG_SKIP_CHITCHAT = frozenset({
    "hi", "hello", "hey", "hola", "namaste", "ok", "okay", "yes", "no", "yeah", "yep", "nope",
    "thanks", "thank", "thankyou", "ty", "thx", "bye", "goodbye", "good", "fine", "cool", "hii",
})

DEFAULT_HANDOFF_MESSAGE = (
    "I don't have enough information on this. "
    "I'll connect you with our team who can assist you further."
)

DEFAULT_FALLBACK_MESSAGE = os.getenv("WHATSAPP_HANDOFF_CUSTOMER_MESSAGE", "").strip() or DEFAULT_HANDOFF_MESSAGE

_IDENTITY_LEAK_PATTERNS = (
    r"\b(?:i am|i'm|as) an? (?:ai|artificial intelligence|language model|chatbot|bot)\b",
    r"\b(?:powered by|built with|using) (?:gemini|google|openai|chatgpt|gpt)\b",
    r"\b(?:gemini|google ai|openai|chatgpt|large language model|llm)\b",
    r"\bsociovia\b",
    r"\b(?:trained by|created by) (?:google|openai|meta)\b",
)

_NO_KNOWLEDGE_PATTERNS = (
    r"don'?t have (?:that|such|this|any|the|enough)?\s*information",
    r"do not have (?:that|such|this|any|the|enough)?\s*information",
    r"not in (?:my|the|our) knowledge",
    r"not in the knowledge base",
    r"cannot find (?:that|this|any)",
    r"can'?t find (?:that|this|any)",
    r"no information (?:about|on|regarding)",
    r"unable to find (?:that|this|any)",
)

# Requests outside business support scope — never answer from RAG or general knowledge.
_OFF_TOPIC_PATTERNS = (
    r"\b(?:write|create|generate|build|make|show|give|teach)\b.{0,50}\b(?:python|java|javascript|html|css|c\+\+|sql|code|program|script|algorithm)\b",
    r"\b(?:python|java|javascript|html|c\+\+)\b.{0,40}\b(?:program|code|script|function|class)\b",
    r"\b(?:programming|coding|debug|compile)\b",
    r"\b(?:essay|poem|story|joke|homework|assignment)\b.{0,40}\b(?:write|generate|create)\b",
    r"\b(?:random\s+number|hello\s+world|fibonacci|sorting)\b.{0,40}\b(?:program|code|write)\b",
)

_CODE_RESPONSE_PATTERNS = (
    r"^\s*(?:import |from |def |class |print\(|#include|public static)",
    r"```",
    r"\bimport\s+\w+",
    r"\brandom\.randint\b",
    r"\bconsole\.log\b",
)

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """You are a customer support representative for {business_name}.

You speak ONLY on behalf of {business_name}. You are not a generic assistant.

IDENTITY RULES (NEVER BREAK):
- Never say you are an AI, chatbot, language model, Gemini, Google, OpenAI, or Sociovia
- Never mention how you work, your training, embeddings, or internal systems
- If asked who you are, say you represent {business_name} and can help with their services
- Never reveal that answers come from a knowledge base or documents

COMMUNICATION STYLE:
- Polite, professional, clear and complete
- Simple questions: 2-4 sentences. Detailed business questions: give a full answer (up to 12-15 sentences) with all relevant facts from the knowledge — do not truncate or omit important details
- Plain text only — no markdown, no bullet lists, no emojis unless the customer uses them first
- Match the customer's language (English, Hindi, Hinglish, Telugu, Tinglish)

KNOWLEDGE RULES (CRITICAL):
- Answer ONLY using the business knowledge provided in the conversation
- NEVER invent prices, policies, offers, dates, locations, or product details
- NEVER write code, programs, scripts, or technical tutorials
- NEVER answer coding, homework, trivia, or unrelated general-knowledge requests
- If the answer is not in the provided knowledge, reply EXACTLY:
  "{handoff_message}"
- Do not guess or fill gaps with general knowledge"""


# ============================================================
# GenAI Configuration
# ============================================================

_genai_client = None


def get_genai_client():
    """Get or initialize the GenAI client.

    Local dev: set GEMINI_API_KEY (or GOOGLE_API_KEY) — uses Google AI API.
    Production: set GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT for Vertex AI.
    """
    global _genai_client

    if _genai_client:
        return _genai_client

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if api_key:
        try:
            logger.info("Initializing GenAI Client (API key mode)")
            _genai_client = genai.Client(api_key=api_key)
            logger.info("GenAI Client initialized (API key mode)")
            return _genai_client
        except Exception as e:
            logger.error(f"GenAI Client init failed (API key mode): {e}")
            return None

    project = os.environ.get("GCP_PROJECT") or os.environ.get("PROJECT_ID")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
    adc_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    if not project and not adc_path:
        logger.warning(
            "GenAI not configured: set GEMINI_API_KEY for local dev, "
            "or GOOGLE_APPLICATION_CREDENTIALS + GCP_PROJECT for Vertex AI"
        )
        return None

    project = project or "angular-sorter-473216-k8"

    try:
        logger.info(f"Initializing Vertex AI Client: project={project}, location={location}")
        _genai_client = genai.Client(
            http_options=HttpOptions(api_version="v1"),
            project=project,
            location=location,
            vertexai=True,
        )
        logger.info("GenAI Client initialized (Vertex mode)")
        return _genai_client
    except Exception as e:
        logger.error(f"GenAI Client init failed (Vertex mode): {e}")
        return None


def normalize_fallback_message(value: Optional[str]) -> str:
    text = str(value or "").strip()
    return text or DEFAULT_FALLBACK_MESSAGE


def build_business_system_prompt(
    business_name: Optional[str] = None,
    handoff_message: Optional[str] = None,
    extra_instructions: Optional[str] = None,
) -> str:
    """Build a business-scoped system prompt with identity guardrails."""
    name = (business_name or "our business").strip() or "our business"
    handoff = normalize_fallback_message(handoff_message)
    prompt = DEFAULT_SYSTEM_PROMPT_TEMPLATE.format(
        business_name=name,
        handoff_message=handoff,
    )
    if extra_instructions and extra_instructions.strip():
        prompt += f"\n\nADDITIONAL BUSINESS INSTRUCTIONS:\n{extra_instructions.strip()}"
    return prompt


def _should_skip_rag_for_message(message: str) -> bool:
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
    tokens = [t for t in (message or "").split() if t.strip()]
    if len(tokens) <= 3 and len((message or "").strip()) < 64:
        short_floor = float(os.getenv("WHATSAPP_RAG_SHORT_QUERY_THRESHOLD", "0.20"))
        return min(configured, short_floor)
    return configured


def _build_fast_chitchat_reply(message: str, business_name: Optional[str] = None) -> Optional[str]:
    text = (message or "").strip()
    if not text or len(text) > 48 or "?" in text:
        return None
    normalized = re.sub(r"\s+", " ", text.lower()).strip(" !?.,")
    if not normalized:
        return None
    name = (business_name or "us").strip() or "us"
    if normalized in {"thanks", "thank", "thankyou", "ty", "thx", "dhanyawad", "shukriya"}:
        return f"You're welcome! Feel free to message {name} anytime if you need help."
    if normalized in {"ok", "okay", "sure", "fine", "cool"}:
        return "Sure! Let us know if you need anything else."
    if normalized in {"bye", "goodbye", "see you"}:
        return f"Thank you for contacting {name}. Have a great day!"
    return _build_fast_greeting_reply(message, business_name)


def _build_fast_greeting_reply(message: str, business_name: Optional[str] = None) -> Optional[str]:
    text = (message or "").strip()
    if not text or len(text) > 48 or "?" in text:
        return None
    normalized = re.sub(r"\s+", " ", text.lower()).strip(" !?.,")
    if not normalized:
        return None
    name = (business_name or "our team").strip() or "our team"
    if normalized in {"hi", "hello", "hey", "namaste", "hey there", "hi there", "good morning", "good afternoon", "good evening"}:
        return f"Hello! Welcome to {name}. How can I help you today?"
    words = normalized.split()
    if len(words) <= 2 and words and words[0] in {"hi", "hello", "hey", "namaste"}:
        return f"Hello! Welcome to {name}. How can I help you today?"
    return None


def message_indicates_no_knowledge(text: str) -> bool:
    normalized = (text or "").strip().lower()
    if not normalized:
        return False
    for pattern in _NO_KNOWLEDGE_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def is_off_topic_request(message: str) -> bool:
    """True when the user asks for non-business help (coding, homework, etc.)."""
    normalized = (message or "").strip().lower()
    if not normalized:
        return False
    for pattern in _OFF_TOPIC_PATTERNS:
        if re.search(pattern, normalized):
            return True
    return False


def _response_contains_unsupported_code(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    for pattern in _CODE_RESPONSE_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE | re.MULTILINE):
            return True
    return False


def _sanitize_identity_leaks(text: str, business_name: Optional[str] = None) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned
    lower = cleaned.lower()
    for pattern in _IDENTITY_LEAK_PATTERNS:
        if re.search(pattern, lower):
            return normalize_fallback_message(None)
    if message_indicates_no_knowledge(cleaned):
        return normalize_fallback_message(None)
    if _response_contains_unsupported_code(cleaned):
        return normalize_fallback_message(None)
    return cleaned


# Backward-compatible alias used by ai_routes defaults
DEFAULT_SYSTEM_PROMPT = build_business_system_prompt()

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
        }


@dataclass
class AIConfig:
    """AI configuration for an account."""
    enabled: bool = False
    system_prompt: str = ""
    business_name: Optional[str] = None
    model: str = DEFAULT_MODEL
    max_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    temperature: float = 0.3
    fallback_message: str = DEFAULT_FALLBACK_MESSAGE
    context_messages: int = 5
    # RAG Configuration
    use_rag: bool = True
    rag_top_k: int = RAG_TOP_K
    rag_confidence_threshold: float = RAG_CONFIDENCE_THRESHOLD
    workspace_id: Optional[str] = None

    def __post_init__(self):
        self.model = _resolve_generation_model(self.model)
        self.fallback_message = normalize_fallback_message(self.fallback_message)
        if not self.system_prompt:
            self.system_prompt = build_business_system_prompt(
                self.business_name,
                self.fallback_message,
            )


# ============================================================
# Data Classes (continued)
# ============================================================


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
            model=_resolve_generation_model(model_name),
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
        # Use cloud retrieval with context window
        results, stats = rag.retrieve_with_context_window(
            query=query,
            workspace_id=int(workspace_id),
            top_k=top_k,
            score_threshold=threshold,
        )

        if not results:
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
                    stats = {**(stats or {}), **(retry_stats or {}), "fallback_used": True}
        
        if not results:
            return [], False
        
        # Log for debugging
        logger.info(f"RAG retrieved {len(results)} chunks for workspace_id={workspace_id}, timing={stats}")
        
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
    for i, c in enumerate(rag_chunks):
        text = c.get('text', '')
        source = c.get('source', 'Unknown')
        score = c.get('score', 0)
        if text:
            # Only include top-scoring chunks for cleaner context
            if score >= 0.2:
                context_parts.append(f"[{source}]:\n{text}")
    
    # Limit context to avoid overwhelming the model
    context_str = "\n---\n".join(context_parts[:5])
    handoff = normalize_fallback_message(None)
    
    return f"""### BUSINESS KNOWLEDGE (answer ONLY from this — nothing else):
{context_str}

### CUSTOMER QUESTION: {user_message}

### STRICT RULES:
1. Answer ONLY from the business knowledge above
2. Do NOT use general knowledge or guess
3. Do NOT write code, programs, or partial code snippets
4. Give a COMPLETE answer — include all relevant details from the knowledge; never cut off mid-sentence
5. If the answer is NOT fully supported by the knowledge above, reply EXACTLY:
   "{handoff}"
6. Plain text only — no markdown, no emojis
7. Match the customer's language (English, Hindi, Hinglish, Telugu, Tinglish)
8. Never mention AI, chatbots, Gemini, Google, or Sociovia

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
    
    def generate_response(self, message: str, context: Optional[List[Dict[str, str]]] = None) -> ChatResponse:
        """
        Generate AI response with RAG integration. FAIL-SAFE.

        Guardrails:
        - Acts as a business representative (never reveals AI/Gemini/Sociovia identity)
        - Does NOT call Gemini when knowledge base has no confident match (handoff instead)
        - Sanitizes replies that admit missing knowledge or leak underlying identity
        """
        import time
        start_time = time.time()
        handoff_message = normalize_fallback_message(self.config.fallback_message)
        business_name = self.config.business_name

        if not self.is_available():
            return ChatResponse(
                message=handoff_message,
                success=False,
                error=self._init_error,
            )

        try:
            greeting_reply = _build_fast_chitchat_reply(message, business_name)
            if greeting_reply:
                elapsed_ms = int((time.time() - start_time) * 1000)
                return ChatResponse(
                    message=greeting_reply,
                    success=True,
                    response_time_ms=elapsed_ms,
                    model_used=self.config.model,
                )

            if is_off_topic_request(message):
                elapsed_ms = int((time.time() - start_time) * 1000)
                return ChatResponse(
                    message=handoff_message,
                    success=True,
                    response_time_ms=elapsed_ms,
                    error="Off-topic request — business assistant scope only",
                )

            rag_chunks: List[Dict] = []
            high_conf = False
            max_score = 0.0

            if not self.config.use_rag:
                elapsed_ms = int((time.time() - start_time) * 1000)
                return ChatResponse(
                    message=handoff_message,
                    success=True,
                    response_time_ms=elapsed_ms,
                    error="RAG disabled — business assistant requires knowledge base",
                )

            if self.config.use_rag and self.config.workspace_id:
                retrieval_threshold = _effective_rag_threshold(
                    message, self.config.rag_confidence_threshold
                )
                rag_chunks, high_conf = get_rag_context(
                    query=message,
                    workspace_id=int(self.config.workspace_id),
                    top_k=self.config.rag_top_k,
                    threshold=retrieval_threshold,
                )
                if rag_chunks:
                    max_score = max(c.get("score", 0) for c in rag_chunks)
                    high_conf = max_score >= self.config.rag_confidence_threshold

                logger.info(
                    "RAG result: chunks=%d high_conf=%s max_score=%.3f threshold=%s",
                    len(rag_chunks),
                    high_conf,
                    max_score,
                    retrieval_threshold,
                )

                if not high_conf:
                    elapsed_ms = int((time.time() - start_time) * 1000)
                    return ChatResponse(
                        message=handoff_message,
                        success=True,
                        response_time_ms=elapsed_ms,
                        used_rag=bool(rag_chunks),
                        rag_chunks=len(rag_chunks),
                        error=f"RAG confidence {max_score:.3f} below threshold {self.config.rag_confidence_threshold}",
                    )
            else:
                elapsed_ms = int((time.time() - start_time) * 1000)
                return ChatResponse(
                    message=handoff_message,
                    success=True,
                    response_time_ms=elapsed_ms,
                    error="No workspace configured for knowledge lookup",
                )

            enhanced_message = build_rag_enhanced_message(message, rag_chunks)

            base_prompt = build_business_system_prompt(business_name, handoff_message)
            custom_prompt = (self.config.system_prompt or "").strip()
            extra_instructions = custom_prompt if custom_prompt and custom_prompt != base_prompt else None
            effective_system_prompt = build_business_system_prompt(
                business_name=business_name,
                handoff_message=handoff_message,
                extra_instructions=extra_instructions,
            )

            response = self.client.models.generate_content(
                model=self.config.model,
                contents=enhanced_message,
                config=GenerateContentConfig(
                    max_output_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    system_instruction=effective_system_prompt,
                ),
            )

            response_text = self._clean_response(response.text.strip()) if response.text else ""
            response_text = _sanitize_identity_leaks(response_text, business_name)

            if not response_text or len(response_text) < 5:
                return ChatResponse(
                    message=handoff_message,
                    success=False,
                    error="Empty response from AI",
                )

            if len(response_text) > MAX_RESPONSE_CHARS:
                response_text = response_text[: MAX_RESPONSE_CHARS - 3] + "..."

            elapsed_ms = int((time.time() - start_time) * 1000)
            return ChatResponse(
                message=response_text,
                success=True,
                model_used=self.config.model,
                response_time_ms=elapsed_ms,
                used_rag=len(rag_chunks) > 0,
                rag_chunks=len(rag_chunks),
            )

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.exception(f"AI response failed: {e}")
            return ChatResponse(
                message=handoff_message,
                success=False,
                error=str(e),
                response_time_ms=elapsed_ms,
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
        business_name = config_dict.get("business_name")
        fallback_message = normalize_fallback_message(config_dict.get("fallback_message"))
        custom_prompt = (config_dict.get("system_prompt") or "").strip()
        config = AIConfig(
            enabled=config_dict.get("enabled", False),
            system_prompt=custom_prompt,
            business_name=business_name,
            model=_resolve_generation_model(config_dict.get("model", DEFAULT_MODEL)),
            max_tokens=config_dict.get("max_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
            temperature=float(config_dict.get("temperature", 0.3)),
            fallback_message=fallback_message,
            context_messages=config_dict.get("context_messages", 5),
            use_rag=config_dict.get("use_rag", True),
            rag_top_k=config_dict.get("rag_top_k", RAG_TOP_K),
            rag_confidence_threshold=float(config_dict.get("rag_confidence_threshold", RAG_CONFIDENCE_THRESHOLD)),
            workspace_id=config_dict.get("workspace_id"),
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
    business_name: Optional[str] = None,
    max_tokens: Optional[int] = None,
) -> ChatResponse:
    """Convenience function to generate AI response."""
    handoff = normalize_fallback_message(fallback_message)
    config = AIConfig(
        enabled=True,
        system_prompt=system_prompt or build_business_system_prompt(business_name, handoff),
        business_name=business_name,
        fallback_message=handoff,
        use_rag=use_rag,
        workspace_id=workspace_id,
        max_tokens=int(max_tokens) if max_tokens else DEFAULT_MAX_OUTPUT_TOKENS,
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
