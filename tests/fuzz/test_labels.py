"""Target 9: `core.policy.check_labels` and `pipeline.steps.labels_of`.

Inputs: arbitrary Unicode and control characters in the `X-Spendgate-Session` / `-User` / `-Tags`
header values (including NUL, CR / LF, DEL, NBSP, full-width digits, combining marks, 128 / 129 and
32 / 33 character boundaries, 10 / 11 tags, empty and duplicate tags), and a body `user` /
`metadata.user_id` of any JSON type.

Invariants: a label that passes `check_labels` fully matches the documented charset (written out
here character by character, not with the implementation's regex) and nothing else passes; the user
label that reaches logs and the ledger (`labels_of`'s second result, taken only for a request that
`check_labels` accepts) and every tag name (the per-call tag labels) contain only accepted values; a
body-derived user is dropped, never truncated or altered, unless it matches the charset.
"""

import string
from typing import Any

from hypothesis import strategies as st

from spendgate.core.policy import SESSION_HEADER, TAGS_HEADER, USER_HEADER, Labels, check_labels
from spendgate.pipeline.steps import labels_of
from tests.fuzz.conftest import Case, run_fuzz

_ID_CHARS = set(string.ascii_letters + string.digits + "._:-")
_TAG_CHARS = set(string.ascii_lowercase + string.digits + "._:-")
MAX_TAGS = 10


def accepted_id(value: str) -> bool:
    return 1 <= len(value) <= 128 and set(value) <= _ID_CHARS


def accepted_tag(value: str) -> bool:
    return 1 <= len(value) <= 32 and set(value) <= _TAG_CHARS


_EXOTIC = [
    "\x00",
    "\r",
    "\n",
    "\x7f",
    "\x1f",
    "\u00a0",
    "\uff11",
    "\u0663",
    "e\u0301",
    "\u202e",
    "\u200b",
    "\ud7ff",
    "\U0001f600",
    "\u212a",
    " ",
    "\t",
    ",",
    "/",
    "\\",
    "'",
    '"',
    "%",
    "$",
]


@st.composite
def value(draw: st.DrawFn, chars: str = string.ascii_letters + string.digits + "._:-") -> str:
    """Mostly the legal alphabet, with exotic characters and boundary lengths mixed in."""
    length = draw(
        st.sampled_from([0, 1, 2, 5, 31, 32, 33, 127, 128, 129, 200]) | st.integers(0, 40)
    )
    out = [draw(st.sampled_from(chars)) for _ in range(length)]
    for _ in range(draw(st.sampled_from([0, 0, 0, 1, 1, 2]))):
        if out:
            out[draw(st.integers(0, len(out) - 1))] = draw(st.sampled_from(_EXOTIC))
        else:
            out.append(draw(st.sampled_from(_EXOTIC)))
    return "".join(out)


_ANY_TEXT = st.text(max_size=140) | value()
_BODY_USER = st.one_of(
    value(),
    _ANY_TEXT,
    st.none(),
    st.integers(),
    st.booleans(),
    st.lists(st.text(max_size=4), max_size=2),
    st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=2),
)
_TAGS = st.one_of(
    st.lists(value(string.ascii_lowercase + string.digits + "._:-"), max_size=13).map(",".join),
    st.lists(value(string.ascii_lowercase + string.digits + "._:-"), max_size=13).map(", ".join),
    _ANY_TEXT,
    st.sampled_from(
        [
            "",
            ",",
            ",,",
            "a,",
            ",a",
            "a,,b",
            "a" * 32,
            "a" * 33,
            ",".join(f"t{i}" for i in range(10)),
            ",".join(f"t{i}" for i in range(11)),
        ]
    ),
)
_HEADERS = st.fixed_dictionaries(
    {},
    optional={
        SESSION_HEADER.lower(): value() | _ANY_TEXT,
        USER_HEADER.lower(): value() | _ANY_TEXT,
        TAGS_HEADER.lower(): _TAGS,
    },
)
_BODIES = st.fixed_dictionaries(
    {},
    optional={
        "user": _BODY_USER,
        "metadata": st.fixed_dictionaries({}, optional={"user_id": _BODY_USER}) | _BODY_USER,
    },
)


def check_labels_charset(case: tuple[str | None, str | None, list[str]]) -> None:
    session, user, tags = case
    refusal = check_labels(Labels(session, user, tuple(tags)))
    ok = (
        (session is None or accepted_id(session))
        and (user is None or accepted_id(user))
        and len(tags) <= MAX_TAGS
        and all(accepted_tag(t) for t in tags)
    )
    assert (refusal is None) == ok, (session, user, tags, refusal)
    if refusal is not None:
        assert refusal.param in (SESSION_HEADER, USER_HEADER, TAGS_HEADER)


def check_labels_of(case: tuple[dict[str, str], dict[str, Any]]) -> None:
    headers, body = case
    labels, attributed, tag_labels = labels_of(headers, body)
    # What reaches logs, the ledger and the metrics: only for a request `resolve` lets through.
    if check_labels(labels) is None:
        assert labels.session is None or accepted_id(labels.session)
        assert attributed is None or accepted_id(attributed)
        assert all(accepted_tag(t) for t in tag_labels), tag_labels
        assert len(tag_labels) <= MAX_TAGS
        assert set(tag_labels.values()) <= {"1"}
    if USER_HEADER.lower() not in headers:
        # the body fallback: dropped unless it is a legal identifier; never altered
        raw = body.get("user")
        meta = body.get("metadata")
        if raw is None and isinstance(meta, dict):
            raw = meta.get("user_id")
        assert attributed == (raw if isinstance(raw, str) and accepted_id(raw) else None), (
            raw,
            attributed,
        )
    else:
        assert attributed == headers[USER_HEADER.lower()]


def test_fuzz_labels() -> None:
    run_fuzz(
        "labels",
        [
            Case(
                "check_labels_exact_charset",
                st.tuples(
                    st.none() | _ANY_TEXT | value(),
                    st.none() | _ANY_TEXT | value(),
                    st.lists(
                        value(string.ascii_lowercase + string.digits + "._:-") | _ANY_TEXT,
                        max_size=12,
                    ),
                ),
                check_labels_charset,
            ),
            Case("labels_of_headers_and_body", st.tuples(_HEADERS, _BODIES), check_labels_of),
        ],
    )
