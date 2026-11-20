"""Hypothesis strategies built from the OpenAPI schemas of `docs/admin-openapi.yaml`.

schemathesis is not installed (and may not be added), so this is the small subset of JSON Schema the
admin spec uses: `$ref`, `allOf`, `type` (also a list with `null`), `enum`, `properties` /
`required`, `items`, string `pattern` / `minLength` / `maxLength` / `format: date-time`, and numeric
`minimum` / `maximum`. `valid` yields documents that satisfy the schema; `hostile` yields the same
documents with a few values swapped for boundary or wrong-typed ones, which is what finds crashes.
"""

from datetime import datetime
from typing import Any

from hypothesis import strategies as st

Schema = dict[str, Any]

_EDGE_NUMBERS = [0, 1, -1, 2**31, 2**32, 2**53, 2**63 - 1, 2**63, 2**64, 10**30, -(2**63), -(2**64)]
_EDGE_TEXT = [
    "",
    " ",
    "\x00",
    "a" * 600,
    "\u202e",
    "../..",
    "%00",
    "' OR 1=1 --",
    "{{x}}",
    "\U0001f600",
    "null",
    "\n",
    "\u00f1" * 130,
]

junk = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**70), 2**70)
    | st.floats()
    | st.text(max_size=20)
    | st.sampled_from(_EDGE_TEXT),
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=6,
)

_DATES = st.datetimes(
    min_value=datetime(1, 1, 1),
    max_value=datetime(9999, 12, 31, 23, 59, 59),
    timezones=st.timezones(),
).map(lambda d: d.isoformat())


def _resolve(schema: Schema, spec: Schema) -> Schema:
    while "$ref" in schema:
        node: Any = spec
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def _merge(schema: Schema, spec: Schema) -> Schema:
    schema = _resolve(schema, spec)
    if "allOf" not in schema:
        return schema
    merged: Schema = {k: v for k, v in schema.items() if k != "allOf"}
    for member in schema["allOf"]:
        part = _merge(member, spec)
        merged["properties"] = {**merged.get("properties", {}), **part.get("properties", {})}
        merged["required"] = [*merged.get("required", []), *part.get("required", [])]
        for key in ("type", "additionalProperties"):
            if key in part:
                merged[key] = part[key]
    return merged


def valid(schema: Schema, spec: Schema, depth: int = 0) -> st.SearchStrategy[Any]:
    """Documents that satisfy `schema` (all optional properties may be present or absent)."""
    schema = _merge(schema, spec)
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else list(kinds or [])
    if "enum" in schema:
        return st.sampled_from(schema["enum"])
    options: list[st.SearchStrategy[Any]] = []
    for kind in kinds or ["object" if "properties" in schema else "string"]:
        if kind == "null":
            options.append(st.none())
        elif kind == "boolean":
            options.append(st.booleans())
        elif kind == "integer":
            low, high = schema.get("minimum", -(2**40)), schema.get("maximum", 2**40)
            options.append(st.integers(low, high))
        elif kind == "number":
            options.append(
                st.floats(schema.get("minimum", -1e9), schema.get("maximum", 1e9), allow_nan=False)
            )
        elif kind == "string":
            options.append(_string(schema))
        elif kind == "array":
            item = valid(schema.get("items", {}), spec, depth + 1) if depth < 4 else st.nothing()
            options.append(st.lists(item, max_size=min(schema.get("maxItems", 4), 4)))
        elif kind == "object":
            options.append(_object(schema, spec, depth))
    return st.one_of(options)


def _string(schema: Schema) -> st.SearchStrategy[Any]:
    if schema.get("format") == "date-time":
        return _DATES
    if "pattern" in schema:
        return st.from_regex(schema["pattern"], fullmatch=True)
    low, high = schema.get("minLength", 0), min(schema.get("maxLength", 40), 130)
    return st.text(min_size=low, max_size=max(low, high)) | st.sampled_from(
        [e for e in _EDGE_TEXT if low <= len(e) <= high] or ["x" * max(low, 1)]
    )


def _object(schema: Schema, spec: Schema, depth: int) -> st.SearchStrategy[dict[str, Any]]:
    props: Schema = schema.get("properties", {})
    required = set(schema.get("required", []))
    if depth >= 4:
        return st.just({})
    return st.fixed_dictionaries(
        {k: valid(v, spec, depth + 1) for k, v in props.items() if k in required},
        optional={k: valid(v, spec, depth + 1) for k, v in props.items() if k not in required},
    )


def hostile(schema: Schema, spec: Schema) -> st.SearchStrategy[Any]:
    """A valid document with up to three nodes replaced by an edge value, a wrong type or junk."""
    base = valid(schema, spec)

    @st.composite
    def build(draw: st.DrawFn) -> Any:  # noqa: ANN401
        doc = draw(base)
        for _ in range(draw(st.sampled_from([0, 1, 1, 2, 3]))):
            slots: list[tuple[Any, Any]] = []

            def walk(node: Any, slots: list[tuple[Any, Any]] = slots) -> None:  # noqa: ANN401
                items = (
                    enumerate(node)
                    if isinstance(node, list)
                    else node.items()
                    if isinstance(node, dict)
                    else ()
                )
                for key, child in list(items):
                    slots.append((node, key))
                    walk(child, slots)

            walk(doc)
            if not slots:
                return draw(junk)
            container, key = slots[draw(st.integers(0, len(slots) - 1))]
            replacement = draw(
                st.one_of(junk, st.sampled_from(_EDGE_NUMBERS), st.sampled_from(_EDGE_TEXT), _DATES)
            )
            if draw(st.integers(0, 5)) == 0 and isinstance(container, dict):
                container[draw(st.text(max_size=8))] = replacement  # an unknown property
            else:
                container[key] = replacement
        return doc

    return build()
