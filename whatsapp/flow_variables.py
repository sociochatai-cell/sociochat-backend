"""
Flow variable resolution and button-capture rules for interactive automations.

Variables and flow_config are stored per automation in the database — not in env vars.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|api_key|apikey|credential)", re.IGNORECASE)


def is_sensitive_variable_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key or "")))


def mask_variables_for_response(variables: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return variables safe for API responses (mask secret-like keys)."""
    if not isinstance(variables, dict):
        return {}
    masked: Dict[str, Any] = {}
    for key, value in variables.items():
        if is_sensitive_variable_key(key):
            masked[key] = "***" if value not in (None, "") else value
        else:
            masked[key] = value
    return masked


def merge_variable_defaults(
    variables: Dict[str, Any],
    defaults: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(variables or {})
    for key, default in (defaults or {}).items():
        current = merged.get(key)
        if current is None or (isinstance(current, str) and not current.strip()):
            merged[key] = default
    return merged


def _rule_matches(rule: Dict[str, Any], button_payload: str) -> bool:
    payload = str(button_payload or "").strip()
    if not payload:
        return False

    match_type = (rule.get("matchType") or rule.get("match") or "exact").lower()
    if match_type == "uuid":
        return bool(_UUID_RE.match(payload))
    if match_type == "exact":
        expected = str(rule.get("value") or rule.get("equals") or "").strip()
        if not expected:
            return False
        if rule.get("caseInsensitive", True):
            return payload.lower() == expected.lower()
        return payload == expected
    if match_type == "regex":
        pattern = rule.get("pattern") or rule.get("value")
        if not pattern:
            return False
        flags = re.IGNORECASE if rule.get("caseInsensitive", True) else 0
        return bool(re.match(str(pattern), payload, flags))
    if match_type == "any":
        return True
    return False


def apply_button_capture_rules(
    rules: Optional[List[Dict[str, Any]]],
    button_payload: str,
    set_field: Callable[[str, Any], None],
) -> bool:
    """
    Apply configured buttonCapture rules. Returns True if any rule matched.
    set_field(field_name, value) persists into conversation collected fields.
    """
    matched = False
    for rule in rules or []:
        if not isinstance(rule, dict):
            continue
        if not _rule_matches(rule, button_payload):
            continue

        field = rule.get("field") or rule.get("setField")
        if not field:
            continue

        if "setValue" in rule:
            value = rule.get("setValue")
        elif rule.get("valueFrom") == "button_id" or rule.get("from") == "button_id":
            value = button_payload
        else:
            value = button_payload

        set_field(str(field), value)
        matched = True
        if rule.get("stopOnMatch", True):
            break
    return matched


def collect_button_capture_rules(
    node_data: Optional[Dict[str, Any]],
    flow_config: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    if isinstance(flow_config, dict):
        global_rules = flow_config.get("buttonCaptureRules") or flow_config.get("buttonCapture")
        if isinstance(global_rules, list):
            rules.extend(global_rules)
    if isinstance(node_data, dict):
        node_rules = node_data.get("buttonCapture")
        if isinstance(node_rules, list):
            rules.extend(node_rules)
    return rules
