"""
AI Lead-Status Classifier (Phase 3) - WhatsApp -> CRM
=====================================================

Reads an inbound WhatsApp message and uses an LLM to classify the lead's buying
intent into a CRM status. This is the AI counterpart to the deterministic
keyword matcher in SocioviaCrm/qualify_keywords.py.

Key facts:
    - MODEL: Google Gemini "gemini-3.1-flash-lite" (override via env
      GEMINI_STATUS_MODEL). Calls go through the SAME reusable client used by the
      WhatsApp chatbot: whatsapp.ai_chatbot.get_genai_client().
    - EDITABLE PROMPT: each workspace may override the classification prompt and
      toggle the feature on/off. Both are stored as CRM Setting rows
      (current_app.crm_models["Setting"], scoped by workspace_id + name), exactly
      like qualify_keywords.py:
          * crm_ai_status_enabled -> "1" / "0"
          * crm_ai_status_prompt   -> the prompt text (must contain "{message}")
    - PROMPT GUARDRAIL: the user's editable prompt is always suffixed with a
      FIXED instruction that forces a single-word answer. This means a workspace
      can reword the prompt freely without breaking output parsing.
    - FAIL-SAFE: every function is wrapped in try/except and returns a safe
      default. classify_lead_status() runs on the webhook hot path and must NEVER
      raise. On any error (no client, bad config, API failure) it returns None.
    - FORWARD-ONLY: this module only ever proposes "contacted" or "qualified"
      (never "new"). Enforcing that a lead is not moved BACKWARDS is the caller's
      responsibility - this module just classifies.

Public names below are a CONTRACT other modules import - do not rename.
"""

import os
import logging

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Contract constants
# ------------------------------------------------------------------

# Gemini model used for status classification. Flash-lite is cheap/fast - good
# for a one-word classification on the webhook hot path.
MODEL = os.getenv("GEMINI_STATUS_MODEL", "gemini-3.1-flash-lite")

# CRM Setting row names (scoped per workspace, mirrors qualify_keywords.py).
AI_ENABLED_SETTING = "crm_ai_status_enabled"   # value: "1" / "0"
AI_PROMPT_SETTING = "crm_ai_status_prompt"     # value: prompt text

# Statuses the AI is allowed to assign. It never assigns "new".
VALID_STATUSES = ["contacted", "qualified"]

# Default editable prompt. MUST contain the literal "{message}" placeholder so
# the inbound text can be substituted in. Kept short on purpose.
DEFAULT_AI_PROMPT = (
    "You classify a customer's buying intent for a sales CRM based on their "
    "WhatsApp message. Reply with ONE word only: 'qualified' if they show buying "
    "intent (asking price, wanting to buy/order/book, ready to pay), otherwise "
    "'contacted'. Message: {message}"
)

# Fixed guardrail appended to the (possibly edited) prompt before every call.
# A workspace can reword DEFAULT_AI_PROMPT however they like; this suffix still
# pins the output format so parsing can't be broken by prompt edits.
_PROMPT_FORMAT_SUFFIX = "\n\nRespond with exactly one word: qualified or contacted."

# Max characters of the inbound message we feed to the model.
_MAX_MESSAGE_CHARS = 500

# Small local set of greetings / acknowledgements to skip BEFORE any AI call
# (same idea as ai_chatbot._RAG_SKIP_CHITCHAT). These carry no buying intent, so
# there is no point spending an API call on them.
_SKIP_CHITCHAT = frozenset({
    "hi", "hii", "hiii", "hello", "helo", "hey", "heya", "hola", "namaste",
    "ok", "okay", "okk", "k", "kk", "yes", "yep", "yeah", "ya", "no", "nope",
    "thanks", "thank", "thankyou", "ty", "thx", "tq", "thnx",
    "bye", "goodbye", "cya", "good", "fine", "cool", "great", "nice", "sure",
    "morning", "evening", "afternoon",
    "\U0001f44d", "\U0001f64f", "\U0001f60a", "\U0001f44c",  # 👍 🙏 😊 👌
})


# ------------------------------------------------------------------
# Workspace-id matching (mirrors qualify_keywords.py)
# ------------------------------------------------------------------

def _coerce_workspace_id_candidates(workspace_id):
    """
    Return candidate values to match Setting.workspace_id against. The column is
    an Integer but the contract passes str; try int first, then str, then raw.
    Never raises.
    """
    candidates = []
    try:
        candidates.append(int(workspace_id))
    except (TypeError, ValueError):
        pass
    s = str(workspace_id)
    if s not in (str(c) for c in candidates):
        candidates.append(s)
    if workspace_id not in candidates:
        candidates.append(workspace_id)
    return candidates


def _find_setting_row(session, Setting, workspace_id, name):
    """
    Locate the Setting row for (workspace_id, name). Returns the row or None.
    Tries each workspace_id candidate form. Never raises.
    """
    for candidate in _coerce_workspace_id_candidates(workspace_id):
        try:
            row = (
                session.query(Setting)
                .filter(
                    Setting.workspace_id == candidate,
                    Setting.name == name,
                )
                .first()
            )
        except Exception:
            # A type-mismatch on one candidate must not abort the others.
            try:
                session.rollback()
            except Exception:
                pass
            row = None
        if row is not None:
            return row
    return None


def _sanitize_prompt(prompt):
    """
    Coerce a prompt to a clean string. Falls back to DEFAULT_AI_PROMPT when the
    value is missing/empty. Never raises.
    """
    try:
        if prompt is None:
            return DEFAULT_AI_PROMPT
        text = str(prompt).strip()
        return text or DEFAULT_AI_PROMPT
    except Exception:
        return DEFAULT_AI_PROMPT


# ------------------------------------------------------------------
# Config read / write
# ------------------------------------------------------------------

def get_ai_status_config(workspace_id):
    """
    Return the AI status-classifier config for a workspace.

    Shape:
        {
            "enabled": bool,            # from crm_ai_status_enabled ("1"/"0")
            "prompt": str,              # from crm_ai_status_prompt, else default
            "default_prompt": str,      # always DEFAULT_AI_PROMPT (for the UI)
        }

    Defaults: enabled=False, prompt=DEFAULT_AI_PROMPT. Never raises.
    """
    from flask import current_app

    result = {
        "enabled": False,
        "prompt": DEFAULT_AI_PROMPT,
        "default_prompt": DEFAULT_AI_PROMPT,
    }

    try:
        Setting = None
        try:
            Setting = current_app.crm_models["Setting"]
        except Exception:
            Setting = None
        if Setting is None:
            logger.warning("get_ai_status_config: Setting model not configured")
            return result

        if workspace_id is None:
            return result

        session = current_app.db.session

        # enabled flag
        enabled_row = _find_setting_row(session, Setting, workspace_id, AI_ENABLED_SETTING)
        if enabled_row is not None:
            raw_enabled = getattr(enabled_row, "value", None)
            try:
                result["enabled"] = str(raw_enabled).strip() == "1"
            except Exception:
                result["enabled"] = False

        # prompt text
        prompt_row = _find_setting_row(session, Setting, workspace_id, AI_PROMPT_SETTING)
        if prompt_row is not None:
            raw_prompt = getattr(prompt_row, "value", None)
            if raw_prompt is not None and str(raw_prompt).strip():
                result["prompt"] = str(raw_prompt).strip()

        return result

    except Exception:
        logger.exception("get_ai_status_config: unexpected error")
        return result


def _upsert_setting(session, Setting, workspace_id, name, value):
    """
    Create or update the Setting row (workspace_id, name) with `value`. Caller is
    responsible for commit. Returns True on success, False otherwise. Never raises.
    """
    try:
        row = _find_setting_row(session, Setting, workspace_id, name)
        if row is not None:
            row.value = value
            session.add(row)
        else:
            ws_value = workspace_id
            try:
                ws_value = int(workspace_id)
            except (TypeError, ValueError):
                ws_value = str(workspace_id)
            row = Setting(workspace_id=ws_value, name=name, value=value)
            session.add(row)
        return True
    except Exception:
        logger.exception("_upsert_setting: failed for %s", name)
        return False


def set_ai_status_config(workspace_id, enabled=None, prompt=None):
    """
    Upsert the per-workspace AI status-classifier config.

    Only the provided fields are written:
        - enabled (bool-ish) -> crm_ai_status_enabled as "1"/"0"
        - prompt  (str)      -> crm_ai_status_prompt (sanitized; empty -> default)

    Returns the same dict shape as get_ai_status_config (re-read after commit).
    Never raises (rolls back and returns current/default config on error).
    """
    from flask import current_app

    try:
        Setting = None
        try:
            Setting = current_app.crm_models["Setting"]
        except Exception:
            Setting = None
        if Setting is None:
            logger.warning("set_ai_status_config: Setting model not configured")
            return get_ai_status_config(workspace_id)

        if workspace_id is None:
            logger.info("set_ai_status_config: no workspace_id, skipping")
            return get_ai_status_config(workspace_id)

        session = current_app.db.session

        wrote_any = False

        if enabled is not None:
            try:
                enabled_value = "1" if bool(enabled) else "0"
            except Exception:
                enabled_value = "0"
            if _upsert_setting(session, Setting, workspace_id, AI_ENABLED_SETTING, enabled_value):
                wrote_any = True

        if prompt is not None:
            prompt_value = _sanitize_prompt(prompt)
            if _upsert_setting(session, Setting, workspace_id, AI_PROMPT_SETTING, prompt_value):
                wrote_any = True

        if wrote_any:
            try:
                session.commit()
            except Exception:
                logger.exception("set_ai_status_config: commit failed")
                try:
                    session.rollback()
                except Exception:
                    pass

        return get_ai_status_config(workspace_id)

    except Exception:
        logger.exception("set_ai_status_config: unexpected error")
        try:
            current_app.db.session.rollback()
        except Exception:
            pass
        return get_ai_status_config(workspace_id)


# ------------------------------------------------------------------
# Chit-chat skip
# ------------------------------------------------------------------

def _should_skip_for_classification(text):
    """
    True when `text` is empty, pure chit-chat/acknowledgement, or too short to
    carry buying intent - in which case we skip the AI call entirely. Never raises.
    """
    try:
        s = (text or "").strip()
        if not s:
            return True

        # Tokenize, stripping common punctuation/emoji-adjacent chars.
        tokens = [t.strip(".,!?;:\"'") .lower() for t in s.split()]
        tokens = [t for t in tokens if t]
        if not tokens:
            return True

        # Short message made up entirely of greetings/acks -> skip.
        if len(tokens) <= 2 and all(t in _SKIP_CHITCHAT for t in tokens):
            return True

        # Very short non-alphabetic message (e.g. an emoji or "??") -> skip.
        if len(s) <= 3 and not any(ch.isalpha() for ch in s):
            return True

        return False
    except Exception:
        # If we can't decide, do NOT skip - let the AI try (still fail-safe).
        return False


# ------------------------------------------------------------------
# Classification
# ------------------------------------------------------------------

def classify_lead_status(text, workspace_id):
    """
    Classify the lead status implied by an inbound WhatsApp message.

    Returns one of VALID_STATUSES ("contacted" / "qualified"), or None when:
        - text is empty,
        - the feature is disabled for the workspace,
        - the message is obvious chit-chat (no AI call made),
        - the GenAI client is unavailable,
        - the model errors or returns an unparseable / invalid answer.

    FAIL-SAFE: wraps everything in try/except and returns None on any error.
    NEVER raises - this runs on the webhook hot path.
    """
    try:
        if not text or not str(text).strip():
            return None

        config = get_ai_status_config(workspace_id)
        if not config.get("enabled"):
            return None

        # Cheap local filter before spending an API call.
        if _should_skip_for_classification(text):
            return None

        # Lazy import keeps this module importable even if the chatbot module or
        # google-genai is unavailable at import time.
        try:
            from whatsapp.ai_chatbot import get_genai_client
        except Exception as e:
            logger.warning("classify_lead_status: could not import get_genai_client: %s", e)
            return None

        client = get_genai_client()
        if client is None:
            logger.debug("classify_lead_status: GenAI client unavailable")
            return None

        message = str(text)[:_MAX_MESSAGE_CHARS]

        base_prompt = _sanitize_prompt(config.get("prompt"))
        # Substitute the message. If the (edited) prompt dropped the placeholder,
        # the message still reaches the model via the appended guardrail context.
        if "{message}" in base_prompt:
            filled = base_prompt.replace("{message}", message)
        else:
            filled = base_prompt + "\n\nMessage: " + message
        prompt = filled + _PROMPT_FORMAT_SUFFIX

        try:
            from google.genai.types import GenerateContentConfig

            from core.genai_bridge import generate_text
            response = generate_text(
                model=MODEL,
                contents=prompt,
                config=GenerateContentConfig(
                    max_output_tokens=8,
                    temperature=0.0,
                ),
                gemini_client=client,
                workspace_id=workspace_id,
                feature="lead_status_classify",
            )

            # --- AI usage metering (fail-soft) ---
            # Only workspace_id is available here; resolve the owner user id via
            # resolve_workspace_owner. No-ops if the workspace can't be resolved.
            try:
                from subscription.service import record_ai_usage, resolve_workspace_owner
                _meter_uid, _meter_wid = resolve_workspace_owner(workspace_id)
                record_ai_usage(
                    _meter_uid,
                    _meter_wid,
                    feature="crm_lead_status",
                    model=MODEL,
                    _commit=True,
                )
            except Exception:
                pass
            # --- end metering ---
        except Exception as e:
            logger.warning("classify_lead_status: generate_content failed: %s", e)
            return None

        raw = getattr(response, "text", None)
        if not raw:
            return None

        answer = str(raw).strip().lower()

        # Find which valid status the model emitted. Check "qualified" first so a
        # response mentioning both leans to the higher-intent label.
        for status in ("qualified", "contacted"):
            if status in VALID_STATUSES and status in answer:
                return status

        logger.debug("classify_lead_status: unrecognized answer %r", answer)
        return None

    except Exception:
        logger.warning("classify_lead_status: unexpected error", exc_info=True)
        return None
