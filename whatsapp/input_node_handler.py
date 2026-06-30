"""
Input Node Handler
===================

Processes free-text user input during interactive automation flows.

Responsibilities:
- Validate input against node-configured rules (text, number, email, phone, regex, enum, pincode)
- Extract clean values from natural language ("My name is Prabhu" → "Prabhu")
- Detect correction intent ("sorry 22", "actually it's 23")
- Substitute {{variables}} in downstream messages
- Store collected fields in conversation state

Design:
- Stateless functions — all state is in WhatsAppConversationState.state_data
- No database access — pure input processing logic
- Called from InteractiveAutomationEngine._handle_flow_continuation()
"""

import logging
import re
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Correction Detection ───────────────────────────────────────────

_CORRECTION_PREFIXES = (
    "sorry",
    "actually",
    "change",
    "no ",
    "no,",
    "wait",
    "correction",
    "correct ",
    "i meant",
    "i mean",
    "not that",
    "wrong",
    "oops",
    "my bad",
)

_NAME_PREFIXES = (
    "my name is",
    "i am",
    "i'm",
    "im",
    "it's",
    "its",
    "name is",
    "call me",
    "they call me",
    "name:",
)

_AGE_PREFIXES = (
    "i am",
    "i'm",
    "im",
    "my age is",
    "age is",
    "age:",
    "years old",
    "year old",
)

_SKIP_KEYWORDS = {"skip", "next", "pass", "na", "n/a", "-"}


def is_correction_message(text: str) -> bool:
    """Detect if the user is correcting a previous answer."""
    normalized = text.strip().lower()
    return any(normalized.startswith(prefix) for prefix in _CORRECTION_PREFIXES)


def extract_correction_value(text: str) -> str:
    """Strip correction prefix to get the actual corrected value."""
    normalized = text.strip()
    lower = normalized.lower()

    for prefix in _CORRECTION_PREFIXES:
        if lower.startswith(prefix):
            remainder = normalized[len(prefix):].strip()
            # Strip trailing punctuation and connectors
            remainder = re.sub(r"^[,.:;!\-–—]+\s*", "", remainder)
            # Handle "sorry it's X" / "actually it is X"
            remainder = re.sub(r"^(?:it'?s|it is|its)\s+", "", remainder, flags=re.IGNORECASE)
            if remainder:
                return remainder

    return normalized


# ── Smart Value Extraction ─────────────────────────────────────────

def extract_field_value(text: str, field_type: str) -> str:
    """
    Extract clean value from natural language input.

    Examples:
        "My name is Prabhu" → "Prabhu"
        "I am 21 years old" → "21"
        "It's male" → "male"
    """
    stripped = text.strip()

    if field_type in ("name", "text"):
        lower = stripped.lower()
        for prefix in _NAME_PREFIXES:
            if lower.startswith(prefix):
                remainder = stripped[len(prefix):].strip()
                # Remove trailing punctuation
                remainder = re.sub(r"^[,.:;!\-–—]+\s*", "", remainder)
                if remainder:
                    return remainder
                # Prefix matched but nothing after it (split message case)
                # Return empty so validation fails and re-asks
                return ""
        return stripped

    if field_type in ("number", "age"):
        lower = stripped.lower()
        for prefix in _AGE_PREFIXES:
            if lower.startswith(prefix):
                remainder = stripped[len(prefix):].strip()
                remainder = re.sub(r"^[,.:;!\-–—]+\s*", "", remainder)
                if remainder:
                    stripped = remainder
                    break
        # Remove trailing "years old" etc.
        stripped = re.sub(r"\s*(?:years?\s*old|yrs?\s*old?|yr)\s*$", "", stripped, flags=re.IGNORECASE)
        # Try to extract just the number
        match = re.search(r"\d+\.?\d*", stripped)
        if match:
            return match.group(0)
        return stripped

    return stripped


# ── Validation ─────────────────────────────────────────────────────

def validate_input(
    text: str,
    validation_config: Dict[str, Any],
) -> Tuple[bool, Any, Optional[str]]:
    """
    Validate user input against node configuration.

    Args:
        text: Raw user message text
        validation_config: Node's validation rules
            {
                "type": "text|number|email|phone|regex|enum|pincode",
                "min_length": int,
                "max_length": int,
                "min_value": number,
                "max_value": number,
                "pattern": str (for regex type),
                "options": list (for enum type),
                "error_message": str (custom error)
            }

    Returns:
        (is_valid, cleaned_value, error_message)
    """
    if not text or not text.strip():
        error = validation_config.get("error_message", "This field is required. Please provide a valid answer.")
        return False, None, error

    val_type = validation_config.get("type", "text")
    cleaned = text.strip()

    if val_type == "text":
        return _validate_text(cleaned, validation_config)
    elif val_type in ("number", "age"):
        return _validate_number(cleaned, validation_config)
    elif val_type == "email":
        return _validate_email(cleaned, validation_config)
    elif val_type == "phone":
        return _validate_phone(cleaned, validation_config)
    elif val_type == "regex":
        return _validate_regex(cleaned, validation_config)
    elif val_type == "enum":
        return _validate_enum(cleaned, validation_config)
    elif val_type == "pincode":
        return _validate_pincode(cleaned, validation_config)
    else:
        # Unknown type — accept as text
        return _validate_text(cleaned, validation_config)


def _validate_text(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    min_len = config.get("min_length", 1)
    max_len = config.get("max_length", 500)
    error = config.get("error_message")

    if len(text) < min_len:
        return False, text, error or f"Please enter at least {min_len} characters."
    if len(text) > max_len:
        return False, text, error or f"Please keep your answer under {max_len} characters."

    return True, text, None


def _validate_number(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, Any, Optional[str]]:
    error = config.get("error_message")

    # Strip non-numeric noise
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned:
        return False, text, error or "Please enter a valid number."

    try:
        if "." in cleaned:
            value = float(cleaned)
        else:
            value = int(cleaned)
    except (ValueError, OverflowError):
        return False, text, error or "Please enter a valid number."

    min_val = config.get("min_value")
    max_val = config.get("max_value")

    if min_val is not None and value < min_val:
        return False, text, error or f"Please enter a number of at least {min_val}."
    if max_val is not None and value > max_val:
        return False, text, error or f"Please enter a number no more than {max_val}."

    return True, value, None


def _validate_email(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    error = config.get("error_message")
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if re.match(pattern, text.strip()):
        return True, text.strip().lower(), None
    return False, text, error or "Please enter a valid email address (e.g. name@example.com)."


def _validate_phone(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    error = config.get("error_message")
    digits = re.sub(r"[^\d]", "", text)
    min_len = config.get("min_length", 10)
    max_len = config.get("max_length", 15)

    if len(digits) < min_len or len(digits) > max_len:
        return False, text, error or f"Please enter a valid phone number ({min_len}-{max_len} digits)."

    return True, digits, None


def _validate_regex(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    error = config.get("error_message", "Input does not match the expected format.")
    pattern = config.get("pattern")
    if not pattern:
        return True, text, None

    try:
        if re.match(pattern, text):
            return True, text, None
    except re.error:
        logger.warning(f"[input_handler] Invalid regex pattern: {pattern}")
        return True, text, None

    return False, text, error


def _validate_enum(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    error = config.get("error_message")
    options = config.get("options", [])
    if not options:
        return True, text, None

    normalized = text.strip().lower()

    # Exact match
    for option in options:
        if str(option).strip().lower() == normalized:
            return True, str(option).strip(), None

    # Numeric selection ("1", "2", "3")
    if normalized.isdigit():
        idx = int(normalized) - 1
        if 0 <= idx < len(options):
            return True, str(options[idx]).strip(), None

    # Fuzzy match (threshold 0.75)
    best_match = None
    best_score = 0.0
    for option in options:
        score = SequenceMatcher(None, normalized, str(option).strip().lower()).ratio()
        if score > best_score:
            best_score = score
            best_match = str(option).strip()

    if best_match and best_score >= 0.75:
        return True, best_match, None

    options_display = ", ".join(str(o) for o in options)
    return False, text, error or f"Please choose one of: {options_display}"


def _validate_pincode(
    text: str, config: Dict[str, Any]
) -> Tuple[bool, str, Optional[str]]:
    error = config.get("error_message")
    digits = re.sub(r"[^\d]", "", text)
    expected_length = config.get("length", 6)

    if len(digits) == expected_length:
        return True, digits, None

    return False, text, error or f"Please enter a valid {expected_length}-digit pincode."


# ── Variable Substitution ──────────────────────────────────────────

_VAR_PATTERN = re.compile(r"\{\{(\w+)\}\}")


def substitute_variables(text: str, collected_fields: Dict[str, Any]) -> str:
    """
    Replace {{field_name}} placeholders with collected values.

    Example:
        text = "Thanks {{name}}, your age is {{age}}"
        collected = {"name": "Prabhu", "age": 21}
        → "Thanks Prabhu, your age is 21"
    """
    if not text or not collected_fields:
        return text or ""

    def _replacer(match):
        field = match.group(1)
        value = collected_fields.get(field)
        if value is not None:
            return str(value)
        return match.group(0)  # Keep original if not found

    return _VAR_PATTERN.sub(_replacer, text)


# ── Question Builder ───────────────────────────────────────────────

def build_question_message(
    node_data: Dict[str, Any],
    collected_fields: Dict[str, Any],
    error_message: Optional[str] = None,
) -> str:
    """
    Build the question message to send to the user.

    Supports variable substitution and optional error prefix.
    """
    question = node_data.get("body", "Please provide your answer:")
    question = substitute_variables(question, collected_fields)

    val_type = node_data.get("validationType", "text")

    parts = []

    if error_message:
        err = (error_message or "").strip()
        if err:
            # Single outbound bubble: error first, then prompt (no extra prefix —
            # put emoji in the node's custom error text if you want it).
            parts.append(f"{err}\n\n")

    parts.append(question)

    # For enum, show options
    if val_type == "enum":
        options = node_data.get("enumValues", [])
        if options:
            parts.append("")
            for idx, option in enumerate(options, 1):
                parts.append(f"{idx}. {option}")

    # Show skip hint if allowed
    skip_keyword = node_data.get("skipKeyword") or node_data.get("skip_keyword")
    required = node_data.get("required", True)
    if skip_keyword and not required:
        parts.append(f"\n💡 Type \"{skip_keyword}\" to skip this question.")

    return "\n".join(parts)


# ── Process Input Response (Main Entry Point) ──────────────────────

def process_input_response(
    message_text: str,
    node_data: Dict[str, Any],
    state,
) -> Dict[str, Any]:
    """
    Process a user's text response to an input node question.

    This is the main entry point called from the engine.

    Args:
        message_text: Raw user message
        node_data: The input node's data configuration
        state: WhatsAppConversationState instance

    Returns:
        {
            "valid": bool,
            "value": any,            # Cleaned value (if valid)
            "error_message": str,    # Error text (if invalid)
            "is_correction": bool,   # True if user corrected previous answer
            "corrected_field": str,  # Field that was corrected (if correction)
            "is_skip": bool,         # True if user skipped
        }
    """
    field = node_data.get("field", "unknown")
    val_type = node_data.get("validationType", "text")
    skip_keyword = node_data.get("skipKeyword") or node_data.get("skip_keyword")
    required = node_data.get("required", True)

    validation_config = {"type": val_type}
    min_len = node_data.get("minLength")
    if min_len is None:
        min_len = node_data.get("min_length")
    if min_len is not None:
        validation_config["min_length"] = min_len

    max_len = node_data.get("maxLength")
    if max_len is None:
        max_len = node_data.get("max_length")
    if max_len is not None:
        validation_config["max_length"] = max_len

    min_val = node_data.get("minValue")
    if min_val is None:
        min_val = node_data.get("min_value")
    if min_val is not None:
        validation_config["min_value"] = min_val

    max_val = node_data.get("maxValue")
    if max_val is None:
        max_val = node_data.get("max_value")
    if max_val is not None:
        validation_config["max_value"] = max_val

    if node_data.get("pattern") is not None:
        validation_config["pattern"] = node_data.get("pattern")

    enum_opts = node_data.get("enumValues")
    if enum_opts is None:
        enum_opts = node_data.get("enum_values")
    if enum_opts is not None:
        validation_config["options"] = enum_opts

    err_msg = node_data.get("errorMessage")
    if err_msg is None:
        err_msg = node_data.get("error_message")
    if err_msg is not None:
        validation_config["error_message"] = err_msg

    text = (message_text or "").strip()

    # Check skip
    if skip_keyword and text.lower() in _SKIP_KEYWORDS | {skip_keyword.lower()}:
        if not required:
            state.set_collected_field(field, None)
            state.clear_waiting_for_input()
            logger.info(f"[input_handler] Field '{field}' skipped by user")
            return {
                "valid": True,
                "value": None,
                "is_skip": True,
                "is_correction": False,
            }
        else:
            return {
                "valid": False,
                "error_message": "This field is required and cannot be skipped.",
                "is_skip": False,
                "is_correction": False,
            }

    # Check correction intent
    if is_correction_message(text):
        corrected_value_text = extract_correction_value(text)
        last_field = state.get_last_collected_field()

        if last_field and corrected_value_text:
            # Get the last field's validation config — we need to re-validate.
            # For correction, we extract + validate the corrected value.
            extracted = extract_field_value(corrected_value_text, val_type)
            is_valid, cleaned, error = validate_input(extracted, validation_config)

            if is_valid:
                # Overwrite the CURRENT field with the corrected value,
                # because the correction applies to the question being asked.
                # But if the user is correcting the PREVIOUS field, handle that.
                state.set_collected_field(last_field, cleaned)
                logger.info(
                    f"[input_handler] Correction: field '{last_field}' updated to '{cleaned}'"
                )
                return {
                    "valid": True,
                    "value": cleaned,
                    "is_correction": True,
                    "corrected_field": last_field,
                    "is_skip": False,
                }
            else:
                return {
                    "valid": False,
                    "error_message": error or "Could not understand the correction. Please try again.",
                    "is_correction": True,
                    "corrected_field": last_field,
                    "is_skip": False,
                }

    # Normal input: extract → validate → store
    extracted = extract_field_value(text, val_type)
    is_valid, cleaned, error = validate_input(extracted, validation_config)

    if is_valid:
        state.set_collected_field(field, cleaned)
        if field == "location":
            pin_digits = re.sub(r"\s", "", str(cleaned))
            if re.fullmatch(r"\d{6}", pin_digits):
                state.set_collected_field("pincode", pin_digits)
        state.clear_waiting_for_input()
        logger.info(f"[input_handler] Field '{field}' = '{cleaned}'")
        return {
            "valid": True,
            "value": cleaned,
            "is_correction": False,
            "is_skip": False,
        }
    else:
        logger.info(f"[input_handler] Field '{field}' validation failed: {error}")
        return {
            "valid": False,
            "error_message": error,
            "is_correction": False,
            "is_skip": False,
        }
