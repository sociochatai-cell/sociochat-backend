"""
GenAI Provider Bridge
=====================

Flag-gated seam that routes text-generation calls through either:
  - Google Gemini  (default, existing path)
  - OpenAI via the AI Gateway  (GENAI_TEXT_PROVIDER=openai)

Usage:
    from core.genai_bridge import generate_text, is_openai_mode

    response = generate_text(
        model="gemini-3.5-flash",
        contents="Hello, how are you?",
        config=GenerateContentConfig(max_output_tokens=256, temperature=0.1),
        gemini_client=client,            # existing genai.Client
        workspace_id=workspace_id,       # optional, for identity headers
    )
    text = response.text   # works identically in both modes

Set env vars for OpenAI mode:
    GENAI_TEXT_PROVIDER=openai
    OPENAI_BASE_URL=http://ai-gateway:8080/v1
    OPENAI_API_KEY=sk-gw-...
"""

import os
import logging
from typing import Optional, Any, List, Union
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model mapping: Gemini -> OpenAI
# ---------------------------------------------------------------------------
_GEMINI_TO_OPENAI = {
    "gemini-3.5-flash": "gpt-4o-mini",
    "gemini-3.1-flash-lite": "gpt-4o-mini",
    "gemini-3.1-pro-preview": "gpt-4o",
    "gemini-2.0-flash": "gpt-4o-mini",
    "gemini-2.0-flash-001": "gpt-4o-mini",
    "gemini-1.5-flash": "gpt-4o-mini",
    "gemini-1.5-pro": "gpt-4o",
    "gemini-pro": "gpt-4o-mini",
}

_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def _text_provider() -> str:
    return (os.environ.get("GENAI_TEXT_PROVIDER") or "gemini").strip().lower()


def is_openai_mode() -> bool:
    return _text_provider() == "openai"


# ---------------------------------------------------------------------------
# OpenAI client singleton
# ---------------------------------------------------------------------------
_openai_client = None


def _get_openai_client():
    global _openai_client
    if _openai_client is not None:
        return _openai_client

    try:
        from openai import OpenAI
    except ImportError:
        logger.error("openai package not installed — pip install openai")
        return None

    base_url = os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE")
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        logger.error("OPENAI_API_KEY not set — cannot use OpenAI mode")
        return None

    _openai_client = OpenAI(base_url=base_url, api_key=api_key)
    logger.info("OpenAI client initialized (base_url=%s)", base_url)
    return _openai_client


def _map_model(gemini_model: str) -> str:
    return _GEMINI_TO_OPENAI.get(gemini_model, _DEFAULT_OPENAI_MODEL)


# ---------------------------------------------------------------------------
# Unified response wrapper
# ---------------------------------------------------------------------------
@dataclass
class BridgeResponse:
    """Mimics the subset of google.genai response that callers use."""
    text: Optional[str] = None
    raw: Any = None


# ---------------------------------------------------------------------------
# Core: generate_text
# ---------------------------------------------------------------------------
def generate_text(
    model: str,
    contents: Any,
    config: Any = None,
    gemini_client: Any = None,
    workspace_id: Optional[str] = None,
    user_id: Optional[str] = None,
    feature: Optional[str] = None,
) -> BridgeResponse:
    """Generate text via Gemini or OpenAI depending on GENAI_TEXT_PROVIDER.

    Returns a BridgeResponse with .text (always a string or None).
    """
    if is_openai_mode():
        return _generate_openai(
            model=model,
            contents=contents,
            config=config,
            workspace_id=workspace_id,
            user_id=user_id,
            feature=feature,
        )
    else:
        return _generate_gemini(
            model=model,
            contents=contents,
            config=config,
            gemini_client=gemini_client,
        )


# ---------------------------------------------------------------------------
# Gemini path (existing behaviour, zero change)
# ---------------------------------------------------------------------------
def _generate_gemini(model, contents, config, gemini_client) -> BridgeResponse:
    if gemini_client is None:
        return BridgeResponse(text=None)

    kwargs = {"model": model, "contents": contents}
    if config is not None:
        kwargs["config"] = config

    response = gemini_client.models.generate_content(**kwargs)
    return BridgeResponse(text=getattr(response, "text", None), raw=response)


# ---------------------------------------------------------------------------
# OpenAI path (via AI Gateway)
# ---------------------------------------------------------------------------
def _generate_openai(model, contents, config, workspace_id=None,
                     user_id=None, feature=None) -> BridgeResponse:
    client = _get_openai_client()
    if client is None:
        return BridgeResponse(text=None)

    openai_model = _map_model(model)

    # Build messages from contents
    messages = _contents_to_messages(contents)

    # Extract config params
    kwargs = {}
    system_instruction = None
    if config is not None:
        if hasattr(config, "max_output_tokens") and config.max_output_tokens:
            kwargs["max_tokens"] = config.max_output_tokens
        if hasattr(config, "temperature") and config.temperature is not None:
            kwargs["temperature"] = config.temperature
        if hasattr(config, "response_mime_type") and config.response_mime_type == "application/json":
            kwargs["response_format"] = {"type": "json_object"}
        if hasattr(config, "system_instruction") and config.system_instruction:
            system_instruction = config.system_instruction

    # Identity headers for gateway logging
    extra_headers = {}
    if workspace_id:
        extra_headers["x-workspace-id"] = str(workspace_id)
    if user_id:
        extra_headers["x-user-id"] = str(user_id)
    if feature:
        extra_headers["x-feature"] = str(feature)

    if system_instruction:
        messages.insert(0, {"role": "system", "content": str(system_instruction)})

    try:
        response = client.chat.completions.create(
            model=openai_model,
            messages=messages,
            extra_headers=extra_headers if extra_headers else None,
            **kwargs,
        )
        text = response.choices[0].message.content if response.choices else None
        return BridgeResponse(text=text, raw=response)
    except Exception as e:
        logger.error("OpenAI generate_text failed: %s", e)
        return BridgeResponse(text=None)


def _convert_dict_content(item: dict, messages: list):
    """Convert a Gemini-format dict (role+parts) or OpenAI-format dict to OpenAI messages."""
    if "parts" in item:
        # Gemini dict format: {"role": "model"/"user", "parts": [{"text": "..."}]}
        role = item.get("role", "user")
        role = "assistant" if role == "model" else role
        texts = []
        for p in item["parts"]:
            if isinstance(p, dict) and p.get("text"):
                texts.append(p["text"])
            elif hasattr(p, "text") and p.text:
                texts.append(p.text)
        if texts:
            messages.append({"role": role, "content": "\n".join(texts)})
    elif "content" in item or "role" in item:
        # Already OpenAI format — just fix role
        msg = dict(item)
        if msg.get("role") == "model":
            msg["role"] = "assistant"
        messages.append(msg)
    else:
        messages.append({"role": "user", "content": str(item)})


def _contents_to_messages(contents: Any) -> list:
    """Convert Gemini-style contents to OpenAI messages format."""
    if isinstance(contents, str):
        return [{"role": "user", "content": contents}]

    if isinstance(contents, list):
        messages = []
        for item in contents:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
            elif isinstance(item, dict):
                _convert_dict_content(item, messages)
            elif hasattr(item, "role") and hasattr(item, "parts"):
                role = "assistant" if item.role == "model" else item.role
                text_parts = []
                for p in (item.parts or []):
                    if hasattr(p, "text") and p.text:
                        text_parts.append(p.text)
                if text_parts:
                    messages.append({"role": role, "content": "\n".join(text_parts)})
            else:
                messages.append({"role": "user", "content": str(item)})
        return messages if messages else [{"role": "user", "content": ""}]

    return [{"role": "user", "content": str(contents)}]


# ---------------------------------------------------------------------------
# Agentic (function-calling) support
# ---------------------------------------------------------------------------

@dataclass
class _FunctionCall:
    """Mimics Gemini's Part.function_call so the agentic loop reads it unchanged."""
    name: str
    args: dict

@dataclass
class _Part:
    function_call: Optional[_FunctionCall] = None
    text: Optional[str] = None

@dataclass
class _Content:
    role: str
    parts: List[Any]

@dataclass
class _Candidate:
    content: _Content

@dataclass
class AgenticResponse:
    """Mimics the Gemini response shape the agentic loop expects:
       response.text, response.candidates[0].content.parts[].function_call
    """
    text: Optional[str] = None
    candidates: Optional[List[Any]] = None
    raw: Any = None


def _gemini_tools_to_openai(tools: list) -> list:
    """Convert Gemini Tool(function_declarations=[FunctionDeclaration(...)]) to
    OpenAI tools=[{type:"function", function:{name, description, parameters}}]."""
    openai_tools = []
    for tool in tools:
        for fd in (getattr(tool, "function_declarations", None) or []):
            params = getattr(fd, "parameters", None) or {"type": "object", "properties": {}}
            if isinstance(params, dict):
                fn_params = params
            else:
                fn_params = {"type": "object", "properties": {}}
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": fd.name,
                    "description": getattr(fd, "description", "") or "",
                    "parameters": fn_params,
                },
            })
    return openai_tools


def generate_agentic(
    model: str,
    contents: list,
    config: Any = None,
    tools: Optional[list] = None,
    gemini_client: Any = None,
    workspace_id: Optional[str] = None,
    feature: Optional[str] = None,
) -> AgenticResponse:
    """Agentic generation with function calling — Gemini or OpenAI.

    Returns an AgenticResponse with .text and .candidates matching
    the Gemini shape the agentic loop reads.
    """
    if is_openai_mode():
        return _agentic_openai(model, contents, config, tools,
                               workspace_id, feature)
    else:
        return _agentic_gemini(model, contents, config, tools, gemini_client)


def _agentic_gemini(model, contents, config, tools, gemini_client) -> AgenticResponse:
    if gemini_client is None:
        return AgenticResponse(text=None)
    kwargs = {"model": model, "contents": contents}
    if config is not None:
        kwargs["config"] = config
    response = gemini_client.models.generate_content(**kwargs)
    return response  # native Gemini response — the loop handles it natively


def _agentic_openai(model, contents, config, tools,
                    workspace_id=None, feature=None) -> AgenticResponse:
    client = _get_openai_client()
    if client is None:
        return AgenticResponse(text=None)

    openai_model = _map_model(model)

    # Build messages from the conversation history
    messages = _agentic_contents_to_messages(contents)

    # System instruction
    if config and hasattr(config, "system_instruction") and config.system_instruction:
        messages.insert(0, {"role": "system", "content": str(config.system_instruction)})

    kwargs = {}
    if config:
        if hasattr(config, "max_output_tokens") and config.max_output_tokens:
            kwargs["max_tokens"] = config.max_output_tokens
        if hasattr(config, "temperature") and config.temperature is not None:
            kwargs["temperature"] = config.temperature

    # Convert tools
    if tools:
        openai_tools = _gemini_tools_to_openai(tools)
        if openai_tools:
            kwargs["tools"] = openai_tools

    extra_headers = {}
    if workspace_id:
        extra_headers["x-workspace-id"] = str(workspace_id)
    if feature:
        extra_headers["x-feature"] = str(feature)

    try:
        response = client.chat.completions.create(
            model=openai_model,
            messages=messages,
            extra_headers=extra_headers if extra_headers else None,
            **kwargs,
        )
    except Exception as e:
        logger.error("OpenAI agentic call failed: %s", e)
        return AgenticResponse(text=None)

    choice = response.choices[0] if response.choices else None
    if not choice:
        return AgenticResponse(text=None, raw=response)

    msg = choice.message

    # If model returned tool calls, build Gemini-shaped response
    if msg.tool_calls:
        parts = []
        for tc in msg.tool_calls:
            import json as _json
            try:
                args = _json.loads(tc.function.arguments) if tc.function.arguments else {}
            except Exception:
                args = {}
            fc = _FunctionCall(name=tc.function.name, args=args)
            part = _Part(function_call=fc)
            part._tool_call_id = tc.id  # preserve OpenAI's unique ID for response matching
            parts.append(part)
        content = _Content(role="model", parts=parts)
        return AgenticResponse(
            text=None,
            candidates=[_Candidate(content=content)],
            raw=response,
        )

    # Plain text response
    return AgenticResponse(
        text=msg.content,
        candidates=[_Candidate(content=_Content(role="model", parts=[_Part(text=msg.content)]))],
        raw=response,
    )


def _agentic_contents_to_messages(contents: list) -> list:
    """Convert the agentic loop's contents list (mix of dicts, Content objects,
    and function-response Content objects) into OpenAI messages."""
    messages = []
    for item in contents:
        if isinstance(item, dict):
            _convert_dict_content(item, messages)
        elif isinstance(item, _Content):
            # Our own wrapper from a previous OpenAI turn
            _convert_bridge_content(item, messages)
        elif hasattr(item, "role") and hasattr(item, "parts"):
            # Gemini Content object — shouldn't appear in OpenAI mode but handle anyway
            _convert_gemini_content(item, messages)
        elif isinstance(item, str):
            messages.append({"role": "user", "content": item})
        else:
            messages.append({"role": "user", "content": str(item)})
    return messages


def _convert_bridge_content(content, messages):
    """Convert our _Content wrapper into OpenAI messages."""
    role = "assistant" if content.role == "model" else content.role
    # Check for function calls (assistant tool_calls)
    tool_calls = []
    texts = []
    for p in (content.parts or []):
        if isinstance(p, _Part) and p.function_call:
            import json as _json
            tc_id = getattr(p, "_tool_call_id", None) or f"call_{p.function_call.name}"
            tool_calls.append({
                "id": tc_id,
                "type": "function",
                "function": {
                    "name": p.function_call.name,
                    "arguments": _json.dumps(p.function_call.args),
                },
            })
        elif isinstance(p, _Part) and p.text:
            texts.append(p.text)
    if tool_calls:
        messages.append({
            "role": "assistant",
            "content": "\n".join(texts) if texts else None,
            "tool_calls": tool_calls,
        })
    elif texts:
        messages.append({"role": role, "content": "\n".join(texts)})


def _convert_gemini_content(content, messages):
    """Convert a Gemini Content object into OpenAI messages."""
    role = "assistant" if content.role == "model" else content.role
    texts = []
    func_responses = []
    tool_calls = []
    for p in (content.parts or []):
        if hasattr(p, "function_call") and p.function_call:
            import json as _json
            tool_calls.append({
                "id": f"call_{p.function_call.name}",
                "type": "function",
                "function": {
                    "name": p.function_call.name,
                    "arguments": _json.dumps(dict(p.function_call.args or {})),
                },
            })
        elif hasattr(p, "function_response") and p.function_response:
            import json as _json
            func_responses.append({
                "name": p.function_response.name,
                "content": _json.dumps(p.function_response.response or {}),
            })
        elif hasattr(p, "text") and p.text:
            texts.append(p.text)

    if tool_calls:
        messages.append({
            "role": "assistant",
            "content": "\n".join(texts) if texts else None,
            "tool_calls": tool_calls,
        })
    elif func_responses:
        for fr in func_responses:
            messages.append({
                "role": "tool",
                "tool_call_id": f"call_{fr['name']}",
                "content": fr["content"],
            })
    elif texts:
        messages.append({"role": role, "content": "\n".join(texts)})


def build_function_response_content(tool_name: str, result: dict,
                                    tool_call_id: Optional[str] = None) -> dict:
    """Build an OpenAI-format tool response message for the agentic loop.
    In OpenAI mode, the loop appends this instead of Part.from_function_response."""
    import json as _json
    return {
        "role": "tool",
        "tool_call_id": tool_call_id or f"call_{tool_name}",
        "content": _json.dumps(result),
    }


# ---------------------------------------------------------------------------
# Embedding support
# ---------------------------------------------------------------------------
_GEMINI_EMBED_TO_OPENAI = {
    "gemini-embedding-001": "text-embedding-3-small",
    "text-embedding-004": "text-embedding-3-small",
}

_TASK_TYPE_MAP = {
    "RETRIEVAL_DOCUMENT": "RETRIEVAL_DOCUMENT",
    "RETRIEVAL_QUERY": "RETRIEVAL_QUERY",
}


def embed_content(
    model: str,
    contents: str,
    config: Any = None,
    gemini_client: Any = None,
    workspace_id: Optional[str] = None,
    feature: Optional[str] = None,
    dimensions: int = 768,
) -> List[float]:
    """Generate embeddings via Gemini or OpenAI depending on GENAI_TEXT_PROVIDER.

    Returns a list of floats (the embedding vector).
    In OpenAI mode, uses text-embedding-3-small with matching dimensions
    so existing Qdrant vectors stay compatible.
    """
    if is_openai_mode():
        return _embed_openai(model, contents, workspace_id, feature, dimensions)
    else:
        return _embed_gemini(model, contents, config, gemini_client)


def _embed_gemini(model, contents, config, gemini_client) -> List[float]:
    if gemini_client is None:
        raise RuntimeError("GenAI client not initialized")
    result = gemini_client.models.embed_content(
        model=model, contents=contents, config=config,
    )
    return result.embeddings[0].values


def _embed_openai(model, contents, workspace_id, feature, dimensions) -> List[float]:
    client = _get_openai_client()
    if client is None:
        raise RuntimeError("OpenAI client not initialized for embeddings")

    openai_model = _GEMINI_EMBED_TO_OPENAI.get(model, "text-embedding-3-small")

    extra_headers = {}
    if workspace_id:
        extra_headers["x-workspace-id"] = str(workspace_id)
    if feature:
        extra_headers["x-feature"] = str(feature)

    response = client.embeddings.create(
        model=openai_model,
        input=contents,
        dimensions=dimensions,
        extra_headers=extra_headers if extra_headers else None,
    )
    return response.data[0].embedding


# ---------------------------------------------------------------------------
# Image generation support
# ---------------------------------------------------------------------------

def generate_image(
    prompt: str,
    count: int = 1,
    size: str = "1024x1792",
    workspace_id: Optional[str] = None,
    feature: Optional[str] = None,
) -> list:
    """Generate images via OpenAI DALL-E 3 through the gateway.

    Returns a list of dicts: [{"url": "...", "revised_prompt": "..."}]
    DALL-E 3 only supports n=1 per call, so we loop for count > 1.
    """
    client = _get_openai_client()
    if client is None:
        raise RuntimeError("OpenAI client not initialized for image generation")

    extra_headers = {}
    if workspace_id:
        extra_headers["x-workspace-id"] = str(workspace_id)
    if feature:
        extra_headers["x-feature"] = str(feature)

    results = []
    for _ in range(min(count, 4)):
        try:
            response = client.images.generate(
                model="dall-e-3",
                prompt=prompt,
                n=1,
                size=size,
                quality="standard",
                extra_headers=extra_headers if extra_headers else None,
            )
            for img in response.data:
                results.append({
                    "url": img.url,
                    "revised_prompt": getattr(img, "revised_prompt", None),
                })
        except Exception as e:
            logger.error("OpenAI image generation failed: %s", e)
            if not results:
                raise
    return results
