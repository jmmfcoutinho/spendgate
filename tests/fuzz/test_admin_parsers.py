# ruff: noqa: PLR0912
"""Target 10: `admin_http.decode_cursor`, `admin_http.parse_time`, the `ledger.usage` query builder
(`UsageQuery` and `build`) and `config.load` on fuzzed YAML.

Inputs: arbitrary text and mutated valid values for cursors and times; hostile group-by / filter /
limit / offset / time values for the usage query; and configuration files that are the test rig's
valid YAML with nodes replaced (huge or negative numbers, NaN, wrong types, odd secret references,
inline secret values, duplicated keys, anchors and tags), byte-level damage, or plain junk.

Invariants: only the documented error types are raised (`ApiError` for cursors and times,
`UsageQueryError` for the query, `ConfigError` for the configuration); a cursor round-trips and
decodes to exactly the requested types; times land in range; user-controlled strings reach the SQL
only as bound parameters; no secret value (an inline secret pasted into the YAML, a secret an
`env:` reference resolved to) appears in any configuration error.
"""

import contextlib
import re
import string
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from hypothesis import strategies as st

from spendgate.config import ConfigError, load
from spendgate.core.errors import ErrorCode
from spendgate.gateway.admin_http import ApiError, decode_cursor, encode_cursor, parse_time
from spendgate.ledger.usage import (
    DIMENSIONS,
    MAX_GROUP_BY,
    MAX_LIMIT,
    Basis,
    UsageFilters,
    UsageQuery,
    UsageQueryError,
    build,
)
from tests.fuzz.conftest import Case, run_fuzz
from tests.fuzz.schema_strategies import junk
from tests.integration import harness as rig

NOW = 1_800_000_000_000

# --- cursors -----------------------------------------------------------------------------------

# The base64url alphabet cursors are drawn from (not a secret).
_URLSAFE = string.ascii_letters + string.digits + "-_="

_SHAPES = [(int,), (int, str), (str, int), (int, int, str), (str,)]
_PARTS = st.lists(st.one_of(st.integers(-(2**70), 2**70), st.text(max_size=20)), max_size=4)
_RAW_CURSORS = st.one_of(
    st.none(),
    st.text(max_size=60),
    st.text(alphabet=_URLSAFE, max_size=60),
    _PARTS.map(lambda p: encode_cursor(*p)),
    _PARTS.map(lambda p: encode_cursor(*p)[:-1]),
    _PARTS.map(lambda p: encode_cursor(*p) + "=="),
    _PARTS.map(lambda p: encode_cursor(*p).replace("W", "X", 1)),
    st.sampled_from(
        [
            "",
            "W10",
            "e30",
            "bnVsbA",
            "W251bGxd",
            "W3RydWVd",
            "WzEuNV0",
            "WyJhIl0",
            "WzEsImEiXQ",
            "A" * 4000,
        ]
    ),
)


def check_cursor(case: tuple[str | None, tuple[type, ...]]) -> None:
    raw, shape = case
    try:
        out = decode_cursor(raw, shape)
    except ApiError as exc:
        assert exc.refusal.code is ErrorCode.INVALID_REQUEST and exc.refusal.param == "cursor"
        assert raw is not None
        return
    if raw is None:
        assert out is None
    else:
        assert out is not None and len(out) == len(shape)
        assert all(type(v) is t for v, t in zip(out, shape, strict=True))


def check_cursor_roundtrip(parts: list[int | str]) -> None:
    shape = tuple(type(p) for p in parts)
    if not parts:
        return
    assert decode_cursor(encode_cursor(*parts), shape) == tuple(parts)


# --- times -------------------------------------------------------------------------------------

_TIME_TEXT = st.one_of(
    st.text(max_size=40),
    st.from_regex(r"[0-9]{1,8}[smhdwy]?", fullmatch=True),
    st.from_regex(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}(:[0-9]{2}(\.[0-9]{1,9})?)?"
        r"(Z|[+-][0-9]{2}:?[0-9]{2})?",
        fullmatch=True,
    ),
    st.datetimes(
        min_value=__import__("datetime").datetime(1, 1, 1),
        max_value=__import__("datetime").datetime(9999, 12, 31, 23, 59, 59),
        timezones=st.timezones(),
    ).map(lambda d: d.isoformat()),
    st.sampled_from(
        [
            "0001-01-01T00:00:00+14:00",
            "9999-12-31T23:59:59-14:00",
            "9999-12-31T23:59:59.999999Z",
            "0001-01-01T00:00:00Z",
            "999999w",
            "\u00b2d",
            "\u0661d",
            "1e5d",
            "2026-W01-1",
            "20261117T000000Z",
        ]
    ),
)


def check_time(case: tuple[str, int]) -> None:
    raw, now = case
    try:
        value = parse_time(raw, now, "since")
    except ApiError as exc:
        assert exc.refusal.code is ErrorCode.INVALID_REQUEST and exc.refusal.param == "since"
        return
    assert isinstance(value, int) and -(2**63) < value < 2**63


# --- the usage query ---------------------------------------------------------------------------

_MARK = "ZQX"
_FILTER = st.none() | st.text(max_size=20).map(lambda t: _MARK + t) | st.just(_MARK + "' OR 1=1 --")
_INTS = st.one_of(
    st.none(),
    st.integers(-(2**70), 2**70),
    st.sampled_from([0, 1, MAX_LIMIT, MAX_LIMIT + 1, 2**63 - 1, 2**63, -1]),
)
_COUNTS = st.one_of(
    st.integers(-(2**70), 2**70),
    st.sampled_from([0, 1, MAX_LIMIT, MAX_LIMIT + 1, 2**63 - 1, 2**63, -1]),
)  # typed `int`
_GROUPS = st.lists(
    st.sampled_from(sorted(DIMENSIONS))
    | st.text(max_size=10)
    | st.sampled_from(["", "scope; DROP TABLE x", "tag", "day"]),
    max_size=9,
).map(tuple)


def check_query(case: tuple[Any, ...]) -> None:
    group_by, since, until, filters, basis, limit, offset, scope_path = case
    try:
        query = UsageQuery(group_by, since, until, filters, basis, limit, offset)
    except UsageQueryError:
        return
    assert len(query.group_by) <= MAX_GROUP_BY and set(query.group_by) <= set(DIMENSIONS)
    assert 1 <= query.limit <= MAX_LIMIT and query.offset >= 0
    for grouped in (True, False):
        stmt = build(query, scope_path, grouped=grouped)
        assert _MARK not in stmt.sql, "a filter value was interpolated into the SQL text"
        assert "--" not in stmt.sql and ";" not in stmt.sql
        for name in re.findall(r":(\w+)", stmt.sql):
            assert name in stmt.params, name


# --- configuration -----------------------------------------------------------------------------

SECRETS = ["sk-INLINE-SECRET-aaaa1111", "pepper-INLINE-SECRET-bbbb2222-pad", *rig.ENV.values()]
_BASE_TEXT = rig.CONFIG.format(
    max_body=26_214_400,
    max_json_body=1_048_576,
    concurrent=128,
    attempts=2,
    cooldown=5,
    models=rig.MODELS,
)
_BASE: dict[str, Any] = yaml.safe_load(_BASE_TEXT)
_HOSTILE = st.one_of(
    junk,
    st.sampled_from(
        [
            0,
            -1,
            2**31,
            2**63,
            10**30,
            float("nan"),
            float("inf"),
            "",
            "env:",
            "env:UNSET_VAR",
            "env:1BAD",
            "file:/nonexistent/x",
            "file:",
            "file:relative",
            "http://x",
            "https://169.254.169.254/",
            "ftp://x",
            "x" * 400,
            [],
            {},
            None,
            True,
        ]
    ),
)
# Pasted into a secret-bearing field, a real secret must be refused without being echoed.
_INLINE = st.sampled_from(
    [
        *SECRETS[:2],
        "env:TEST_PEPPER ",
        "env:TEST_PEPPER\n",
        " env:TEST_PEPPER",
        "env:TEST_PEPPER" + SECRETS[0],
        "pepper:" + SECRETS[1],
    ]
)


def _slots(
    node: object, path: tuple[Any, ...] = (), out: list[tuple[Any, ...]] | None = None
) -> list[tuple[Any, ...]]:
    out = [] if out is None else out
    items = (
        enumerate(node)
        if isinstance(node, list)
        else node.items()
        if isinstance(node, dict)
        else ()
    )
    for key, child in items:
        out.append((*path, key))
        _slots(child, (*path, key), out)
    return out


_SLOTS = _slots(_BASE)
_SECRET_SLOTS = [p for p in _SLOTS if p[-1] in ("key_pepper", "credential", "dsn")] + [
    ("server", "key_pepper"),
    ("providers", "openai", "credential"),
]


@st.composite
def config_texts(draw: st.DrawFn) -> bytes:
    mode = draw(st.sampled_from(["structured"] * 6 + ["text", "bytes", "junk", "yaml_tricks"]))
    if mode == "junk":
        return draw(st.binary(max_size=300))
    if mode == "yaml_tricks":
        return draw(
            st.sampled_from(
                [
                    b"a: &x [*x]",
                    b"a: &x\n  b: *x",
                    b"? [a]\n: b",
                    b"a: !!python/object:os.system x",
                    b"\xef\xbb\xbfserver: {}",
                    b"\t- a",
                    b"a: b\na: c\n",
                    b"---\n---\nserver: {}",
                    b"- 1\n- 2",
                    b"",
                    b"null",
                    b"server: {key_pepper: !!binary aGk=}",
                    b"<<: {a: 1}",
                    b"a: &a [1]\nb: &b [*a,*a,*a]\nc: [*b,*b,*b]",
                ]
            )
        )
    if mode == "text":
        text = _BASE_TEXT
        for _ in range(draw(st.integers(1, 4))):
            at = draw(st.integers(0, len(text)))
            op = draw(st.sampled_from(["delete", "insert", "dup_line", "drop_line", "indent"]))
            if op == "delete":
                text = text[:at] + text[at + draw(st.integers(1, 30)) :]
            elif op == "insert":
                text = (
                    text[:at]
                    + draw(
                        st.sampled_from(
                            [
                                "\t",
                                ": ",
                                "- ",
                                "&a ",
                                "*a",
                                "!!str ",
                                "{",
                                "}",
                                "[",
                                "]",
                                "'",
                                '"',
                                "#",
                                "\n",
                                "\x00",
                                "%",
                                "|",
                                ">",
                                "? ",
                            ]
                        )
                    )
                    + text[at:]
                )
            elif op == "dup_line":
                lines = text.split("\n")
                i = draw(st.integers(0, len(lines) - 1))
                lines.insert(i, lines[i])
                text = "\n".join(lines)
            elif op == "drop_line":
                lines = text.split("\n")
                del lines[draw(st.integers(0, len(lines) - 1))]
                text = "\n".join(lines)
            else:
                text = text[:at] + "  " + text[at:]
        return text.encode()
    if mode == "bytes":
        data = bytearray(_BASE_TEXT.encode())
        for _ in range(draw(st.integers(1, 5))):
            data[draw(st.integers(0, len(data) - 1))] = draw(st.integers(0, 255))
        return bytes(data)
    doc = yaml.safe_load(_BASE_TEXT)
    for _ in range(draw(st.integers(1, 3))):
        secret_slot = draw(st.integers(0, 3)) == 0
        pool = _SECRET_SLOTS if secret_slot else _SLOTS
        path = draw(st.sampled_from(pool))
        parent: Any = doc
        try:
            for key in path[:-1]:
                parent = parent[key]
        except (KeyError, IndexError, TypeError):
            continue
        op = draw(st.sampled_from(["replace", "replace", "replace", "delete", "add"]))
        if op == "replace" and isinstance(parent, dict | list):
            with contextlib.suppress(KeyError, IndexError, TypeError):
                parent[path[-1]] = draw(_INLINE if secret_slot else _HOSTILE)
        elif op == "delete" and isinstance(parent, dict):
            parent.pop(path[-1], None)
        elif op == "add" and isinstance(parent, dict):
            parent[
                draw(
                    st.sampled_from(
                        ["unknown", "extra", "debug", "providers", "models", "bootstrap"]
                    )
                    | st.text(max_size=8)
                )
            ] = draw(_HOSTILE)
    return yaml.safe_dump(doc, allow_unicode=True).encode()


_WORK = tempfile.TemporaryDirectory()
_DIR = Path(_WORK.name)
(_DIR / "catalog_override.yaml").write_text(rig.CATALOG_OVERRIDE)
(_DIR / "secret.txt").write_text("file-INLINE-SECRET-cccc3333\n")


def check_config(data: bytes) -> None:
    path = _DIR / "config.yaml"
    path.write_bytes(data)
    path.chmod(0o600)
    started = time.monotonic()
    try:
        settings = load(path, env=rig.ENV, now_ms=rig.NOW_MS)
    except ConfigError as exc:
        text = str(exc) + repr(exc.issues) + repr(exc.args)
        for secret in (*SECRETS, "file-INLINE-SECRET-cccc3333"):
            assert secret not in text, (
                f"a secret value appears in a configuration error: {secret!r}"
            )
        assert time.monotonic() - started < 5
        return
    assert settings.routes is not None
    assert time.monotonic() - started < 5


def test_fuzz_admin_parsers() -> None:
    try:
        run_fuzz(
            "admin_parsers",
            [
                Case(
                    "decode_cursor", st.tuples(_RAW_CURSORS, st.sampled_from(_SHAPES)), check_cursor
                ),
                Case("cursor_roundtrip", _PARTS, check_cursor_roundtrip),
                Case(
                    "parse_time",
                    st.tuples(_TIME_TEXT, st.sampled_from([NOW, 0, 2**40, 10**15])),
                    check_time,
                ),
                Case(
                    "usage_query_builder",
                    st.tuples(
                        _GROUPS,
                        _INTS,
                        _INTS,
                        st.builds(
                            UsageFilters,
                            _FILTER,
                            _FILTER,
                            _FILTER,
                            _FILTER,
                            _FILTER,
                            _FILTER,
                            _FILTER,
                            st.none(),
                            _FILTER,
                        ),
                        st.sampled_from(list(Basis)),
                        _COUNTS,
                        _COUNTS,
                        _FILTER,
                    ),
                    check_query,
                ),
                Case("config_load_yaml", config_texts(), check_config),
            ],
        )
    finally:
        _WORK.cleanup()
