# ruff: noqa: PLR0912
"""Target 5: the request allowlists (would have caught SR-04).

Under test: `openai_shapes.validate` / `check` (chat, Responses, embeddings, transcription, speech),
`AnthropicProvider.validate`, the Bedrock request builder (`converse_body`) and the Google request
builder (`chat_to_gemini`).

Inputs: mostly-valid nested bodies, then mutated: media parts with every URL spelling (remote,
`file-` ids, protocol-relative, upper-case `DATA:`, other schemes), file / audio / document parts,
extra keys at random depth, content lists inside `function_call_output.output`, tool-result lists,
and free-form subtrees nested up to the interpreter's JSON limit.

Invariants: no accepted body carries a non-`data:` URL or a file reference in a media position
(unless `remote_ok` for the OpenAI shapes), nor inline media that is not a png, jpeg, webp or gif
image (M-14, even with `remote_ok`); only `Untranslatable` / `Refusal` are ever raised; an
OpenAI body that `validate` accepts also translates, and the rebuilt upstream body is clean too; the
Bedrock and Google bodies built from a chat body contain inline bytes only.
"""

import copy
import json
from collections.abc import Mapping
from typing import Any

from hypothesis import strategies as st

from spendgate.core.errors import Refusal
from spendgate.providers import openai_shapes as openai
from spendgate.providers.anthropic import PATH as ANTHROPIC_PATH
from spendgate.providers.anthropic import AnthropicProvider
from spendgate.providers.bedrock_request import converse_body
from spendgate.providers.google import chat_to_gemini
from spendgate.providers.openai import OpenAIProvider
from spendgate.providers.port import AuthMode, CleanRequest, Deployment, Untranslatable
from tests.fuzz.conftest import Case, run_fuzz

# --- the oracle: where a media reference may hide ----------------------------------------------
# Positional and independent of the validators: it knows where each API reads content parts (chat
# `messages[].content[]`, Responses `input[].content[]` and `input[].output[]`, Anthropic
# `messages[].content[]`, `system[]` and `tool_result.content[]`) and what a part may be there.

_ALLOWED_TYPES = {
    "chat": {"text", "image_url"},
    "responses": {"input_text", "output_text", "input_image"},
    "anthropic": {"text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking"},
}
_FILE_KEYS = frozenset(
    {
        "file_id",
        "file_url",
        "file_data",
        "file",
        "input_audio",
        "input_file",
        "source_url",
        "uri",
        "gcs_uri",
        "fileData",
        "fileUri",
        "s3Location",
        "document",
        "video",
        "audio",
    }
)
_FETCH_TYPES = frozenset(
    {"url", "file", "input_file", "input_audio", "document", "video", "file_url"}
)
# The only inline media the estimate prices (M-14); spelled out here, not imported.
_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")
_IMAGE_PREFIXES = tuple(f"data:{t};base64," for t in _IMAGE_TYPES)


def _direct(part: Mapping[str, Any], remote_ok: bool, path: str) -> list[str]:
    """The places a media part can name a resource: itself, its `image_url` object, its `source`."""
    found: list[str] = []
    for where, node in (
        (path, part),
        (path + ".image_url", part.get("image_url")),
        (path + ".source", part.get("source")),
    ):
        if not isinstance(node, Mapping):
            continue
        found += [f"{where}.{k}: file reference" for k in node if k in _FILE_KEYS]
        url = node.get("url")
        if isinstance(url, str) and not (url.startswith("data:") or remote_ok):
            found.append(f"{where}.url: non-data URL {url[:40]!r}")
        if isinstance(node.get("type"), str) and node["type"] in _FETCH_TYPES:
            found.append(f"{where}.type: {node['type']!r}")
    return found


def _part(kind: str, part: Any, remote_ok: bool, path: str) -> list[str]:  # noqa: ANN401
    if not isinstance(part, Mapping) or part.get("type") not in _ALLOWED_TYPES[kind]:
        return [
            f"{path}: part {part.get('type') if isinstance(part, Mapping) else part!r} accepted"
        ]
    found = _direct(part, remote_ok, path)
    if part["type"] in ("image_url", "input_image"):
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, Mapping) else image
        if not isinstance(url, str) or not (url.startswith("data:") or remote_ok):
            found.append(f"{path}: image without an inline data URL")
        elif url[:5].lower() == "data:" and not url.startswith(_IMAGE_PREFIXES):
            found.append(f"{path}: inline media that is not an image")
    if part["type"] == "image":
        source = part.get("source")
        if not isinstance(source, Mapping) or source.get("type") != "base64":
            found.append(f"{path}: image source is not base64")
        elif source.get("media_type") not in _IMAGE_TYPES:
            found.append(f"{path}: image media_type is not an image type")
    if part["type"] == "tool_result" and isinstance(part.get("content"), list):
        for i, inner in enumerate(part["content"]):
            found += _inner_anthropic(inner, f"{path}.content[{i}]")
    return found


def _inner_anthropic(part: Any, path: str) -> list[str]:  # noqa: ANN401
    if not isinstance(part, Mapping) or part.get("type") not in ("text", "image"):
        return [f"{path}: tool_result part {part!r:.40} accepted"]
    return _part("anthropic", part, False, path)


def _lists(*candidates: Any) -> list[tuple[str, list[Any]]]:  # noqa: ANN401
    return [(name, value) for name, value in candidates if isinstance(value, list)]


def violations(kind: str, body: Mapping[str, Any], *, remote_ok: bool = False) -> list[str]:
    """Every content part of an accepted `body` that would make a provider fetch something."""
    found: list[str] = []
    if kind == "chat":
        for i, message in enumerate(body.get("messages") or []):
            if isinstance(message, Mapping):
                for name, lst in _lists((f"messages[{i}].content", message.get("content"))):
                    found += [
                        v
                        for j, p in enumerate(lst)
                        for v in _part(kind, p, remote_ok, f"{name}[{j}]")
                    ]
    elif kind == "responses":
        for i, item in enumerate(body["input"] if isinstance(body.get("input"), list) else []):
            if isinstance(item, Mapping):
                for field in ("content", "output"):
                    for name, lst in _lists((f"input[{i}].{field}", item.get(field))):
                        found += [
                            v
                            for j, p in enumerate(lst)
                            for v in _part(kind, p, remote_ok, f"{name}[{j}]")
                        ]
    else:
        for i, message in enumerate(body.get("messages") or []):
            if isinstance(message, Mapping):
                for name, lst in _lists((f"messages[{i}].content", message.get("content"))):
                    found += [
                        v
                        for j, p in enumerate(lst)
                        for v in _part(kind, p, remote_ok, f"{name}[{j}]")
                    ]
        for name, lst in _lists(("system", body.get("system"))):
            found += [
                v for j, p in enumerate(lst) for v in _part(kind, p, remote_ok, f"{name}[{j}]")
            ]
    return found


def output_violations(node: Any, path: str = "$") -> list[str]:  # noqa: ANN401
    """The translated Bedrock / Gemini request: media may only be inline bytes."""
    found: list[str] = []
    if isinstance(node, list):
        for i, item in enumerate(node):
            found += output_violations(item, f"{path}[{i}]")
    elif isinstance(node, Mapping):
        for key, value in node.items():
            if key in (
                "text",
                "args",
                "response",
                "input",
                "functionDeclarations",
                "name",
                "toolUseId",
            ):
                continue
            if key in _FILE_KEYS or key in ("url", "image_url"):
                found.append(f"{path}.{key}")
            if key == "mimeType" and value not in _IMAGE_TYPES:
                found.append(f"{path}.mimeType: {value!r:.40}")
            found += output_violations(value, f"{path}.{key}")
    return found


# --- generators --------------------------------------------------------------------------------

_NASTY_URLS = [
    "http://169.254.169.254/latest/meta-data",
    "https://example.com/a.png",
    "file-abc123",
    "ftp://x/y",
    "//evil.test/x",
    "DATA:image/png;base64,AAAA",
    " data:image/png;base64,AAAA",
    "data:image/png;base64,AAAA",
    "data:image/jpeg;base64,/9j/4A==",
    "data:text/html,<script>",
    "data:video/mp4;base64,AAAA",
    "data:application/pdf;base64,AAAA",
    "data:audio/wav;base64,AAAA",
    "DATA:video/mp4;base64,AAAA",
    "javascript:alert(1)",
    "",
    "s3://bucket/key",
    "gs://bucket/obj",
    "\x00data:",
    "data:",
    "data:image/png;base64,",
    "data:image/png;base64," + "A" * 300,
    "https://x/?data:image/png",
    "data\n:image/png;base64,AA",
]
_URLS = (
    st.sampled_from(_NASTY_URLS)
    | st.text(max_size=30)
    | st.text(max_size=20).map(lambda s: "data:" + s)
)
_TEXT = st.text(max_size=20)


def _deep(depth: int, leaf: object = 1) -> object:
    text = "[" * depth + json.dumps(leaf) + "]" * depth
    return json.loads(text)


leaf = st.none() | st.booleans() | st.integers(-(2**40), 2**40) | st.floats(allow_nan=False) | _TEXT
junk = st.recursive(
    leaf,
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(_TEXT, inner, max_size=3),
    max_leaves=8,
)
free_form = st.one_of(
    junk, st.sampled_from([5, 200, 900]).map(_deep)
)  # nested as deep as json.loads allows


def mostly(valid: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    """Usually the valid shape, now and then something of the wrong type entirely."""
    return st.one_of(valid, valid, valid, valid, valid, valid, junk)


media_part = st.one_of(
    st.builds(
        lambda u, d: {"type": "image_url", "image_url": {"url": u, **d}},
        _URLS,
        st.fixed_dictionaries({}, optional={"detail": st.sampled_from(["low", "high", "auto", 5])}),
    ),
    st.builds(lambda u: {"type": "image_url", "image_url": u}, _URLS),
    st.builds(lambda u: {"type": "input_image", "image_url": u}, _URLS),
    st.builds(lambda u: {"type": "input_image", "image_url": {"url": u}}, _URLS),
    st.builds(
        lambda k, v: {"type": k, k: v},
        st.sampled_from(["input_file", "file", "input_audio", "document"]),
        junk,
    ),
    st.builds(lambda i: {"type": "input_file", "file_id": i}, _URLS),
    st.builds(lambda u: {"type": "input_file", "file_url": u}, _URLS),
    st.builds(lambda u: {"type": "input_file", "file_data": u}, _URLS),
    st.builds(lambda u: {"type": "image", "source": {"type": "url", "url": u}}, _URLS),
    st.builds(
        lambda u, m: {
            "type": "image",
            "source": {"type": "base64", "media_type": m, "data": u},
        },
        _URLS,
        st.sampled_from(["image/png", "image/png", "video/mp4", "application/pdf", "IMAGE/PNG"]),
    ),
    st.builds(lambda i: {"type": "image", "source": {"type": "file", "file_id": i}}, _URLS),
)
text_part = st.builds(
    lambda t, k: {"type": k, "text": t},
    _TEXT,
    st.sampled_from(["text", "input_text", "output_text"]),
)
part = st.one_of(text_part, text_part, text_part, media_part, media_part, junk)
parts = st.lists(part, max_size=4)
content = st.one_of(_TEXT, parts, parts, parts, st.none(), junk)
ROLES = st.sampled_from(
    ["system", "developer", "user", "user", "assistant", "tool", "function", "x"]
)


def _call() -> st.SearchStrategy[Any]:
    return st.fixed_dictionaries(
        {
            "id": _TEXT,
            "type": st.sampled_from(["function", "x"]),
            "function": st.fixed_dictionaries(
                {"name": _TEXT, "arguments": st.sampled_from(["{}", '{"a":1}', "[]", "nope", ""])}
            ),
        }
    )


chat_message = st.fixed_dictionaries(
    {"role": ROLES, "content": content},
    optional={
        "tool_calls": st.lists(_call() | junk, max_size=2),
        "tool_call_id": _TEXT,
        "name": _TEXT,
    },
)
_TOP_CHAT = {
    "stream": mostly(st.booleans()),
    "max_tokens": mostly(st.integers(1, 10**6)),
    "tools": mostly(
        st.lists(
            st.fixed_dictionaries(
                {
                    "type": st.just("function"),
                    "function": st.fixed_dictionaries({"name": _TEXT, "parameters": free_form}),
                }
            ),
            max_size=2,
        )
    ),
    "tool_choice": mostly(st.sampled_from(["auto", "none", "required"])),
    "response_format": mostly(
        st.fixed_dictionaries(
            {
                "type": st.sampled_from(["text", "json_schema", "json_object"]),
                "json_schema": st.fixed_dictionaries({"name": _TEXT, "schema": free_form}),
            }
        )
    ),
    "temperature": mostly(st.floats(0, 2)),
    "n": st.just(1) | junk,
    "store": st.just(False) | junk,
    "modalities": mostly(st.just(["text"])),
    "stop": mostly(st.lists(_TEXT, max_size=2) | _TEXT),
    "user": mostly(_TEXT),
    "metadata": junk,
    "audio": junk,
}
chat_body = st.one_of(
    st.fixed_dictionaries(
        {"model": _TEXT, "messages": st.lists(chat_message, min_size=1, max_size=4)}
    ),
    st.fixed_dictionaries(
        {"model": _TEXT, "messages": mostly(st.lists(chat_message, min_size=1, max_size=4))},
        optional=_TOP_CHAT,
    ),
)

response_item = st.one_of(
    st.fixed_dictionaries({"type": st.just("message"), "role": ROLES, "content": content}),
    st.fixed_dictionaries({"role": ROLES, "content": content}),
    st.fixed_dictionaries(
        {
            "type": st.just("function_call_output"),
            "call_id": _TEXT,
            "output": st.one_of(_TEXT, parts, parts, junk),
        }
    ),
    st.fixed_dictionaries(
        {"type": st.just("function_call_output"), "call_id": _TEXT, "output": parts}
    ),
    st.fixed_dictionaries(
        {"type": st.just("function_call"), "call_id": _TEXT, "name": _TEXT, "arguments": _TEXT}
    ),
    st.fixed_dictionaries({"type": st.just("reasoning"), "id": _TEXT, "summary": junk}),
    junk,
)
responses_body = st.one_of(
    st.fixed_dictionaries(
        {"model": _TEXT, "input": st.lists(response_item, min_size=1, max_size=4)}
    ),
    st.fixed_dictionaries(
        {"model": _TEXT, "input": mostly(_TEXT | st.lists(response_item, max_size=4))},
        optional={
            "instructions": mostly(_TEXT),
            "store": st.just(False) | junk,
            "stream": mostly(st.booleans()),
            "tools": mostly(
                st.lists(
                    st.fixed_dictionaries(
                        {"type": st.just("function"), "name": _TEXT, "parameters": free_form}
                    ),
                    max_size=2,
                )
            ),
            "text": mostly(st.fixed_dictionaries({"format": free_form})),
            "include": mostly(st.just(["reasoning.encrypted_content"])),
            "reasoning": junk,
            "metadata": junk,
        },
    ),
)

anthropic_block = st.one_of(
    st.fixed_dictionaries(
        {"type": st.just("text"), "text": _TEXT},
        optional={"citations": free_form, "cache_control": junk},
    ),
    media_part,
    st.fixed_dictionaries(
        {"type": st.just("tool_use"), "id": _TEXT, "name": _TEXT, "input": free_form}
    ),
    st.fixed_dictionaries(
        {
            "type": st.just("tool_result"),
            "tool_use_id": _TEXT,
            "content": st.one_of(
                _TEXT, st.lists(st.one_of(text_part, media_part, junk), max_size=3), junk
            ),
        }
    ),
    st.fixed_dictionaries(
        {
            "type": st.sampled_from(
                ["document", "thinking", "redacted_thinking", "server_tool_use"]
            ),
            "source": junk,
        }
    ),
    junk,
)
anthropic_body = st.fixed_dictionaries(
    {
        "model": _TEXT,
        "max_tokens": st.integers(1, 5000) | junk,
        "messages": st.lists(
            st.fixed_dictionaries(
                {
                    "role": st.sampled_from(["user", "assistant", "x"]),
                    "content": _TEXT | st.lists(anthropic_block, max_size=3) | junk,
                }
            ),
            min_size=1,
            max_size=3,
        )
        | junk,
    },
    optional={
        "system": _TEXT | st.lists(anthropic_block, max_size=2) | junk,
        "metadata": junk,
        "tools": st.lists(
            st.fixed_dictionaries({"name": _TEXT, "input_schema": free_form}), max_size=2
        )
        | junk,
        "stream": st.booleans() | junk,
        "temperature": st.floats(0, 1) | junk,
        "thinking": junk,
        "output_config": junk,
    },
)
_ANTHROPIC_HEADERS = st.fixed_dictionaries(
    {},
    optional={
        "anthropic-version": st.sampled_from(["2026-10-21", "2023-06-01", "x"]) | _TEXT,
        "Anthropic-Beta": st.sampled_from(["", "a,b", " ,"]) | _TEXT,
        "ANTHROPIC-VERSION": _TEXT,
    },
)


def _inject(body: dict[str, Any], picks: list[tuple[int, str, Any]]) -> dict[str, Any]:
    """Add one extra key at some dict nodes (the first few levels): the SR-04 shape."""
    nodes: list[dict[str, Any]] = []

    def walk(node: Any, depth: int) -> None:  # noqa: ANN401
        if depth > 6:
            return
        if isinstance(node, dict):
            nodes.append(node)
            for child in list(node.values()):
                walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                walk(child, depth + 1)

    walk(body, 0)
    for index, key, value in picks:
        nodes[index % len(nodes)][key] = value
    return body


def injected(bodies: st.SearchStrategy[dict[str, Any]]) -> st.SearchStrategy[dict[str, Any]]:
    extra_key = (
        st.sampled_from(
            [
                *sorted(_FILE_KEYS),
                "url",
                "image_url",
                "type",
                "source",
                "detail",
                "id",
                "content",
                "output",
            ]
        )
        | _TEXT
    )
    pick = st.tuples(st.integers(0, 50), extra_key, st.one_of(_URLS, junk, media_part))
    return st.builds(_inject, bodies, st.lists(pick, max_size=3))


# --- near-valid skeletons, then perturbed ------------------------------------------------------
# Random junk is refused at the first wrong key, so it never reaches the code behind it. These start
# from bodies the allowlist accepts and change a few nodes, which is how a position that nothing
# validates (SR-04) is found: the body stays acceptable except for the one media reference.

_DATA_IMG = "data:image/png;base64,iVBORw0KGgo="
_good_url = st.sampled_from([_DATA_IMG, "data:image/jpeg;base64,/9j/4A=="])
_good_roles = st.sampled_from(["system", "developer", "user", "assistant"])
_ok_text = st.text(min_size=1, max_size=15)


def _chat_parts() -> st.SearchStrategy[Any]:
    one = st.one_of(
        _ok_text.map(lambda t: {"type": "text", "text": t}),
        _good_url.map(lambda u: {"type": "image_url", "image_url": {"url": u, "detail": "low"}}),
    )
    return st.lists(one, min_size=1, max_size=3)


_good_chat_message = st.one_of(
    st.builds(lambda r, c: {"role": r, "content": c}, _good_roles, _ok_text | _chat_parts()),
    st.builds(lambda i, c: {"role": "tool", "tool_call_id": i, "content": c}, _ok_text, _ok_text),
    st.builds(
        lambda i: {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": i, "type": "function", "function": {"name": "f", "arguments": "{}"}}
            ],
        },
        _ok_text,
    ),
)
good_chat = st.builds(
    lambda m, extra: {"model": "m", "messages": m, **extra},
    st.lists(_good_chat_message, min_size=1, max_size=3),
    st.fixed_dictionaries(
        {},
        optional={
            "max_tokens": st.just(50),
            "stream": st.just(False),
            "temperature": st.just(0.5),
            "tools": st.just(
                [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
            ),
            "response_format": st.just(
                {"type": "json_schema", "json_schema": {"name": "s", "schema": {"type": "object"}}}
            ),
        },
    ),
)


def _resp_parts() -> st.SearchStrategy[Any]:
    one = st.one_of(
        _ok_text.map(lambda t: {"type": "input_text", "text": t}),
        _good_url.map(lambda u: {"type": "input_image", "image_url": u}),
    )
    return st.lists(one, min_size=1, max_size=3)


_good_item = st.one_of(
    st.builds(lambda r, c: {"role": r, "content": c}, _good_roles, _ok_text | _resp_parts()),
    st.builds(
        lambda r, c: {"type": "message", "role": r, "content": c}, _good_roles, _resp_parts()
    ),
    st.builds(
        lambda i, o: {"type": "function_call_output", "call_id": i, "output": o},
        _ok_text,
        _ok_text | _resp_parts(),
    ),
    st.builds(
        lambda i: {"type": "function_call", "call_id": i, "name": "f", "arguments": "{}"}, _ok_text
    ),
)
good_responses = st.builds(
    lambda i: {"model": "m", "input": i}, _ok_text | st.lists(_good_item, min_size=1, max_size=3)
)


def _anth_blocks() -> st.SearchStrategy[Any]:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }
    one = st.one_of(
        _ok_text.map(lambda t: {"type": "text", "text": t}),
        st.just(image),
        st.builds(lambda i: {"type": "tool_use", "id": i, "name": "f", "input": {}}, _ok_text),
        st.builds(
            lambda i, c: {"type": "tool_result", "tool_use_id": i, "content": c},
            _ok_text,
            _ok_text
            | st.lists(
                st.just(image) | _ok_text.map(lambda t: {"type": "text", "text": t}),
                min_size=1,
                max_size=2,
            ),
        ),
    )
    return st.lists(one, min_size=1, max_size=3)


good_anthropic = st.builds(
    lambda m, sysm: {"model": "m", "max_tokens": 50, "messages": m, **sysm},
    st.lists(
        st.builds(
            lambda r, c: {"role": r, "content": c},
            st.sampled_from(["user", "assistant"]),
            _ok_text | _anth_blocks(),
        ),
        min_size=1,
        max_size=3,
    ),
    st.fixed_dictionaries(
        {}, optional={"system": _ok_text | st.just([{"type": "text", "text": "s"}])}
    ),
)

_PERTURB = st.one_of(
    _URLS,
    junk,
    media_part,
    st.sampled_from(["text", "image_url", "input_image", "image", "base64", "url", "file"]),
)
_ADD_KEYS = (
    st.sampled_from(
        [
            *sorted(_FILE_KEYS),
            "url",
            "image_url",
            "source",
            "detail",
            "type",
            "content",
            "output",
            "role",
        ]
    )
    | _TEXT
)


def _slots(doc: Any) -> list[tuple[Any, Any]]:  # noqa: ANN401
    out: list[tuple[Any, Any]] = []

    def walk(node: Any, depth: int) -> None:  # noqa: ANN401
        if depth > 7:
            return
        items = (
            enumerate(node)
            if isinstance(node, list)
            else node.items()
            if isinstance(node, dict)
            else ()
        )
        for key, child in list(items):
            out.append((node, key))
            walk(child, depth + 1)

    walk(doc, 0)
    return out


def perturbed(skeleton: st.SearchStrategy[Any]) -> st.SearchStrategy[Any]:
    @st.composite
    def build(draw: st.DrawFn) -> Any:  # noqa: ANN401
        doc = copy.deepcopy(draw(skeleton))  # `just(...)` values are shared between examples
        for _ in range(draw(st.sampled_from([0, 1, 1, 1, 2, 3]))):
            slots = _slots(doc)
            if not slots:
                break
            container, key = slots[draw(st.integers(0, len(slots) - 1))]
            op = draw(st.sampled_from(["replace", "replace", "add", "delete", "wrap"]))
            if op == "replace":
                container[key] = draw(_PERTURB)
            elif op == "wrap":  # a list where a value was, or the value inside a list
                container[key] = [container[key], draw(_PERTURB)]
            elif op == "delete" and isinstance(container, dict):
                del container[key]
            else:
                target = container[key]
                if isinstance(target, dict):
                    target[draw(_ADD_KEYS)] = draw(_PERTURB)
                elif isinstance(target, list):
                    target.insert(draw(st.integers(0, len(target))), draw(_PERTURB))
        return doc

    return build()


# --- checks ------------------------------------------------------------------------------------

_DEPLOYMENT = Deployment(
    "openai", "https://api.example.test", "m", AuthMode.API_KEY, credential="k"
)  # pragma: allowlist secret
_CEILING = 64  # the output ceiling the pipeline would have held (M-15)


def check_openai(case: tuple[str, dict[str, Any], bool]) -> None:
    endpoint, body, remote_ok = case
    refusal = openai.validate(endpoint, body, {"file": b"x"}, remote_ok=remote_ok)
    assert refusal is None or isinstance(refusal, Refusal)
    try:
        openai.check(endpoint, body, {"file": b"x"}, remote_ok=remote_ok)
        checked = True
    except Untranslatable:
        checked = False
    assert checked == (refusal is None)
    if refusal is not None:
        return
    kind = {openai.CHAT: "chat", openai.RESPONSES: "responses"}.get(endpoint)
    bad = violations(kind, body, remote_ok=remote_ok) if kind else []
    assert not bad, f"accepted a body with a media reference outside the allowlist: {bad[:3]}"
    if endpoint != openai.TRANSCRIPTIONS:
        sent = OpenAIProvider(remote_ok=remote_ok).translate(
            CleanRequest(
                endpoint, "m", body, stream=body.get("stream") is True, output_ceiling=_CEILING
            ),
            _DEPLOYMENT,
        )
        rebuilt = json.loads(sent.body)
        if kind:
            assert not violations(kind, rebuilt, remote_ok=remote_ok), (
                "the rebuilt upstream body is not clean"
            )


def check_anthropic(case: tuple[dict[str, Any], dict[str, str]]) -> None:
    body, headers = case
    refusal = AnthropicProvider().validate(CleanRequest(ANTHROPIC_PATH, "m", body, headers=headers))
    assert refusal is None or isinstance(refusal, Refusal)
    if refusal is None:
        bad = violations("anthropic", body)
        assert not bad, f"accepted: {bad[:3]}"


def _translated(builder: Any, body: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ANN401
    try:
        out: dict[str, Any] = builder(body, _CEILING)
    except Untranslatable:
        return None
    return out


def check_bedrock(body: dict[str, Any]) -> None:
    out = _translated(converse_body, body)
    if out is None:
        return
    bad = output_violations({k: v for k, v in out.items() if k in ("messages", "system")})
    assert not bad, f"Bedrock body has a reference: {bad[:3]}"
    for message in out["messages"]:
        for block in message["content"]:
            if "image" in block:
                assert set(block["image"]["source"]) == {"bytes"}


def check_google(body: dict[str, Any]) -> None:
    out = _translated(chat_to_gemini, body)
    if out is None:
        return
    inline = {k: v for k, v in out.items() if k in ("contents", "systemInstruction")}
    assert not output_violations(inline), "Gemini body has a reference"
    for turn in out["contents"]:
        for p in turn["parts"]:
            assert set(p) <= {
                "text",
                "inlineData",
                "functionCall",
                "functionResponse",
                "thoughtSignature",
            }, p


_small_valid = st.one_of(
    st.tuples(
        st.just(openai.EMBEDDINGS),
        st.just({"model": "m", "input": ["a", "b"], "encoding_format": "float"}),
    ),
    st.tuples(
        st.just(openai.SPEECH),
        st.just({"model": "m", "input": "hello", "voice": "alloy", "response_format": "mp3"}),
    ),
    st.tuples(
        st.just(openai.TRANSCRIPTIONS),
        st.just({"model": "m", "language": "en", "timestamp_granularities[]": ["word"]}),
    ),
)

_ENDPOINT_BODIES = st.one_of(
    _small_valid.flatmap(
        lambda t: st.tuples(st.just(t[0]), perturbed(st.just(t[1])), st.booleans())
    ),
    st.tuples(st.just(openai.CHAT), perturbed(good_chat), st.booleans()),
    st.tuples(st.just(openai.CHAT), perturbed(good_chat), st.booleans()),
    st.tuples(st.just(openai.RESPONSES), perturbed(good_responses), st.booleans()),
    st.tuples(st.just(openai.RESPONSES), perturbed(good_responses), st.booleans()),
    st.tuples(st.just(openai.CHAT), injected(chat_body), st.booleans()),
    st.tuples(st.just(openai.RESPONSES), injected(responses_body), st.booleans()),
    st.tuples(
        st.sampled_from(sorted(openai.ENDPOINTS)),
        st.dictionaries(
            _TEXT
            | st.sampled_from(
                [
                    "model",
                    "input",
                    "voice",
                    "language",
                    "response_format",
                    "stream_format",
                    "timestamp_granularities[]",
                    "messages",
                ]
            ),
            junk,
            max_size=5,
        ),
        st.booleans(),
    ),
)


def test_fuzz_allowlists() -> None:
    run_fuzz(
        "allowlists",
        [
            Case("openai_validate_check_translate", _ENDPOINT_BODIES, check_openai),
            Case(
                "anthropic_validate",
                st.tuples(
                    st.one_of(
                        perturbed(good_anthropic),
                        perturbed(good_anthropic),
                        injected(anthropic_body),
                    ),
                    _ANTHROPIC_HEADERS,
                ),
                check_anthropic,
            ),
            Case(
                "bedrock_converse_body",
                st.one_of(perturbed(good_chat), perturbed(good_chat), injected(chat_body)),
                check_bedrock,
            ),
            Case(
                "google_chat_to_gemini",
                st.one_of(perturbed(good_chat), perturbed(good_chat), injected(chat_body)),
                check_google,
            ),
        ],
    )
