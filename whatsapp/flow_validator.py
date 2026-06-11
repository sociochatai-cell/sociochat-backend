"""
Flow Validator - Strict Pre-Publish Validation
===============================================

Validates WhatsApp Flow JSON BEFORE publishing to Meta.
Implements all Meta requirements and Refinement #3.

Validation Rules:
- Max 10 screens (Meta hard limit)
- Footer required on non-terminal screens
- No banned keywords (password, login, etc.)
- No external URLs in static flows
- Entry screen must exist
- Valid component types
"""

import json
import re
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


# Valid component types for Phase 1
VALID_COMPONENT_TYPES = {
    "TextHeading",
    "TextBody",
    "TextInput",
    "TextArea",
    "Dropdown",
    "RadioButtonsGroup",
    "CheckboxGroup",
    "DatePicker",
    "Footer",
    "Image",
    "EmbeddedLink",
}

# Banned keywords that Meta will reject
BANNED_KEYWORDS = [
    "password",
    "login",
    "sign in",
    "signin",
    "sign-in",
    "credit card",
    "debit card",
    "cvv",
    "ssn",
    "social security",
    "bank account",
    "routing number",
]

# Input types that are banned
BANNED_INPUT_TYPES = [
    "password",
]


@dataclass
class ValidationError:
    """Single validation error with severity."""
    message: str
    severity: str  # "error" or "warning"
    screen_id: Optional[str] = None
    component_type: Optional[str] = None


@dataclass
class FlowValidationResult:
    """Result of flow validation."""
    valid: bool
    errors: List[ValidationError] = field(default_factory=list)
    warnings: List[ValidationError] = field(default_factory=list)
    screen_count: int = 0
    component_count: int = 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [{"message": e.message, "screen_id": e.screen_id, "component": e.component_type} for e in self.errors],
            "warnings": [{"message": w.message, "screen_id": w.screen_id, "component": w.component_type} for w in self.warnings],
            "screen_count": self.screen_count,
            "component_count": self.component_count,
        }


def _alpha_suffix(index: int) -> str:
    suffix = ""
    n = max(0, int(index))
    while True:
        remainder = n % 26
        suffix = chr(65 + remainder) + suffix
        n = n // 26
        if n == 0:
            break
        n -= 1
    return suffix


def _to_meta_safe_screen_id(source: str, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z_]+", "_", (source or fallback or "SCREEN"))
    candidate = re.sub(r"_+", "_", candidate).strip("_") or "SCREEN"
    if candidate.upper() == "SUCCESS":
        candidate = "SCREEN_SUCCESS"
    return candidate[:64]


def sanitize_flow_json(
    flow_json: Dict[str, Any],
    entry_screen_id: str,
) -> tuple[Dict[str, Any], str]:
    """
    Remap screen IDs to Meta-safe format (letters and underscores only).
    Fixes flows saved with internal builder IDs that contain numbers.
    """
    import copy

    flow_json = copy.deepcopy(flow_json)
    screens = flow_json.get("screens") or []
    if not screens:
        return flow_json, entry_screen_id

    id_map: Dict[str, str] = {}
    used: set[str] = set()
    base_counts: Dict[str, int] = {}

    for i, screen in enumerate(screens):
        old_id = screen.get("id") or f"screen_{i}"
        base = _to_meta_safe_screen_id(old_id, screen.get("title") or f"STEP_{i + 1}")
        attempt = base_counts.get(base, 0)
        new_id = base
        while new_id in used:
            new_id = f"{base}_{_alpha_suffix(attempt)}"
            attempt += 1
        base_counts[base] = attempt
        used.add(new_id)
        id_map[old_id] = new_id
        screen["id"] = new_id

    routing = flow_json.get("routing_model") or {}
    new_routing: Dict[str, list] = {}
    for old_key, targets in routing.items():
        new_key = id_map.get(old_key, _to_meta_safe_screen_id(old_key, old_key))
        new_targets = [id_map.get(t, t) for t in (targets or [])]
        new_routing[new_key] = new_targets
    flow_json["routing_model"] = new_routing

    for screen in screens:
        children = screen.get("layout", {}).get("children", [])
        for child in children:
            action = child.get("on-click-action") or {}
            next_obj = action.get("next") or {}
            if next_obj.get("type") == "screen" and next_obj.get("name"):
                old_next = next_obj["name"]
                if old_next in id_map:
                    next_obj["name"] = id_map[old_next]

    screen_ids = [s.get("id") for s in screens]
    new_entry = id_map.get(entry_screen_id, entry_screen_id)
    if new_entry not in screen_ids:
        new_entry = screen_ids[0]

    version = str(flow_json.get("version") or "")
    if version in ("", "5.0", "6.0", "6.1"):
        flow_json["version"] = "7.3"
    if "data_api_version" in flow_json and not flow_json.get("endpoint_uri"):
        flow_json.pop("data_api_version", None)

    return flow_json, new_entry


def validate_flow_json(flow_json: Dict[str, Any], entry_screen_id: str) -> FlowValidationResult:
    """
    Validate Flow JSON strictly before publishing.
    
    Args:
        flow_json: The complete Flow JSON object
        entry_screen_id: The ID of the entry/start screen
        
    Returns:
        FlowValidationResult with errors and warnings
    """
    result = FlowValidationResult(valid=True)
    
    if not flow_json:
        result.valid = False
        result.errors.append(ValidationError("Flow JSON is empty", "error"))
        return result
    
    screens = flow_json.get("screens", [])
    result.screen_count = len(screens)
    
    # === Rule 1: Max 10 screens (Meta hard limit) ===
    if len(screens) > 10:
        result.valid = False
        result.errors.append(ValidationError(
            f"Too many screens ({len(screens)}). Maximum allowed is 10.",
            "error"
        ))
    
    # === Rule 2: At least 1 screen required ===
    if len(screens) == 0:
        result.valid = False
        result.errors.append(ValidationError("At least one screen is required", "error"))
        return result
    
    # === Rule 3: Entry screen must exist ===
    screen_ids = [s.get("id") for s in screens]
    if entry_screen_id not in screen_ids:
        result.valid = False
        result.errors.append(ValidationError(
            f"Entry screen '{entry_screen_id}' not found in flow screens. Available: {screen_ids}",
            "error"
        ))
    
    # === Rule 4: Check for banned keywords ===
    flow_text = json.dumps(flow_json).lower()
    for keyword in BANNED_KEYWORDS:
        if keyword in flow_text:
            result.valid = False
            result.errors.append(ValidationError(
                f"Banned keyword '{keyword}' found. Meta will reject this flow.",
                "error"
            ))
    
    # === Rule 5: Validate each screen ===
    for screen in screens:
        screen_id = screen.get("id", "unknown")
        layout = screen.get("layout", {})
        children = layout.get("children", [])
        is_terminal = screen.get("terminal", False)
        
        # Count components
        result.component_count += len(children)
        
        # 5a: Screen must have an ID
        if not screen.get("id"):
            result.valid = False
            result.errors.append(ValidationError("Screen missing 'id' field", "error", screen_id))
        else:
            # 5a-2: Screen ID must only contain alphabetic characters and underscores (NO NUMBERS!)
            # Meta's strict requirement
            screen_id_value = screen.get("id")
            if not re.match(r'^[A-Za-z_]+$', screen_id_value):
                result.valid = False
                result.errors.append(ValidationError(
                    f"Screen ID '{screen_id_value}' contains invalid characters. "
                    "Only alphabetic characters (A-Z, a-z) and underscores are allowed. NO NUMBERS!",
                    "error",
                    screen_id
                ))
            # 5a-3: 'SUCCESS' is a reserved keyword
            if screen_id_value.upper() == "SUCCESS":
                result.valid = False
                result.errors.append(ValidationError(
                    "Screen ID 'SUCCESS' is reserved by Meta and cannot be used.",
                    "error",
                    screen_id
                ))
        
        # 5b: Screen must have a title
        if not screen.get("title"):
            result.warnings.append(ValidationError("Screen missing 'title' field", "warning", screen_id))
        
        # 5c: Footer required on non-terminal screens
        if not is_terminal:
            has_footer = any(c.get("type") == "Footer" for c in children)
            if not has_footer:
                result.valid = False
                result.errors.append(ValidationError(
                    "Footer with navigation is required on non-terminal screens",
                    "error",
                    screen_id
                ))
        
        # 5d: Validate each component
        for component in children:
            component_type = component.get("type", "Unknown")
            
            # Check if valid component type
            if component_type not in VALID_COMPONENT_TYPES:
                result.warnings.append(ValidationError(
                    f"Unknown component type '{component_type}'",
                    "warning",
                    screen_id,
                    component_type
                ))
            
            # Check for banned input types
            if component.get("input-type") in BANNED_INPUT_TYPES:
                result.valid = False
                result.errors.append(ValidationError(
                    f"Banned input type '{component.get('input-type')}'",
                    "error",
                    screen_id,
                    component_type
                ))
            
            # Check for external URLs in text components
            if component_type in ["TextBody", "TextHeading"]:
                text = component.get("text", "")
                if "http://" in text or "https://" in text:
                    result.valid = False
                    result.errors.append(ValidationError(
                        "External URLs are not allowed in static flows",
                        "error",
                        screen_id,
                        component_type
                    ))
    
    # === Rule 6: At least one terminal screen ===
    has_terminal = any(s.get("terminal", False) for s in screens)
    if not has_terminal:
        result.valid = False
        result.errors.append(ValidationError(
            "Flow must have at least one terminal screen (with 'terminal': true)",
            "error"
        ))
    
    # === Rule 7: UX recommendation (warning only) ===
    if len(screens) > 4:
        result.warnings.append(ValidationError(
            f"Flow has {len(screens)} screens. Recommend 4 or fewer for better completion rates.",
            "warning"
        ))
    
    # === Rule 8: Validate version (v7.3 recommended) ===
    version = flow_json.get("version", "")
    if version in ["2.1", "3.0", "3.1", "4.0", "5.0"]:
        result.warnings.append(ValidationError(
            f"Flow version '{version}' is deprecated. Upgrade to v7.3 for best compatibility.",
            "warning"
        ))
    elif not version:
        result.valid = False
        result.errors.append(ValidationError("Flow JSON missing 'version' field", "error"))
    
    # === Rule 9: Validate routing_model (required for v7.3+) ===
    routing_model = flow_json.get("routing_model")
    if version and version >= "7.0":
        if routing_model is None:
            result.valid = False
            result.errors.append(ValidationError(
                "Flow v7.3+ requires a 'routing_model' field mapping screen navigation",
                "error"
            ))
        elif isinstance(routing_model, dict):
            # Validate routing_model entries
            for screen_id, next_screens in routing_model.items():
                # Screen ID must exist
                if screen_id not in screen_ids:
                    result.valid = False
                    result.errors.append(ValidationError(
                        f"routing_model contains unknown screen '{screen_id}'",
                        "error"
                    ))
                # Target screens must exist
                if isinstance(next_screens, list):
                    for target in next_screens:
                        if target not in screen_ids:
                            result.valid = False
                            result.errors.append(ValidationError(
                                f"routing_model['{screen_id}'] references unknown screen '{target}'",
                                "error"
                            ))
            
            # Check terminal screens have empty routing
            for screen in screens:
                if screen.get("terminal", False):
                    screen_id = screen.get("id")
                    if screen_id in routing_model and len(routing_model[screen_id]) > 0:
                        result.warnings.append(ValidationError(
                            f"Terminal screen '{screen_id}' should have empty routing_model entry",
                            "warning",
                            screen_id
                        ))
    
    return result


def validate_component(component: Dict[str, Any]) -> List[str]:
    """Validate a single component. Returns list of error messages."""
    errors = []
    comp_type = component.get("type", "")
    
    # TextInput validation
    if comp_type == "TextInput":
        if not component.get("name"):
            errors.append("TextInput must have a 'name' property")
        if not component.get("label"):
            errors.append("TextInput must have a 'label' property")
    
    # Dropdown validation
    if comp_type == "Dropdown":
        if not component.get("name"):
            errors.append("Dropdown must have a 'name' property")
        if not component.get("data-source"):
            errors.append("Dropdown must have a 'data-source' property")
    
    # Footer validation
    if comp_type == "Footer":
        if not component.get("label"):
            errors.append("Footer must have a 'label' property")
        if not component.get("on-click-action"):
            errors.append("Footer must have an 'on-click-action' property")
    
    return errors


def generate_sample_flow(category: str = "LEAD_GEN") -> Dict[str, Any]:
    """
    Generate a sample Flow JSON for testing or templates.
    
    Args:
        category: Flow category (LEAD_GEN, SURVEY, etc.)
        
    Returns:
        Valid Flow JSON structure
    """
    if category == "LEAD_GEN":
        return {
            "version": "5.0",
            "screens": [
                {
                    "id": "WELCOME",
                    "title": "Get Started",
                    "terminal": False,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [
                            {
                                "type": "TextHeading",
                                "text": "Welcome! Let's get to know you."
                            },
                            {
                                "type": "TextInput",
                                "name": "full_name",
                                "label": "Your Name",
                                "input-type": "text",
                                "required": True
                            },
                            {
                                "type": "TextInput",
                                "name": "email",
                                "label": "Email Address",
                                "input-type": "email",
                                "required": True
                            },
                            {
                                "type": "TextInput",
                                "name": "phone",
                                "label": "Phone Number",
                                "input-type": "phone",
                                "required": False
                            },
                            {
                                "type": "Footer",
                                "label": "Continue",
                                "on-click-action": {
                                    "name": "navigate",
                                    "next": {
                                        "type": "screen",
                                        "name": "THANK_YOU"
                                    }
                                }
                            }
                        ]
                    }
                },
                {
                    "id": "THANK_YOU",
                    "title": "Thank You",
                    "terminal": True,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [
                            {
                                "type": "TextHeading",
                                "text": "Thanks for your interest!"
                            },
                            {
                                "type": "TextBody",
                                "text": "We'll be in touch soon."
                            },
                            {
                                "type": "Footer",
                                "label": "Done",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {
                                        "full_name": "${form.full_name}",
                                        "email": "${form.email}",
                                        "phone": "${form.phone}"
                                    }
                                }
                            }
                        ]
                    }
                }
            ],
            "routing_model": {}
        }
    
    elif category == "SURVEY":
        return {
            "version": "5.0",
            "screens": [
                {
                    "id": "RATING",
                    "title": "Quick Survey",
                    "terminal": False,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [
                            {
                                "type": "TextHeading",
                                "text": "How was your experience?"
                            },
                            {
                                "type": "RadioButtonsGroup",
                                "name": "rating",
                                "label": "Select your rating",
                                "required": True,
                                "data-source": [
                                    {"id": "5", "title": "⭐⭐⭐⭐⭐ Excellent"},
                                    {"id": "4", "title": "⭐⭐⭐⭐ Good"},
                                    {"id": "3", "title": "⭐⭐⭐ Average"},
                                    {"id": "2", "title": "⭐⭐ Poor"},
                                    {"id": "1", "title": "⭐ Very Poor"}
                                ]
                            },
                            {
                                "type": "Footer",
                                "label": "Next",
                                "on-click-action": {
                                    "name": "navigate",
                                    "next": {
                                        "type": "screen",
                                        "name": "FEEDBACK"
                                    }
                                }
                            }
                        ]
                    }
                },
                {
                    "id": "FEEDBACK",
                    "title": "Additional Feedback",
                    "terminal": True,
                    "layout": {
                        "type": "SingleColumnLayout",
                        "children": [
                            {
                                "type": "TextHeading",
                                "text": "Any additional comments?"
                            },
                            {
                                "type": "TextArea",
                                "name": "comments",
                                "label": "Your feedback (optional)",
                                "required": False
                            },
                            {
                                "type": "Footer",
                                "label": "Submit",
                                "on-click-action": {
                                    "name": "complete",
                                    "payload": {
                                        "rating": "${form.rating}",
                                        "comments": "${form.comments}"
                                    }
                                }
                            }
                        ]
                    }
                }
            ],
            "routing_model": {}
        }
    
    # Default empty flow
    return {
        "version": "5.0",
        "screens": [],
        "routing_model": {}
    }
