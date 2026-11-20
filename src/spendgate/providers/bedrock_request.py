"""OpenAI chat request -> Converse request (protocol 2.7: only the fields it lists).

Anything else is refused with `Untranslatable` naming the field; nothing is silently
dropped. Pure functions over parsed JSON; no HTTP, no botocore.
"""

import base64
import binascii
import json
from collections.abc import Mapping

from spendgate.providers.bedrock_stream import as_obj
from spendgate.providers.port import Json, Untranslatable

# Chat fields the protocol translates, plus fields the gateway itself sets or consumes.
_TRANSLATED = frozenset(
    {"model", "messages", "max_tokens", "max_completion_tokens", "temperature", "top_p", "stop"}
    | {"tools", "tool_choice", "response_format", "stream", "user", "stream_options", "store"}
)
_IMAGE_FORMATS = {"png": "png", "jpeg": "jpeg", "jpg": "jpeg", "gif": "gif", "webp": "webp"}


def _refuse(param: str, message: str) -> Untranslatable:
    return Untranslatable(f"{param}: {message}", param)


def _text_blocks(content: Json, param: str) -> list[Json]:
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        raise _refuse(param, "must be a string or a list of text parts")
    blocks: list[Json] = []
    for i, part in enumerate(content):
        obj = as_obj(part)
        if obj.get("type") != "text" or not isinstance(obj.get("text"), str):
            raise _refuse(f"{param}[{i}].type", "only text parts are supported here")
        blocks.append({"text": obj["text"]})
    return blocks


def _image_block(url: Json, param: str) -> Json:
    if not isinstance(url, str) or not url.startswith("data:image/") or ";base64," not in url:
        raise _refuse(param, "only base64 data: image URLs are supported")
    head, _, data = url.partition(";base64,")
    fmt = _IMAGE_FORMATS.get(head.removeprefix("data:image/").lower())
    try:
        base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise _refuse(param, "invalid base64 image data") from None
    if fmt is None:
        raise _refuse(param, "unsupported image format")
    return {"image": {"format": fmt, "source": {"bytes": data}}}


def _user_blocks(content: Json, param: str) -> list[Json]:
    if isinstance(content, str):
        return [{"text": content}]
    if not isinstance(content, list):
        raise _refuse(param, "must be a string or a list of parts")
    blocks: list[Json] = []
    for i, part in enumerate(content):
        obj, where = as_obj(part), f"{param}[{i}]"
        if obj.get("type") == "text" and isinstance(obj.get("text"), str):
            blocks.append({"text": obj["text"]})
        elif obj.get("type") == "image_url":
            blocks.append(
                _image_block(as_obj(obj.get("image_url")).get("url"), f"{where}.image_url")
            )
        else:
            raise _refuse(f"{where}.type", "only text and image_url parts are supported")
    return blocks


def _tool_use(call: Json, param: str) -> Json:
    obj, fn = as_obj(call), as_obj(as_obj(call).get("function"))
    call_id, name, args = obj.get("id"), fn.get("name"), fn.get("arguments", "{}")
    if obj.get("type") != "function" or not isinstance(call_id, str) or not isinstance(name, str):
        raise _refuse(param, "must be a function call with id and name")
    try:
        parsed = json.loads(args) if isinstance(args, str) else None
    except ValueError:
        parsed = None
    if not isinstance(parsed, Mapping):
        raise _refuse(f"{param}.function.arguments", "must be a JSON object string")
    return {"toolUse": {"toolUseId": call_id, "name": name, "input": parsed}}


def _message(msg: Json, i: int) -> tuple[str, list[Json]]:
    """One chat message as (Converse role, content blocks)."""
    obj, param = as_obj(msg), f"messages[{i}]"
    role, content = obj.get("role"), obj.get("content")
    if role == "user":
        return "user", _user_blocks(content, f"{param}.content")
    if role == "tool":
        call_id = obj.get("tool_call_id")
        if not isinstance(call_id, str):
            raise _refuse(f"{param}.tool_call_id", "is required")
        result: Json = {"toolUseId": call_id, "content": _text_blocks(content, f"{param}.content")}
        return "user", [{"toolResult": result}]
    if role == "assistant":
        blocks: list[Json] = []
        if content not in (None, "", []):
            blocks = _text_blocks(content, f"{param}.content")
        calls = obj.get("tool_calls") or []
        if not isinstance(calls, list):
            raise _refuse(f"{param}.tool_calls", "must be a list")
        blocks += [_tool_use(c, f"{param}.tool_calls[{j}]") for j, c in enumerate(calls)]
        if not blocks:
            raise _refuse(f"{param}.content", "an assistant message needs content or tool calls")
        return "assistant", blocks
    raise _refuse(f"{param}.role", "must be system, developer, user, assistant or tool")


def _conversation(messages: Json) -> tuple[list[Json], list[Json]]:
    """(system blocks, messages), merging same-role neighbours as Converse requires."""
    if not isinstance(messages, list) or not messages:
        raise _refuse("messages", "must be a non-empty list")
    system: list[Json] = []
    turns: list[tuple[str, list[Json]]] = []
    for i, msg in enumerate(messages):
        role = as_obj(msg).get("role")
        if role in ("system", "developer"):
            system += _text_blocks(as_obj(msg).get("content"), f"messages[{i}].content")
            continue
        turn_role, blocks = _message(msg, i)
        if turns and turns[-1][0] == turn_role:
            turns[-1][1].extend(blocks)
        else:
            turns.append((turn_role, blocks))
    return system, [{"role": r, "content": b} for r, b in turns]


def _number(body: Mapping[str, Json], key: str) -> float | int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _refuse(key, "must be a number")
    return value


def _inference_config(body: Mapping[str, Json]) -> dict[str, Json]:
    cfg: dict[str, Json] = {}
    limits = [
        v for k in ("max_tokens", "max_completion_tokens") if (v := _number(body, k)) is not None
    ]
    if limits:
        cfg["maxTokens"] = int(min(limits))  # both set: the smaller applies (protocol 2.2)
    for key, target in (("temperature", "temperature"), ("top_p", "topP")):
        if (value := _number(body, key)) is not None:
            cfg[target] = value
    stop = body.get("stop")
    if isinstance(stop, str):
        cfg["stopSequences"] = [stop]
    elif isinstance(stop, list) and all(isinstance(s, str) for s in stop):
        cfg["stopSequences"] = stop
    elif stop is not None:
        raise _refuse("stop", "must be a string or a list of strings")
    return cfg


def _tool_specs(tools: Json) -> list[Json]:
    if not isinstance(tools, list) or not tools:
        raise _refuse("tools", "must be a non-empty list")
    specs: list[Json] = []
    for i, tool in enumerate(tools):
        fn = as_obj(as_obj(tool).get("function"))
        name = fn.get("name")
        if as_obj(tool).get("type") != "function" or not isinstance(name, str):
            raise _refuse(f"tools[{i}]", "only function tools are supported")
        if fn.get("strict"):
            raise _refuse(f"tools[{i}].function.strict", "strict tool schemas are not supported")
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        spec: dict[str, Json] = {"name": name, "inputSchema": {"json": schema}}
        if isinstance(fn.get("description"), str):
            spec["description"] = fn["description"]
        specs.append({"toolSpec": spec})
    return specs


def _tool_choice(choice: Json) -> Json:
    if choice is None or choice == "auto":
        return {"auto": {}} if choice else None
    if choice == "required":
        return {"any": {}}
    picked = as_obj(as_obj(choice).get("function")).get("name")
    if as_obj(choice).get("type") != "function" or not isinstance(picked, str):
        raise _refuse("tool_choice", "must be auto, required, none or a function")
    return {"tool": {"name": picked}}


def _tool_config(body: Mapping[str, Json]) -> Json:
    tools, choice = body.get("tools"), body.get("tool_choice")
    if tools is None:
        if choice is not None:
            raise _refuse("tool_choice", "requires tools")
        return None
    if choice == "none":
        return None  # Converse has no "none"; sending no tools is the same thing
    config: dict[str, Json] = {"tools": _tool_specs(tools)}
    if (picked := _tool_choice(choice)) is not None:
        config["toolChoice"] = picked
    return config


def _output_config(fmt: Json) -> Json:
    if fmt is None:
        return None
    obj = as_obj(fmt)
    if obj.get("type") == "text":
        return None
    schema = as_obj(obj.get("json_schema"))
    if obj.get("type") != "json_schema" or not isinstance(schema.get("schema"), Mapping):
        raise _refuse("response_format", "only text and json_schema are supported")
    definition: dict[str, Json] = {"schema": json.dumps(schema["schema"])}
    for key in ("name", "description"):
        if isinstance(schema.get(key), str):
            definition[key] = schema[key]
    return {"textFormat": {"type": "json_schema", "structure": {"jsonSchema": definition}}}


def converse_body(body: Mapping[str, Json]) -> dict[str, Json]:
    """The Converse request for an allowlisted chat body; refuses everything not translated."""
    for key in body:
        if key not in _TRANSLATED:
            raise _refuse(key, "is not supported for Bedrock")
    if body.get("store") not in (None, False):
        raise _refuse("store", "must be false")
    system, messages = _conversation(body.get("messages"))
    out: dict[str, Json] = {"messages": messages}
    if system:
        out["system"] = system
    if cfg := _inference_config(body):
        out["inferenceConfig"] = cfg
    if (tools := _tool_config(body)) is not None:
        out["toolConfig"] = tools
    if (fmt := _output_config(body.get("response_format"))) is not None:
        out["outputConfig"] = fmt
    return out  # `user`, `stream`, `stream_options`: consumed by the gateway, not forwarded
