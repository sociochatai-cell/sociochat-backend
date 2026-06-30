"""
AI-assisted draft generation for WhatsApp interactive automation flows.

Produces nodes + edges compatible with the Launchpad visual flow builder,
including fully configured API nodes (headers, auth placeholders, button capture).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .ai_chatbot import DEFAULT_MODEL, _resolve_generation_model, get_genai_client

logger = logging.getLogger(__name__)

_VALID_INPUT_TYPES = frozenset({"text", "number", "email", "phone", "regex", "enum", "pincode"})
_VALID_NODE_TYPES = frozenset({"message", "input", "end", "template", "api"})
_VALID_API_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
_DEFAULT_AUTH_HEADERS = [
    {"key": "Authorization", "value": "Bearer {{flow_api_token}}", "enabled": True},
    {"key": "Content-Type", "value": "application/json", "enabled": True},
]
_DEFAULT_API_OUTPUT = {
    "onSuccess": {
        "mode": "auto",
        "textPath": "message",
        "buttonsPath": "quickReplies",
        "fallbackText": "Done.",
    },
    "onError": {
        "text": "Service unavailable. Please try again.",
    },
}


def _extract_json_object(raw: str) -> Dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("Empty model response")

    if "```" in text:
        for part in text.split("```"):
            chunk = part.strip()
            if chunk.lower().startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                text = chunk
                break

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("Model did not return JSON")

    return json.loads(text[start : end + 1])


def _new_node_id(prefix: str = "node") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _edge_id(source: str, handle: str, target: str) -> str:
    return f"edge_{source}_{handle}_{target}"


def _prompt_suggests_auth(brief: str) -> bool:
    lower = (brief or "").lower()
    markers = (
        "bearer",
        "authorization:",
        "authorization header",
        "api token",
        "flow_api_token",
        "static bearer",
        "shared secret",
        "401",
    )
    return any(m in lower for m in markers)


def _prompt_suggests_quick_replies(brief: str) -> bool:
    lower = (brief or "").lower()
    return any(m in lower for m in ("quickreplies", "quick replies", "quick-replies", "quickreply"))


def _coerce_kv_items(raw: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or entry.get("name") or "").strip()
        if not key:
            continue
        items.append(
            {
                "key": key,
                "value": str(entry.get("value") or entry.get("val") or ""),
                "enabled": entry.get("enabled", True) is not False,
            }
        )
    return items


def _coerce_button_capture(raw: Any) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return rules
    for rule in raw:
        if not isinstance(rule, dict):
            continue
        field = rule.get("field") or rule.get("setField")
        if not field:
            continue
        normalized = {
            "matchType": str(rule.get("matchType") or rule.get("match") or "exact").lower(),
            "field": str(field),
        }
        if "setValue" in rule:
            normalized["setValue"] = rule.get("setValue")
        if rule.get("valueFrom") or rule.get("from"):
            normalized["valueFrom"] = "button_id"
        if rule.get("value") is not None or rule.get("equals") is not None:
            normalized["value"] = str(rule.get("value") or rule.get("equals"))
        if rule.get("pattern"):
            normalized["pattern"] = str(rule.get("pattern"))
        rules.append(normalized)
    return rules


def _finalize_api_node_data(
    data: Dict[str, Any],
    *,
    brief: str,
    raw_node: Dict[str, Any],
) -> Dict[str, Any]:
    """Ensure API nodes have headers, output mapping, and body type when inferrable."""
    method = (data.get("method") or "GET").upper()
    body = str(data.get("body") or "").strip()
    headers = _coerce_kv_items(data.get("headers") or raw_node.get("headers"))
    has_auth_header = any(
        str(h.get("key", "")).lower() == "authorization" for h in headers
    )
    has_content_type = any(
        str(h.get("key", "")).lower() == "content-type" for h in headers
    )

    needs_auth = _prompt_suggests_auth(brief) or raw_node.get("requires_auth") is True
    if needs_auth and not has_auth_header:
        headers.insert(0, dict(_DEFAULT_AUTH_HEADERS[0]))
    if method in {"POST", "PUT", "PATCH"} and body and not has_content_type:
        headers.append(dict(_DEFAULT_AUTH_HEADERS[1]))
    if headers:
        data["headers"] = headers

    query_params = _coerce_kv_items(data.get("queryParams") or raw_node.get("query_params"))
    if query_params:
        data["queryParams"] = query_params

    body_type = str(data.get("bodyType") or raw_node.get("body_type") or "").lower()
    if not body_type or body_type == "none":
        if body:
            data["bodyType"] = "json"
        else:
            data["bodyType"] = "none"
    if not data.get("timeoutSec"):
        data["timeoutSec"] = 20
    if not data.get("responseFormat"):
        data["responseFormat"] = "auto"

    output = data.get("output") if isinstance(data.get("output"), dict) else {}
    on_success = output.get("onSuccess") if isinstance(output.get("onSuccess"), dict) else {}
    if _prompt_suggests_quick_replies(brief) or raw_node.get("returns_quick_replies") is True:
        on_success.setdefault("mode", "auto")
        on_success.setdefault("textPath", "message")
        on_success.setdefault("buttonsPath", "quickReplies")
    on_success.setdefault("fallbackText", on_success.get("fallbackText") or "Done.")
    on_error = output.get("onError") if isinstance(output.get("onError"), dict) else {}
    on_error.setdefault("text", on_error.get("text") or "Service unavailable. Please try again.")
    data["output"] = {"onSuccess": on_success, "onError": on_error}

    capture = _coerce_button_capture(
        data.get("buttonCapture") or raw_node.get("button_capture") or raw_node.get("buttonCapture")
    )
    if capture:
        data["buttonCapture"] = capture

    return data


def _infer_flow_variables(parsed: Dict[str, Any], brief: str) -> Dict[str, Any]:
    variables = parsed.get("variables") if isinstance(parsed.get("variables"), dict) else {}
    variables = dict(variables)
    if _prompt_suggests_auth(brief) and not variables.get("flow_api_token"):
        variables["flow_api_token"] = ""
    return variables


def _infer_flow_config(parsed: Dict[str, Any]) -> Dict[str, Any]:
    cfg = parsed.get("flow_config") or parsed.get("flowConfig")
    if isinstance(cfg, dict):
        return dict(cfg)
    defaults = parsed.get("variable_defaults") or parsed.get("variableDefaults")
    if isinstance(defaults, dict):
        return {"variableDefaults": defaults}
    return {}


def _add_quick_reply_router(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    routes: List[Dict[str, Any]],
    id_map: Dict[str, str],
) -> None:
    if not routes:
        return
    hub_id = _new_node_id("qr_hub")
    nodes.append(
        {
            "id": hub_id,
            "type": "message",
            "position": {"x": 0, "y": 0},
            "data": {
                "label": "Quick-reply router",
                "body": "(internal routing hub — not shown to users)",
                "interactiveType": "none",
                "buttons": [],
                "sections": [],
            },
        }
    )
    for route in routes:
        if not isinstance(route, dict):
            continue
        button_id = route.get("button_id") or route.get("id")
        pattern = route.get("button_id_pattern") or route.get("pattern")
        next_tid = str(route.get("next_temp_id") or route.get("target_temp_id") or "").strip()
        target = id_map.get(next_tid)
        if not target:
            continue
        handle = str(button_id or pattern or "").strip()
        if not handle:
            continue
        edges.append(
            {
                "id": _edge_id(hub_id, handle, target),
                "source": hub_id,
                "sourceHandle": handle,
                "target": target,
                "targetHandle": "input",
            }
        )


def _normalize_draft(parsed: Dict[str, Any], *, brief: str = "") -> Dict[str, Any]:
    raw_nodes = parsed.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("Model did not return any flow nodes")

    name = str(parsed.get("name") or "AI Generated Flow").strip()[:100]
    description = str(parsed.get("description") or "").strip()[:500]
    trigger_type = str(parsed.get("trigger_type") or "any_reply").strip()
    if trigger_type not in {"any_reply", "keyword", "exact_match", "specific_template", "window_open"}:
        trigger_type = "any_reply"

    keywords_raw = parsed.get("keywords") or []
    keywords: List[str] = []
    if isinstance(keywords_raw, list):
        keywords = [str(k).strip() for k in keywords_raw if str(k).strip()][:20]

    id_map: Dict[str, str] = {}
    for idx, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        temp_id = str(raw.get("temp_id") or f"n{idx}").strip()
        id_map[temp_id] = _new_node_id()

    first_temp = str(parsed.get("first_node_temp_id") or "").strip()
    if not first_temp:
        first_raw = raw_nodes[0] if raw_nodes else {}
        first_temp = str((first_raw or {}).get("temp_id") or "n0")
    first_node_id = id_map.get(first_temp)

    nodes: List[Dict[str, Any]] = [
        {
            "id": "trigger-1",
            "type": "trigger",
            "position": {"x": 250, "y": 50},
            "data": {
                "triggerType": trigger_type,
                "keywords": keywords if trigger_type == "keyword" else [],
            },
        }
    ]
    edges: List[Dict[str, Any]] = []

    y = 220
    for idx, raw in enumerate(raw_nodes):
        if not isinstance(raw, dict):
            continue
        temp_id = str(raw.get("temp_id") or f"n{idx}").strip()
        node_id = id_map.get(temp_id)
        if not node_id:
            continue

        node_type = str(raw.get("type") or "message").strip().lower()
        if node_type not in _VALID_NODE_TYPES:
            node_type = "message"

        position = {"x": 250, "y": y}
        y += 200

        if node_type == "message":
            buttons: List[Dict[str, Any]] = []
            for bi, btn in enumerate((raw.get("buttons") or [])[:3]):
                if not isinstance(btn, dict):
                    continue
                label = str(btn.get("label") or f"Option {bi + 1}").strip()[:20]
                next_tid = str(btn.get("next_temp_id") or "").strip()
                target = id_map.get(next_tid)
                button_id = f"{node_id}_btn_{bi}"
                buttons.append(
                    {
                        "id": button_id,
                        "label": label or f"Option {bi + 1}",
                        "action": {"type": "quick_reply", "targetNodeId": target},
                    }
                )
                if target:
                    edges.append(
                        {
                            "id": _edge_id(node_id, button_id, target),
                            "source": node_id,
                            "sourceHandle": button_id,
                            "target": target,
                            "targetHandle": "input",
                        }
                    )

            nodes.append(
                {
                    "id": node_id,
                    "type": "message",
                    "position": position,
                    "data": {
                        "label": str(raw.get("label") or "Message")[:80],
                        "interactiveType": "button" if buttons else "none",
                        "body": str(raw.get("body") or "How can we help you today?")[:1024],
                        "buttons": buttons,
                        "sections": [],
                    },
                }
            )
            continue

        if node_type == "input":
            next_tid = str(raw.get("next_temp_id") or "").strip()
            target = id_map.get(next_tid)
            validation = str(raw.get("validation_type") or "text").strip().lower()
            if validation not in _VALID_INPUT_TYPES:
                validation = "text"

            nodes.append(
                {
                    "id": node_id,
                    "type": "input",
                    "position": position,
                    "data": {
                        "label": str(raw.get("label") or "Input")[:80],
                        "body": str(raw.get("body") or "Please reply:")[:1024],
                        "field": re.sub(r"[^a-zA-Z0-9_]", "_", str(raw.get("field") or "input"))[:40],
                        "validationType": validation,
                        "targetNodeId": target,
                    },
                }
            )
            if target:
                edges.append(
                    {
                        "id": _edge_id(node_id, "output", target),
                        "source": node_id,
                        "sourceHandle": "output",
                        "target": target,
                        "targetHandle": "input",
                    }
                )
            continue

        if node_type == "template":
            buttons_raw = raw.get("buttons") or []
            button_mappings: List[Dict[str, Any]] = []
            for bi, btn in enumerate(buttons_raw[:3]):
                if not isinstance(btn, dict):
                    continue
                next_tid = str(btn.get("next_temp_id") or "").strip()
                target = id_map.get(next_tid)
                label = str(btn.get("label") or f"Button {bi + 1}").strip()[:20]
                button_mappings.append(
                    {
                        "buttonIndex": bi,
                        "buttonText": label,
                        "buttonType": "quick_reply",
                        "targetNodeId": target,
                    }
                )
                if target:
                    edges.append(
                        {
                            "id": _edge_id(node_id, f"btn-{bi}", target),
                            "source": node_id,
                            "sourceHandle": f"btn-{bi}",
                            "target": target,
                            "targetHandle": "input",
                        }
                    )

            variables = raw.get("variables") if isinstance(raw.get("variables"), dict) else {}
            nodes.append(
                {
                    "id": node_id,
                    "type": "template",
                    "position": position,
                    "data": {
                        "templateName": str(raw.get("template_name") or raw.get("templateName") or "")[:128],
                        "templateLanguage": str(
                            raw.get("template_language") or raw.get("templateLanguage") or "en_US"
                        ),
                        "buttonMappings": button_mappings,
                        "variables": {str(k): str(v) for k, v in variables.items()},
                    },
                }
            )
            continue

        if node_type == "api":
            method = str(raw.get("method") or "POST").strip().upper()
            if method not in _VALID_API_METHODS:
                method = "POST"

            branches_raw = raw.get("branches") or []
            branches: List[Dict[str, Any]] = []
            for br in branches_raw:
                if not isinstance(br, dict):
                    continue
                bid = str(br.get("id") or f"branch_{len(branches)}").strip()
                branches.append(
                    {
                        "id": bid,
                        "path": str(br.get("path") or "status"),
                        "operator": str(br.get("operator") or "equals"),
                        "value": str(br.get("value") or ""),
                    }
                )
                next_tid = str(br.get("next_temp_id") or "").strip()
                target = id_map.get(next_tid)
                if target:
                    handle = f"branch-{bid}"
                    edges.append(
                        {
                            "id": _edge_id(node_id, handle, target),
                            "source": node_id,
                            "sourceHandle": handle,
                            "target": target,
                            "targetHandle": "input",
                        }
                    )

            # Default/output edge — for library pick (uuid) or text fallback; NOT auto-chain success
            default_tid = str(
                raw.get("default_next_temp_id")
                or raw.get("on_default_next")
                or raw.get("output_next_temp_id")
                or ""
            ).strip()
            default_target = id_map.get(default_tid)
            if default_target:
                edges.append(
                    {
                        "id": _edge_id(node_id, "output", default_target),
                        "source": node_id,
                        "sourceHandle": "output",
                        "target": default_target,
                        "targetHandle": "input",
                    }
                )

            error_tid = str(raw.get("on_error_next") or raw.get("error_next_temp_id") or "").strip()
            error_target = id_map.get(error_tid)
            if error_target:
                edges.append(
                    {
                        "id": _edge_id(node_id, "error", error_target),
                        "source": node_id,
                        "sourceHandle": "error",
                        "target": error_target,
                        "targetHandle": "input",
                    }
                )

            output_raw = raw.get("output_success") or raw.get("output") or {}
            if isinstance(output_raw, dict) and "onSuccess" in output_raw:
                output = output_raw
            else:
                output = {
                    "onSuccess": {
                        "mode": "auto",
                        "textPath": str(output_raw.get("textPath") or "message"),
                        "buttonsPath": str(output_raw.get("buttonsPath") or "quickReplies"),
                        "fallbackText": str(output_raw.get("fallbackText") or "Done."),
                    },
                    "onError": {
                        "text": str(
                            raw.get("error_text")
                            or (output_raw.get("onError") or {}).get("text")
                            or "Service unavailable. Please try again."
                        ),
                    },
                }

            api_data = {
                "label": str(raw.get("label") or "External API")[:80],
                "method": method,
                "url": str(raw.get("url") or "")[:2048],
                "headers": _coerce_kv_items(raw.get("headers")),
                "queryParams": _coerce_kv_items(raw.get("query_params") or raw.get("queryParams")),
                "bodyType": str(raw.get("body_type") or raw.get("bodyType") or "json"),
                "body": str(raw.get("body") or ""),
                "storeAs": str(raw.get("store_as") or raw.get("storeAs") or "")[:64] or None,
                "branches": branches,
                "output": output,
                "buttonCapture": _coerce_button_capture(
                    raw.get("button_capture") or raw.get("buttonCapture")
                ),
            }
            api_data = _finalize_api_node_data(api_data, brief=brief, raw_node=raw)

            nodes.append({"id": node_id, "type": "api", "position": position, "data": api_data})
            continue

        nodes.append(
            {
                "id": node_id,
                "type": "end",
                "position": position,
                "data": {
                    "label": str(raw.get("label") or "End")[:80],
                    "message": str(raw.get("message") or "Thank you for contacting us!")[:500],
                },
            }
        )

    if first_node_id:
        edges.insert(
            0,
            {
                "id": _edge_id("trigger-1", "output", first_node_id),
                "source": "trigger-1",
                "sourceHandle": "output",
                "target": first_node_id,
                "targetHandle": "input",
            },
        )

    routes_raw = parsed.get("quick_reply_routes") or parsed.get("quickReplyRoutes") or []
    if isinstance(routes_raw, list):
        _add_quick_reply_router(nodes, edges, routes_raw, id_map)

    trigger = {
        "type": trigger_type,
        "enabled": True,
        "keywords": keywords if trigger_type == "keyword" else [],
    }

    return {
        "name": name,
        "description": description,
        "trigger": trigger,
        "variables": _infer_flow_variables(parsed, brief),
        "flow_config": _infer_flow_config(parsed),
        "nodes": nodes,
        "edges": edges,
    }


_SYSTEM_PROMPT = """You design production-ready WhatsApp Business interactive chatbot flows for Sociovia.

The user may provide conversation scripts AND/OR REST API documentation. When API docs are present:
- Create one "api" node per documented endpoint (POST/GET as specified).
- Wire the full user journey: welcome → collect inputs → API calls → handle quick-reply buttons → booking/close.

CRITICAL — API nodes MUST be fully configured. Every api node needs ALL of:
1. method, url (full HTTPS URL including path)
2. headers array (REQUIRED when docs mention Bearer/auth):
   [
     {"key": "Authorization", "value": "Bearer {{flow_api_token}}", "enabled": true},
     {"key": "Content-Type", "value": "application/json", "enabled": true}
   ]
   NEVER put "Authorization: Bearer xxx" in the key field — key is "Authorization", value is "Bearer {{flow_api_token}}".
3. body_type: "json" and body string with {{placeholders}} for POST endpoints
4. store_as when response should be saved (e.g. search_result)
5. branches when API returns status codes in JSON (path "status", operator "equals", value "not_found", etc.)
6. default_next_temp_id — output edge for dynamic quick-reply picks (uuid buttons) or text fallback. Do NOT use on_success_next for API nodes that show quickReplies (user must click first).
7. button_capture rules when quickReplies return ids to reuse later:
   [{"matchType": "uuid", "field": "library_id", "valueFrom": "button_id"}]
8. returns_quick_replies: true when API response includes quickReplies array
9. output_success: {"textPath": "message", "buttonsPath": "quickReplies"}

Flow-level secrets (NOT env vars — stored in flow settings UI):
- Include top-level "variables": {"flow_api_token": ""} when auth is required (user fills token in builder).
- Include "flow_config": {"variableDefaults": {"demo_date": "tomorrow", "plan_type": "monthly"}} when docs mention defaults.

Dynamic quick-reply routing (when docs list quickReplies[].id values like demo_today, reserve, daily, monthly):
Include top-level quick_reply_routes:
[
  {"button_id": "demo_today", "next_temp_id": "n_demo_name"},
  {"button_id": "reserve", "next_temp_id": "n_reserve_name"},
  {"button_id": "daily", "next_temp_id": "n_reserve_name"},
  {"button_id": "view_plans", "next_temp_id": "n_plans"}
]
For library uuid picks from search, use default_next_temp_id on the search api node instead.

Built-in runtime placeholders: {{phone}}, {{contact_name}}, {{customer_name}}, {{last_button_clicked}}
User input fields: {{location}}, {{customer_name}}, etc. from input nodes.

OUTPUT: Return ONE JSON object only (no markdown):
{
  "name": "flow title",
  "description": "summary",
  "trigger_type": "keyword",
  "keywords": ["hi", "hello"],
  "first_node_temp_id": "n1",
  "variables": {"flow_api_token": ""},
  "flow_config": {"variableDefaults": {}},
  "quick_reply_routes": [],
  "nodes": [
    {
      "temp_id": "n1",
      "type": "message",
      "label": "Welcome",
      "body": "Hello! Share your location or PIN code."
    },
    {
      "temp_id": "n2",
      "type": "input",
      "label": "Location",
      "body": "Enter area or PIN:",
      "field": "location",
      "validation_type": "text",
      "next_temp_id": "n3"
    },
    {
      "temp_id": "n3",
      "type": "api",
      "label": "Search libraries",
      "method": "POST",
      "url": "https://api.example.com/flow/libraries/search",
      "requires_auth": true,
      "returns_quick_replies": true,
      "headers": [
        {"key": "Authorization", "value": "Bearer {{flow_api_token}}", "enabled": true},
        {"key": "Content-Type", "value": "application/json", "enabled": true}
      ],
      "body_type": "json",
      "body": "{\\"location\\": \\"{{location}}\\", \\"pin_code\\": \\"{{location}}\\"}",
      "store_as": "search_result",
      "button_capture": [{"matchType": "uuid", "field": "library_id", "valueFrom": "button_id"}],
      "branches": [{"id": "not_found", "path": "status", "operator": "equals", "value": "not_found", "next_temp_id": "n_notfound"}],
      "default_next_temp_id": "n_plans"
    },
    {
      "temp_id": "n4",
      "type": "end",
      "message": "Thank you!"
    }
  ]
}

Rules:
- Use 5–25 nodes for API-integrated flows; 3–15 for simple flows.
- Node types: message, input, template, api, end.
- WhatsApp max 3 quick-reply buttons per message node; labels ≤20 chars.
- input nodes: always set field + next_temp_id.
- api nodes: always set headers when auth documented; always map quickReplies output.
- keyword trigger when user says "Hi" or lists trigger words.
- Valid JSON only. Escape quotes inside body strings.

USER BRIEF:
{brief}
"""


def generate_interactive_flow_draft(
    *,
    prompt: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Generate a visual WhatsApp automation draft from a natural-language brief."""
    brief = (prompt or "").strip()
    if not brief:
        raise ValueError("prompt is required")
    if len(brief) > 24000:
        brief = brief[:24000] + "\n[…truncated]"

    client = get_genai_client()
    if client is None:
        raise RuntimeError(
            "AI is not configured. Set GOOGLE_GENAI_API_KEY on whatsapp-api or enable Vertex AI."
        )

    model_id = _resolve_generation_model(
        os.environ.get("TEXT_MODEL") or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    )
    ws_hint = f"Workspace context id: {workspace_id}\n" if workspace_id else ""
    system_prompt = _SYSTEM_PROMPT.replace("{brief}", f"{ws_hint}{brief}")

    response = client.models.generate_content(
        model=model_id,
        contents=system_prompt,
        config={"temperature": 0.2, "max_output_tokens": 16384},
    )
    raw = getattr(response, "text", "") or ""
    parsed = _extract_json_object(raw)
    if not isinstance(parsed, dict):
        raise ValueError("Model returned invalid structure")

    draft = _normalize_draft(parsed, brief=brief)
    draft["generated_at"] = int(time.time())
    return draft
