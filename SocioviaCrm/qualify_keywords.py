"""
Qualify Keywords - real buying-intent keyword matching for WhatsApp -> CRM
=========================================================================

Detects genuine buying-intent words (human language, not machine ref-tags) in
inbound WhatsApp messages so a lead can be auto-advanced through a CRM status.

Per-keyword STATUS (Phase 2)
----------------------------
Every qualify keyword now carries a STATUS describing which CRM stage a match
should map the lead to. Valid statuses are VALID_STATUSES == ["new",
"contacted", "qualified"]. A bad or missing status always falls back to
"qualified" (the historical meaning of a keyword match).

A "rule" is the object shape every keyword is normalized to:
    {"keyword": <str>, "status": <one of VALID_STATUSES>}

Two keyword sources are combined:
    - DEFAULT_QUALIFY_KEYWORDS: a baseline list shipped with the product. Each
      default is treated as status "qualified" (see default_qualify_rules()).
    - Per-workspace custom keywords: stored as a JSON array of rule objects in
      the CRM Setting row (name == SETTING_NAME, scoped to the workspace).

Backward compatibility
-----------------------
Older data stored custom keywords as a JSON array of bare strings. When reading,
a bare string entry is upgraded to {"keyword": <str>, "status": "qualified"}, so
existing rows keep working unchanged. Likewise set_qualify_keywords tolerates
bare strings in the incoming list and treats them as status "qualified".

Design contract (mirrors SocioviaCrm/lead_ingest.py):
    - NEVER raises. Every public function is wrapped in try/except that logs and
      returns a safe default. A failure here must not break the webhook.
    - Models are resolved lazily via current_app.crm_models[...]; Flask is
      imported lazily inside each function.
    - Multi-tenant: workspace_id is compared/stored per the Setting model. It is
      coerced to str (the contract) but matched defensively against the actual
      stored column type as well.

Public names below are a CONTRACT other modules depend on - do not rename.
"""

import json
import logging
import re

logger = logging.getLogger(__name__)

# Human buying-intent words. These are matched whole-word, case-insensitively.
# Each default is treated as status "qualified" - see default_qualify_rules().
DEFAULT_QUALIFY_KEYWORDS = [
    "price",
    "cost",
    "how much",
    "buy",
    "purchase",
    "order",
    "interested",
    "quote",
    "book",
    "appointment",
    "demo",
    "available",
]

# CRM statuses a keyword may map a lead to. A bad/missing status -> "qualified".
VALID_STATUSES = ["new", "contacted", "qualified"]

# Default status applied to any keyword whose status is missing or invalid.
_DEFAULT_STATUS = "qualified"

# Name of the CRM Setting row holding the per-workspace custom rule JSON array.
SETTING_NAME = "crm_qualify_keywords"

# Hard cap on how many custom keywords we persist per workspace.
_MAX_CUSTOM_KEYWORDS = 200

# Minimal negation guard: if the message contains one of these phrases we skip
# qualifying entirely (a "not interested" reply must not advance the lead).
_NEGATION_PHRASES = ["not interested", "no thanks"]


def _normalize_status(status):
    """
    Coerce an arbitrary status value to a valid CRM status string.

    Returns one of VALID_STATUSES. Anything missing, non-string, or not in the
    allowed set falls back to "qualified". Never raises.
    """
    try:
        if status is None:
            return _DEFAULT_STATUS
        s = str(status).strip().lower()
        if s in VALID_STATUSES:
            return s
        return _DEFAULT_STATUS
    except Exception:
        return _DEFAULT_STATUS


def _coerce_rule(item):
    """
    Normalize a single stored/incoming entry into a rule dict, or return None.

    Accepts:
        - a dict like {"keyword": str, "status": str} (status validated, missing
          or invalid -> "qualified")
        - a bare string (BACKWARD-COMPAT) -> {"keyword": <str>, "status":
          "qualified"}

    The keyword is trimmed. Empty/invalid keywords yield None. Never raises.
    """
    try:
        if isinstance(item, dict):
            raw_kw = item.get("keyword", "")
            status = item.get("status")
        else:
            # Bare string (or anything stringifiable) -> qualified.
            raw_kw = item
            status = _DEFAULT_STATUS
        try:
            kw = str(raw_kw).strip()
        except Exception:
            return None
        if not kw:
            return None
        return {"keyword": kw, "status": _normalize_status(status)}
    except Exception:
        return None


def default_qualify_rules():
    """
    Expose DEFAULT_QUALIFY_KEYWORDS as rule objects.

    Returns a fresh list of {"keyword": <kw>, "status": "qualified"} for each
    default keyword. Never raises.
    """
    try:
        return [
            {"keyword": kw, "status": _DEFAULT_STATUS}
            for kw in DEFAULT_QUALIFY_KEYWORDS
        ]
    except Exception:
        logger.exception("default_qualify_rules: failed to build defaults")
        return []


def _coerce_workspace_id_candidates(workspace_id):
    """
    Return a list of candidate values to match Setting.workspace_id against.

    The Setting model stores workspace_id as an Integer, but the contract passes
    it around as str. To be backend-agnostic we try the int form first (matches
    the real column type), then the original/str form. Never raises.
    """
    candidates = []
    try:
        candidates.append(int(workspace_id))
    except (TypeError, ValueError):
        pass
    # Always include the str form (the documented contract value).
    s = str(workspace_id)
    if s not in (str(c) for c in candidates):
        candidates.append(s)
    # And the raw value last, in case it is some other comparable type.
    if workspace_id not in candidates:
        candidates.append(workspace_id)
    return candidates


def _find_setting_row(session, Setting, workspace_id):
    """
    Locate the Setting row for (workspace_id, SETTING_NAME). Returns the row or
    None. Tries each workspace_id candidate form. Never raises.
    """
    for candidate in _coerce_workspace_id_candidates(workspace_id):
        try:
            row = (
                session.query(Setting)
                .filter(
                    Setting.workspace_id == candidate,
                    Setting.name == SETTING_NAME,
                )
                .first()
            )
        except Exception:
            # A type-mismatch on one candidate should not abort the others.
            try:
                session.rollback()
            except Exception:
                pass
            row = None
        if row is not None:
            return row
    return None


def _sanitize_custom_list(custom_list):
    """
    Normalize a custom keyword list into rule objects.

    Each entry is coerced via _coerce_rule (dicts kept; bare strings upgraded to
    status "qualified"; status validated against VALID_STATUSES with a
    "qualified" fallback). Trims keywords, drops empties, dedupes
    case-insensitively by keyword (preserving the first-seen rule), and caps the
    length at _MAX_CUSTOM_KEYWORDS.

    Always returns a list of {"keyword", "status"} (possibly empty). Never
    raises.
    """
    cleaned = []
    seen = set()
    try:
        if not custom_list:
            return cleaned
        for item in custom_list:
            rule = _coerce_rule(item)
            if rule is None:
                continue
            key = rule["keyword"].lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(rule)
            if len(cleaned) >= _MAX_CUSTOM_KEYWORDS:
                break
    except Exception:
        logger.exception("_sanitize_custom_list: failed to sanitize custom keywords")
        return cleaned
    return cleaned


def get_qualify_keywords(workspace_id):
    """
    Return the effective qualify-keyword config for a workspace.

    Shape:
        {"defaults": [{"keyword", "status"}...],
         "custom":   [{"keyword", "status"}...]}

    Defaults come from default_qualify_rules() (all status "qualified"). The
    custom list is read from the CRM Setting row (name == SETTING_NAME, scoped to
    workspace_id). The stored value is a JSON array; each entry is normalized to a
    rule object, with bare-string entries upgraded to status "qualified" for
    backward compatibility. Defaults to [] custom on missing/invalid. Never
    raises.
    """
    from flask import current_app

    result = {"defaults": default_qualify_rules(), "custom": []}

    try:
        Setting = None
        try:
            Setting = current_app.crm_models["Setting"]
        except Exception:
            Setting = None
        if Setting is None:
            logger.warning("get_qualify_keywords: Setting model not configured")
            return result

        if workspace_id is None:
            return result

        session = current_app.db.session

        row = _find_setting_row(session, Setting, workspace_id)
        if row is None:
            return result

        raw = getattr(row, "value", None)
        if not raw:
            return result

        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning(
                "get_qualify_keywords: invalid JSON in Setting value for workspace %s",
                workspace_id,
            )
            return result

        if isinstance(parsed, list):
            result["custom"] = _sanitize_custom_list(parsed)
        else:
            logger.warning(
                "get_qualify_keywords: Setting value is not a JSON array for workspace %s",
                workspace_id,
            )
        return result

    except Exception:
        logger.exception("get_qualify_keywords: unexpected error")
        return result


def set_qualify_keywords(workspace_id, custom_list):
    """
    Upsert the per-workspace custom qualify-keyword list.

    `custom_list` is a list of rule objects {"keyword": str, "status": str}; bare
    strings are also tolerated and treated as status "qualified". Sanitizes via
    _sanitize_custom_list (trim keyword, drop empties, validate status against
    VALID_STATUSES with a "qualified" fallback, dedupe by lowercased keyword, cap
    at _MAX_CUSTOM_KEYWORDS), stores it as json.dumps(...) of rule objects on the
    Setting row (name == SETTING_NAME, scoped to workspace_id), creating the row
    if missing. Commits.

    Returns the same dict shape as get_qualify_keywords. Never raises (rolls back
    and returns the defaults-only shape on error).
    """
    from flask import current_app

    cleaned = _sanitize_custom_list(custom_list)
    fallback = {"defaults": default_qualify_rules(), "custom": []}

    try:
        Setting = None
        try:
            Setting = current_app.crm_models["Setting"]
        except Exception:
            Setting = None
        if Setting is None:
            logger.warning("set_qualify_keywords: Setting model not configured")
            return fallback

        if workspace_id is None:
            logger.info("set_qualify_keywords: no workspace_id, skipping")
            return fallback

        session = current_app.db.session
        encoded = json.dumps(cleaned)

        row = _find_setting_row(session, Setting, workspace_id)

        try:
            if row is not None:
                row.value = encoded
                session.add(row)
            else:
                # Store workspace_id as the int form when possible (matches the
                # Integer column), else fall back to str.
                ws_value = workspace_id
                try:
                    ws_value = int(workspace_id)
                except (TypeError, ValueError):
                    ws_value = str(workspace_id)
                row = Setting(
                    workspace_id=ws_value,
                    name=SETTING_NAME,
                    value=encoded,
                )
                session.add(row)
            session.commit()
        except Exception:
            logger.exception("set_qualify_keywords: failed to upsert Setting row")
            try:
                session.rollback()
            except Exception:
                pass
            return fallback

        return {"defaults": default_qualify_rules(), "custom": cleaned}

    except Exception:
        logger.exception("set_qualify_keywords: unexpected error")
        try:
            current_app.db.session.rollback()
        except Exception:
            pass
        return fallback


def match_qualify_rule(text, workspace_id):
    """
    Return the FIRST matched buying-intent rule in `text`, or None.

    Logic:
        - empty text -> None
        - minimal negation guard: "not interested" / "no thanks" -> None
        - effective list = defaults + custom (from get_qualify_keywords)
        - whole-word, case-insensitive regex match (handles multi-word phrases
          like "how much" too)
        - returns the matched rule object {"keyword": <str>, "status": <str>} on
          first hit, else None

    Never raises.
    """
    try:
        if not text:
            return None

        text_str = str(text)
        lowered = text_str.lower()

        # Minimal negation guard - a clear "no" must not qualify the lead.
        for phrase in _NEGATION_PHRASES:
            if phrase in lowered:
                return None

        config = get_qualify_keywords(workspace_id)
        effective = list(config.get("defaults", [])) + list(config.get("custom", []))

        for rule in effective:
            try:
                if not rule:
                    continue
                kw = rule.get("keyword")
                if not kw:
                    continue
                pattern = r"\b" + re.escape(str(kw)) + r"\b"
                if re.search(pattern, text_str, re.IGNORECASE):
                    # Defensive copy with a validated status.
                    return {
                        "keyword": kw,
                        "status": _normalize_status(rule.get("status")),
                    }
            except Exception:
                # A single bad keyword pattern must not abort the whole scan.
                continue

        return None

    except Exception:
        logger.exception("match_qualify_rule: unexpected error")
        return None


def match_qualify_keyword(text, workspace_id):
    """
    Return the FIRST matched buying-intent keyword string in `text`, or None.

    Backward-compatible thin wrapper over match_qualify_rule: returns just the
    matched keyword (rule["keyword"]) so existing callers keep working. Never
    raises.
    """
    try:
        rule = match_qualify_rule(text, workspace_id)
        if rule is None:
            return None
        return rule.get("keyword")
    except Exception:
        logger.exception("match_qualify_keyword: unexpected error")
        return None
