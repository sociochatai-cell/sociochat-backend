"""
Flow Testing Utilities - Phase 5: Testing & Publishing
=======================================================

Provides utilities for testing flows before publishing:
1. Preview flow in WhatsApp Manager format
2. Generate test data for dynamic flows
3. Validate publishing requirements
4. Clone-and-publish workflow helpers

Endpoints:
    POST /api/whatsapp/flows/{id}/preview - Get preview data
    POST /api/whatsapp/flows/{id}/test    - Test with sample data
    GET  /api/whatsapp/flows/{id}/publish-check - Pre-publish validation
"""

from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple
from flask import Blueprint, request, jsonify, g

from shared_models import db
from .models import WhatsAppFlow, WhatsAppAccount
from .flow_validator import validate_flow_json
from .flow_access import require_flow_access

# Create blueprint
flow_testing_bp = Blueprint("flow_testing", __name__, url_prefix="/api/whatsapp/flows")


# ============================================================
# Preview Data Generator
# ============================================================

def generate_preview_data(flow: WhatsAppFlow) -> Dict[str, Any]:
    """
    Generate preview data for a flow.
    
    Returns a structure that can be rendered in:
    1. WhatsApp Manager preview
    2. Local flow builder preview
    3. Test console
    """
    flow_json = flow.flow_json or {}
    screens = flow_json.get("screens", [])
    
    preview = {
        "flow_id": flow.id,
        "name": flow.name,
        "category": flow.category,
        "status": flow.status,
        "entry_screen": flow.entry_screen_id,
        "total_screens": len(screens),
        "screens": [],
        "navigation_map": {},
        "required_fields": [],
        "estimated_completion_time": _estimate_completion_time(screens)
    }
    
    for screen in screens:
        screen_preview = {
            "id": screen.get("id"),
            "title": screen.get("title", "Untitled"),
            "terminal": screen.get("terminal", False),
            "components": [],
            "input_fields": [],
            "next_screen": None
        }
        
        children = screen.get("layout", {}).get("children", [])
        for component in children:
            comp_type = component.get("type")
            
            # Track input fields
            if comp_type in ["TextInput", "TextArea", "Dropdown", "RadioButtonsGroup", "CheckboxGroup", "DatePicker"]:
                field_name = component.get("name", "unnamed")
                screen_preview["input_fields"].append({
                    "name": field_name,
                    "type": comp_type,
                    "label": component.get("label", field_name),
                    "required": component.get("required", False)
                })
                if component.get("required"):
                    preview["required_fields"].append(f"{screen.get('id')}.{field_name}")
            
            # Track navigation
            if comp_type == "Footer":
                action = component.get("on-click-action", {})
                if action.get("name") == "navigate":
                    next_screen = action.get("next", {}).get("name")
                    screen_preview["next_screen"] = next_screen
                    preview["navigation_map"][screen.get("id")] = next_screen
            
            screen_preview["components"].append({
                "type": comp_type,
                "label": component.get("label") or component.get("text", ""),
            })
        
        preview["screens"].append(screen_preview)
    
    return preview


def _estimate_completion_time(screens: List[dict]) -> str:
    """Estimate time to complete flow based on input fields."""
    total_fields = 0
    for screen in screens:
        children = screen.get("layout", {}).get("children", [])
        for comp in children:
            if comp.get("type") in ["TextInput", "TextArea", "Dropdown", "RadioButtonsGroup", "DatePicker"]:
                total_fields += 1
    
    # Estimate ~15 seconds per field
    seconds = total_fields * 15 + len(screens) * 5  # Plus 5 sec per screen navigation
    
    if seconds < 60:
        return f"~{seconds} seconds"
    else:
        return f"~{seconds // 60} minute{'s' if seconds >= 120 else ''}"


# ============================================================
# Test Data Generator
# ============================================================

def generate_test_data(flow: WhatsAppFlow) -> Dict[str, Any]:
    """
    Generate sample test data for all input fields in a flow.
    
    This data can be used to:
    1. Test dynamic flow endpoints
    2. Preview form completion
    3. Validate data handling
    """
    flow_json = flow.flow_json or {}
    screens = flow_json.get("screens", [])
    
    test_data = {
        "flow_id": flow.id,
        "samples": {},
        "form_data": {},
        "screen_responses": []
    }
    
    for screen in screens:
        screen_id = screen.get("id")
        screen_data = {"screen": screen_id, "data": {}}
        
        children = screen.get("layout", {}).get("children", [])
        for component in children:
            comp_type = component.get("type")
            field_name = component.get("name")
            
            if not field_name:
                continue
            
            # Generate sample data based on field type
            sample = _generate_sample_value(comp_type, field_name, component)
            screen_data["data"][field_name] = sample
            test_data["samples"][field_name] = sample
            test_data["form_data"][field_name] = sample
        
        if screen_data["data"]:
            test_data["screen_responses"].append(screen_data)
    
    return test_data


def _generate_sample_value(comp_type: str, field_name: str, component: dict) -> Any:
    """Generate appropriate sample value for a field type."""
    
    # Check field name for common patterns
    name_lower = field_name.lower()
    
    # Email
    if "email" in name_lower:
        return "test@example.com"
    
    # Phone
    if "phone" in name_lower or "mobile" in name_lower:
        return "+1234567890"
    
    # Name fields
    if "name" in name_lower:
        if "first" in name_lower:
            return "John"
        elif "last" in name_lower:
            return "Doe"
        else:
            return "John Doe"
    
    # Date
    if comp_type == "DatePicker" or "date" in name_lower:
        return datetime.now().strftime("%Y-%m-%d")
    
    # Dropdown / Radio
    if comp_type in ["Dropdown", "RadioButtonsGroup"]:
        options = component.get("data-source", [])
        if options:
            return options[0].get("id", "option_1")
        return "selected_option"
    
    # Checkbox
    if comp_type == "CheckboxGroup":
        options = component.get("data-source", [])
        if options:
            return [options[0].get("id", "option_1")]
        return ["checked_option"]
    
    # TextArea
    if comp_type == "TextArea" or "message" in name_lower or "feedback" in name_lower:
        return "This is sample feedback text for testing purposes."
    
    # Default text
    return "Sample Text"


# ============================================================
# Publish Readiness Check
# ============================================================

def check_publish_readiness(flow: WhatsAppFlow) -> Dict[str, Any]:
    """
    Comprehensive pre-publish validation.
    
    Checks:
    1. Flow status is DRAFT
    2. Flow JSON is valid
    3. All required screens exist
    4. Account has valid access token
    5. WABA has flows capability (if checkable)
    """
    checks = {
        "ready": True,
        "checks": [],
        "errors": [],
        "warnings": []
    }
    
    # Check 1: Status
    if flow.status != "DRAFT":
        checks["checks"].append({
            "name": "Status",
            "passed": False,
            "message": f"Flow is {flow.status}, not DRAFT"
        })
        checks["errors"].append(f"Cannot publish: Flow status is {flow.status}")
        checks["ready"] = False
    else:
        checks["checks"].append({
            "name": "Status",
            "passed": True,
            "message": "Flow is in DRAFT status"
        })
    
    # Check 2: JSON Validation
    validation = validate_flow_json(flow.flow_json, flow.entry_screen_id)
    if not validation.valid:
        checks["checks"].append({
            "name": "JSON Validation",
            "passed": False,
            "message": f"{len(validation.errors)} validation errors"
        })
        checks["errors"].extend([e.message for e in validation.errors])
        checks["ready"] = False
    else:
        checks["checks"].append({
            "name": "JSON Validation",
            "passed": True,
            "message": f"Valid flow with {validation.screen_count} screens"
        })
    
    # Check 3: Entry Screen
    screens = flow.flow_json.get("screens", [])
    screen_ids = [s.get("id") for s in screens]
    if flow.entry_screen_id not in screen_ids:
        checks["checks"].append({
            "name": "Entry Screen",
            "passed": False,
            "message": f"Entry screen '{flow.entry_screen_id}' not found"
        })
        checks["errors"].append(f"Entry screen '{flow.entry_screen_id}' does not exist")
        checks["ready"] = False
    else:
        checks["checks"].append({
            "name": "Entry Screen",
            "passed": True,
            "message": f"Entry screen '{flow.entry_screen_id}' exists"
        })
    
    # Check 4: Terminal Screen
    has_terminal = any(s.get("terminal", False) for s in screens)
    if not has_terminal:
        checks["checks"].append({
            "name": "Terminal Screen",
            "passed": False,
            "message": "No terminal screen found"
        })
        checks["errors"].append("Flow must have at least one terminal screen")
        checks["ready"] = False
    else:
        checks["checks"].append({
            "name": "Terminal Screen",
            "passed": True,
            "message": "Terminal screen exists"
        })
    
    # Check 5: Account Access Token
    account = flow.account
    if account:
        try:
            token = account.get_access_token()
            if token:
                checks["checks"].append({
                    "name": "Access Token",
                    "passed": True,
                    "message": "Account has valid access token"
                })
            else:
                checks["checks"].append({
                    "name": "Access Token",
                    "passed": False,
                    "message": "No access token available"
                })
                checks["errors"].append("Account has no access token")
                checks["ready"] = False
        except Exception:
            checks["checks"].append({
                "name": "Access Token",
                "passed": False,
                "message": "Could not retrieve access token"
            })
            checks["warnings"].append("Could not verify access token")
    
    # Check 6: Screen count
    if len(screens) > 10:
        checks["checks"].append({
            "name": "Screen Count",
            "passed": False,
            "message": f"Too many screens: {len(screens)}/10"
        })
        checks["errors"].append("Meta allows maximum 10 screens per flow")
        checks["ready"] = False
    else:
        checks["checks"].append({
            "name": "Screen Count",
            "passed": True,
            "message": f"Screen count: {len(screens)}/10"
        })
    
    return checks


# ============================================================
# API Endpoints
# ============================================================

@flow_testing_bp.route("/<int:flow_id>/preview", methods=["GET"])
@require_flow_access
def get_flow_preview(flow_id: int):
    """Get preview data for a flow."""
    flow = g.flow
    preview = generate_preview_data(flow)
    
    return jsonify({
        "success": True,
        "preview": preview
    })


@flow_testing_bp.route("/<int:flow_id>/test-data", methods=["GET"])
@require_flow_access
def get_flow_test_data(flow_id: int):
    """Get sample test data for a flow."""
    flow = g.flow
    test_data = generate_test_data(flow)
    
    return jsonify({
        "success": True,
        "test_data": test_data
    })


@flow_testing_bp.route("/<int:flow_id>/publish-check", methods=["GET"])
@require_flow_access
def check_flow_publish_readiness(flow_id: int):
    """Check if a flow is ready to publish."""
    flow = g.flow
    readiness = check_publish_readiness(flow)
    
    return jsonify({
        "success": True,
        "flow_id": flow_id,
        "flow_name": flow.name,
        **readiness
    })


@flow_testing_bp.route("/<int:flow_id>/simulate", methods=["POST"])
@require_flow_access
def simulate_flow_completion(flow_id: int):
    """
    Simulate a complete flow with test data.
    
    This tests the full flow path without actually publishing.
    Useful for validating navigation and data handling.
    """
    flow = g.flow
    
    # Get test data
    test_data = generate_test_data(flow)
    
    # Simulate screen transitions
    screens = flow.flow_json.get("screens", [])
    transitions = []
    current_screen = flow.entry_screen_id
    visited = set()
    
    while current_screen and current_screen not in visited and len(transitions) < 20:
        visited.add(current_screen)
        
        # Find current screen
        screen = next((s for s in screens if s.get("id") == current_screen), None)
        if not screen:
            transitions.append({
                "screen": current_screen,
                "error": "Screen not found"
            })
            break
        
        # Record transition
        transition = {
            "screen": current_screen,
            "title": screen.get("title"),
            "terminal": screen.get("terminal", False),
            "data_collected": {}
        }
        
        # Collect data for this screen
        screen_data = next(
            (r for r in test_data["screen_responses"] if r["screen"] == current_screen),
            {"data": {}}
        )
        transition["data_collected"] = screen_data["data"]
        
        transitions.append(transition)
        
        # Check if terminal
        if screen.get("terminal"):
            break
        
        # Find next screen
        children = screen.get("layout", {}).get("children", [])
        footer = next((c for c in children if c.get("type") == "Footer"), None)
        if footer:
            action = footer.get("on-click-action", {})
            if action.get("name") == "navigate":
                current_screen = action.get("next", {}).get("name")
            else:
                break
        else:
            break
    
    return jsonify({
        "success": True,
        "flow_id": flow_id,
        "simulation": {
            "total_screens_visited": len(transitions),
            "completed": any(t.get("terminal") for t in transitions),
            "transitions": transitions,
            "collected_data": test_data["form_data"]
        }
    })
