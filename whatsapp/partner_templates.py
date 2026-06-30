"""
Partner-facing WhatsApp template list (Vaish / workspace_id integrations).

Returns only APPROVED, send-ready template metadata for outbound send-message calls.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .models import WhatsAppTemplate


def extract_variables_in_order(body_text: Optional[str]) -> List[Dict[str, Any]]:
    if not body_text:
        return []

    seen: set[str] = set()
    variables: List[Dict[str, Any]] = []
    for match in re.finditer(r"\{\{([^}]+)\}\}", body_text):
        raw = match.group(1).strip()
        if raw in seen:
            continue
        seen.add(raw)
        position = len(variables) + 1
        variables.append(
            {
                "position": position,
                "name": raw if not raw.isdigit() else f"var_{raw}",
                "placeholder": raw,
            }
        )
    return variables


def example_value_for_variable(template: "WhatsAppTemplate", placeholder: str, position: int) -> str:
    if placeholder.isdigit():
        if (template.category or "").upper() == "AUTHENTICATION" and position == 1:
            return "123456"
        return f"value_{placeholder}"
    return placeholder.replace("_", " ").title()


def build_send_example(template: "WhatsAppTemplate", workspace_id: str) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "to": "<recipient_phone_digits>",
        "type": "template",
        "template_name": template.name,
        "template_language": template.language,
    }

    variables = extract_variables_in_order(template.body_text)
    mapping = template.get_variable_mapping()
    if variables:
        parameters = []
        for v in variables:
            param = {
                "type": "text",
                "text": example_value_for_variable(template, v["placeholder"], v["position"]),
            }
            placeholder = str(v.get("placeholder") or "").strip()
            pos_key = str(v.get("position") or "")
            if placeholder and not placeholder.isdigit():
                param["parameter_name"] = placeholder
            elif pos_key in mapping and mapping[pos_key]:
                param["parameter_name"] = mapping[pos_key]
            parameters.append(param)
        payload["template_components"] = [{"type": "body", "parameters": parameters}]

    return payload


def template_to_partner_dict(template: "WhatsAppTemplate", workspace_id: str) -> Dict[str, Any]:
    variables = extract_variables_in_order(template.body_text)
    mapping = template.get_variable_mapping()

    return {
        "template_name": template.name,
        "template_language": template.language,
        "category": template.category,
        "status": template.status,
        "body_text": template.body_text,
        "header_text": template.header_text,
        "footer_text": template.footer_text,
        "variable_count": template.variable_count or len(variables),
        "variables": variables,
        "variable_mapping": mapping,
        "approved_at": template.approved_at.isoformat() + "Z" if template.approved_at else None,
        "is_archived": bool(template.is_archived),
        "send_example": build_send_example(template, workspace_id),
    }


def query_approved_templates(account_id: int) -> List["WhatsAppTemplate"]:
    from .models import WhatsAppTemplate

    return (
        WhatsAppTemplate.query.filter_by(account_id=account_id, status="APPROVED")
        .filter(WhatsAppTemplate.is_archived.is_(False))
        .order_by(WhatsAppTemplate.name.asc(), WhatsAppTemplate.language.asc())
        .all()
    )
