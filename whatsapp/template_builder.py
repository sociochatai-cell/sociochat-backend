"""
WhatsApp Template Builder - Phase 1
====================================

Generic template payload builder for WhatsApp Cloud API.

IMPORTANT: Meta Cloud API is VERY strict about template payloads.
- Templates without variables must NOT include components
- Components must EXACTLY match the template structure
- Language code must match the approved template language

This builder handles all template types:
- Text-only templates (hello_world)
- Header templates (text, image, video, document)
- Body with variables
- CTA button templates
- Quick reply button templates

Usage:
    builder = TemplateBuilder("hello_world", "en")
    payload = builder.build()  # Returns exact Meta API payload

    builder = TemplateBuilder("order_update", "en")
    builder.add_body_params(["John", "12345", "$99.00"])
    builder.add_header_image("https://example.com/image.jpg")
    payload = builder.build()
"""

import logging
from typing import Optional, Dict, Any, List, Union
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TemplateComponent:
    """
    Represents a single component in the template payload.
    
    Components are ONLY needed when template has:
    - Header with variable or media
    - Body with variables
    - Buttons with dynamic data
    """
    type: str  # header, body, button
    parameters: List[Dict[str, Any]] = field(default_factory=list)
    sub_type: Optional[str] = None  # For buttons: quick_reply, url
    index: Optional[int] = None  # For buttons: button index (0, 1, 2)
    parameter_name: Optional[str] = None  # For named parameters

    def to_dict(self) -> Dict[str, Any]:
        """Convert to Meta API format."""
        result = {"type": self.type}
        
        if self.parameters:
            result["parameters"] = self.parameters
        
        if self.sub_type:
            result["sub_type"] = self.sub_type
        
        if self.index is not None:
            result["index"] = str(self.index)
        
        return result


class TemplateBuilder:
    """
    Build WhatsApp template message payloads.
    
    Generates payloads that EXACTLY match Meta Cloud API format.
    
    Key principles:
    1. Only include components when necessary
    2. Never include empty components array
    3. Match component types to template structure
    4. Use correct parameter types (text, image, video, document)
    """
    
    def __init__(self, template_name: str, language: str = "en"):
        """
        Initialize template builder.
        
        Args:
            template_name: Name of the approved template
            language: Language code (e.g., "en", "en_US")
        """
        self.template_name = template_name
        self.language = language
        self._header_component: Optional[TemplateComponent] = None
        self._body_component: Optional[TemplateComponent] = None
        self._button_components: List[TemplateComponent] = []
    
    # ============================================================
    # Header Methods
    # ============================================================
    
    def add_header_text(self, text: str) -> "TemplateBuilder":
        """
        Add text header parameter.
        
        Use when template header contains {{1}} variable.
        
        Args:
            text: Text value for header variable
        """
        self._header_component = TemplateComponent(
            type="header",
            parameters=[{"type": "text", "text": text}]
        )
        return self
    
    def add_header_image(self, url: str) -> "TemplateBuilder":
        """
        Add image header.
        
        Use when template has IMAGE header type.
        
        Args:
            url: Public URL of the image
        """
        self._header_component = TemplateComponent(
            type="header",
            parameters=[{
                "type": "image",
                "image": {"link": url}
            }]
        )
        return self
    
    def add_header_video(self, url: str) -> "TemplateBuilder":
        """
        Add video header.
        
        Args:
            url: Public URL of the video
        """
        self._header_component = TemplateComponent(
            type="header",
            parameters=[{
                "type": "video",
                "video": {"link": url}
            }]
        )
        return self
    
    def add_header_document(self, url: str, filename: Optional[str] = None) -> "TemplateBuilder":
        """
        Add document header.
        
        Args:
            url: Public URL of the document
            filename: Optional filename to display
        """
        doc = {"link": url}
        if filename:
            doc["filename"] = filename
        
        self._header_component = TemplateComponent(
            type="header",
            parameters=[{
                "type": "document",
                "document": doc
            }]
        )
        return self
    
    # ============================================================
    # Body Methods
    # ============================================================
    
    def add_body_params(self, params: List[str]) -> "TemplateBuilder":
        """
        Add body text parameters.
        
        Use when template body contains {{1}}, {{2}}, etc.
        Parameters are applied in order.
        
        Args:
            params: List of text values for body variables
        """
        if not params:
            return self
        
        self._body_component = TemplateComponent(
            type="body",
            parameters=[{"type": "text", "text": str(p)} for p in params]
        )
        return self
    
    def add_body_param(self, index: int, text: str) -> "TemplateBuilder":
        """
        Add a single body parameter at specific index.
        
        Args:
            index: Parameter index (1-based, matching {{1}}, {{2}}, etc.)
            text: Text value
        """
        if not self._body_component:
            self._body_component = TemplateComponent(type="body", parameters=[])
        
        # Ensure parameters list is long enough
        while len(self._body_component.parameters) < index:
            self._body_component.parameters.append({"type": "text", "text": ""})
        
        self._body_component.parameters[index - 1] = {"type": "text", "text": text}
        return self

    def add_named_body_params(self, params: Dict[str, str]) -> "TemplateBuilder":
        """
        Add named body parameters.
        
        Use when template uses named variables like {{name}}, {{order_id}}.
        
        Args:
            params: Dictionary of {parameter_name: value}
        """
        if not params:
            return self
            
        parameters = []
        for name, value in params.items():
            parameters.append({
                "type": "text", 
                "parameter_name": name,
                "text": str(value)
            })
            
        self._body_component = TemplateComponent(
            type="body",
            parameters=parameters
        )
        return self
    
    # ============================================================
    # Button Methods
    # ============================================================
    
    def add_quick_reply_button(self, index: int, payload: str) -> "TemplateBuilder":
        """
        Add quick reply button parameter.
        
        Args:
            index: Button index (0-based)
            payload: Payload string returned when button is clicked
        """
        self._button_components.append(TemplateComponent(
            type="button",
            sub_type="quick_reply",
            index=index,
            parameters=[{"type": "payload", "payload": payload}]
        ))
        return self
    
    def add_url_button(self, index: int, url_suffix: str) -> "TemplateBuilder":
        """
        Add URL button with dynamic suffix.
        
        Use when template has URL button with {{1}} variable.
        
        Args:
            index: Button index (0-based)
            url_suffix: Dynamic part of the URL
        """
        self._button_components.append(TemplateComponent(
            type="button",
            sub_type="url",
            index=index,
            parameters=[{"type": "text", "text": url_suffix}]
        ))
        return self
    
    def add_copy_code_button(self, index: int, code: str) -> "TemplateBuilder":
        """
        Add copy code button parameter.
        
        Args:
            index: Button index (0-based)
            code: Code to be copied
        """
        self._button_components.append(TemplateComponent(
            type="button",
            sub_type="copy_code",
            index=index,
            parameters=[{"type": "coupon_code", "coupon_code": code}]
        ))
        return self

    def add_catalog_button(self, index: int, thumbnail_product_retailer_id: str = "") -> "TemplateBuilder":
        """
        Add catalog button parameter.

        Meta requires CATALOG buttons to include an action parameter with
        thumbnail_product_retailer_id. If empty, Meta uses a default thumbnail.

        Args:
            index: Button index (0-based)
            thumbnail_product_retailer_id: Retailer ID of a product to use as thumbnail
        """
        params: List[Dict[str, Any]] = []
        if thumbnail_product_retailer_id:
            params.append({
                "type": "action",
                "action": {
                    "thumbnail_product_retailer_id": thumbnail_product_retailer_id
                }
            })
        self._button_components.append(TemplateComponent(
            type="button",
            sub_type="catalog",
            index=index,
            parameters=params,
        ))
        return self

    def add_flow_button(self, index: int, flow_token: str = "", flow_action_data: Optional[Dict] = None) -> "TemplateBuilder":
        """
        Add flow button parameter.

        Args:
            index: Button index (0-based)
            flow_token: Token for flow session
            flow_action_data: Optional data for the flow action
        """
        params: List[Dict[str, Any]] = []
        if flow_token:
            params.append({"type": "text", "text": flow_token})
        self._button_components.append(TemplateComponent(
            type="button",
            sub_type="flow",
            index=index,
            parameters=params,
        ))
        return self

    def add_voice_call_button(self, index: int) -> "TemplateBuilder":
        """
        Add voice call button. No parameters required at send time.

        Args:
            index: Button index (0-based)
        """
        # Voice call buttons don't need parameters at send time
        return self
    
    # ============================================================
    # Build Methods
    # ============================================================
    
    def _build_components(self) -> Optional[List[Dict[str, Any]]]:
        """
        Build components array for the template.
        
        Returns None if no components are needed.
        This is CRITICAL - templates like hello_world must NOT have components.
        """
        components = []
        
        if self._header_component:
            components.append(self._header_component.to_dict())
        
        if self._body_component and self._body_component.parameters:
            components.append(self._body_component.to_dict())
        
        for btn in self._button_components:
            components.append(btn.to_dict())
        
        # Return None if no components - NEVER return empty array
        return components if components else None
    
    def build(self) -> Dict[str, Any]:
        """
        Build the complete template payload for Meta Cloud API.
        
        Returns:
            Payload dict ready to be sent to /messages endpoint
            
        Format:
        {
            "messaging_product": "whatsapp",
            "to": "<recipient>",  # Added by caller
            "type": "template",
            "template": {
                "name": "<template_name>",
                "language": {"code": "<language>"},
                "components": [...]  # ONLY if needed
            }
        }
        """
        template_obj = {
            "name": self.template_name,
            "language": {"code": self.language}
        }
        
        components = self._build_components()
        if components:
            template_obj["components"] = components
        
        return {
            "messaging_product": "whatsapp",
            "type": "template",
            "template": template_obj
        }
    
    def build_with_recipient(self, to: str) -> Dict[str, Any]:
        """
        Build complete payload including recipient.
        
        Args:
            to: Recipient phone number (E.164 format without +)
            
        Returns:
            Complete payload ready for API call
        """
        payload = self.build()
        payload["to"] = to
        return payload
    
    # ============================================================
    # Debug Methods
    # ============================================================
    
    def get_debug_info(self) -> Dict[str, Any]:
        """Get debug information about the builder state."""
        return {
            "template_name": self.template_name,
            "language": self.language,
            "has_header": self._header_component is not None,
            "has_body_params": self._body_component is not None and bool(self._body_component.parameters),
            "button_count": len(self._button_components),
            "requires_components": self._build_components() is not None,
        }


# ============================================================
# Convenience Functions
# ============================================================

def build_simple_template(template_name: str, language: str = "en") -> Dict[str, Any]:
    """
    Build a simple template payload without any parameters.
    
    Use for templates like hello_world that have no variables.
    
    Args:
        template_name: Name of the template
        language: Language code
        
    Returns:
        Template payload WITHOUT components
    """
    return TemplateBuilder(template_name, language).build()


def build_template_with_body_params(
    template_name: str,
    params: List[str],
    language: str = "en"
) -> Dict[str, Any]:
    """
    Build template with body parameters.
    
    Args:
        template_name: Name of the template
        params: List of body parameter values
        language: Language code
        
    Returns:
        Template payload with body components
    """
    return TemplateBuilder(template_name, language).add_body_params(params).build()


def build_template_with_image_header(
    template_name: str,
    image_url: str,
    body_params: Optional[List[str]] = None,
    language: str = "en"
) -> Dict[str, Any]:
    """
    Build template with image header.
    
    Args:
        template_name: Name of the template
        image_url: Public URL of the image
        body_params: Optional body parameters
        language: Language code
        
    Returns:
        Template payload with header and optionally body components
    """
    builder = TemplateBuilder(template_name, language).add_header_image(image_url)
    if body_params:
        builder.add_body_params(body_params)
    return builder.build()


# ============================================================
# Payload Validation
# ============================================================

def validate_template_payload(payload: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate a template payload before sending.
    
    Args:
        payload: Template payload to validate
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not payload:
        return False, "Payload is empty"
    
    if payload.get("messaging_product") != "whatsapp":
        return False, "messaging_product must be 'whatsapp'"
    
    if payload.get("type") != "template":
        return False, "type must be 'template'"
    
    template = payload.get("template")
    if not template:
        return False, "template object is required"
    
    if not template.get("name"):
        return False, "template.name is required"
    
    language = template.get("language")
    if not language or not language.get("code"):
        return False, "template.language.code is required"
    
    # Validate components if present
    components = template.get("components")
    if components is not None:
        if not isinstance(components, list):
            return False, "components must be an array"
        
        if len(components) == 0:
            return False, "components array must not be empty (omit it instead)"
        
        for i, comp in enumerate(components):
            if not comp.get("type"):
                return False, f"components[{i}].type is required"
            
            comp_type = comp.get("type")
            if comp_type not in ("header", "body", "button"):
                return False, f"Invalid component type: {comp_type}"
            
            # Button components need additional fields
            if comp_type == "button":
                if comp.get("sub_type") is None:
                    return False, f"components[{i}].sub_type is required for button"
                if comp.get("index") is None:
                    return False, f"components[{i}].index is required for button"
    
    return True, None


def log_template_payload(payload: Dict[str, Any], level: str = "debug"):
    """
    Log template payload for debugging.
    
    Args:
        payload: Template payload
        level: Log level (debug, info, warning)
    """
    import json
    
    template = payload.get("template", {})
    has_components = "components" in template
    
    log_func = getattr(logger, level, logger.debug)
    log_func(
        f"Template payload: name={template.get('name')}, "
        f"language={template.get('language', {}).get('code')}, "
        f"has_components={has_components}"
    )
    log_func(f"Full payload:\n{json.dumps(payload, indent=2)}")
