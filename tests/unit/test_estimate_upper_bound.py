"""M-14: what the input-token estimate promises in the production configuration (no tokenizer).

The tests compare the heuristic with real o200k and cl100k counts baked into tests/data/estimate
(see token_corpora), on the texts tokenizers split worst. What is promised:

- natural and random text (prose, code, identifiers, hex, UUIDs, base64, digits, whitespace,
  control characters, CJK, emoji) is at or above the real count after the 25% margin;
- text chosen to tokenize badly (`chosen_*`) can reach about 1.6x the margined estimate, never
  more than one token per UTF-8 byte; any excess is booked as overrun (M-03) and, with the default
  `strict_overrun`, blocks the budget until acknowledged (M-07);
- the long-context tier is chosen from a byte bound as well. Proven: text, including tool-call
  JSON and opaque inputs, costs at most one token per UTF-8 byte. Assumed: an image costs at most
  the catalog image ceiling (`limits.max_image_tokens`, else `DEFAULT_IMAGE_TOKENS`) and hidden
  provider prompts fit the documented overheads.
"""

import itertools
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from spendgate.core.catalog import PriceEntry
from spendgate.core.cost import (
    LONG_CONTEXT_MARGIN_PERCENT,
    Estimate,
    Usage,
    actual_cost,
    worst_case,
)
from spendgate.core.estimate import (
    DEFAULT_IMAGE_TOKENS,
    MESSAGE_OVERHEAD_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    STRUCTURED_OUTPUT_OVERHEAD_TOKENS,
    TOOL_OVERHEAD_TOKENS,
    TOOLS_OVERHEAD_TOKENS,
    RequestKind,
    estimate,
    image_tokens,
)
from spendgate.core.heuristic import heuristic_tokens, utf8_bound
from tests.unit import token_corpora
from tests.unit.test_estimate import chat, make_entry, ok, tokens

CORPUS = token_corpora.corpus()
BAKED = token_corpora.baked()
# Where the heuristic alone sits below o200k and cl100k, measured: random lowercase words
# (0.95), rare consonants (0.98, or 0.82 around one vowel), random CJK Extension A (0.84) and
# hangul (0.97). Prose rates would have to double to cover them, so the 25% margin does: the
# estimate is still at least 1.04x the real count. Everything with digits, case changes, spaces
# or symbols is bounded by the heuristic alone.
NEEDS_THE_MARGIN = ("words_lower", "words_rare", "cjk_ext_a/", "hangul")
WHITESPACE_THAT_DOES_NOT_MERGE = " \t\r\x0b\x0c\x1c\x1d\x1e\x1f\x85"
CHOSEN = "chosen_"
CHOSEN_BOUND = 1.6  # real count / margined estimate, at most, for chosen ASCII words
# What a space merges into, written independently of the heuristic's regex.
RIDING = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
)
# Long-context tiers as in the shipped catalog: OpenAI (2x in, 1.5x out) and Anthropic (5x, 5x).
TIERS = {"openai": ("2", "1.5"), "anthropic": ("5", "5")}
CHAT_MESSAGES = make_entry(
    "messages", {"input_per_mtok": "1", "output_per_mtok": "2"}, {"max_output_tokens": 1000}
)


def real(key: str) -> int:
    return max(BAKED[key][name] for name in token_corpora.ENCODINGS)


def test_m14_generated_texts_are_the_ones_the_counts_were_baked_from() -> None:
    assert sorted(CORPUS) == sorted(BAKED)
    changed = [k for k, text in CORPUS.items() if token_corpora.crc(text) != BAKED[k]["crc32"]]
    assert changed == [], "regenerate: uv run python -m tests.unit.token_corpora"


@pytest.mark.parametrize("key", sorted(k for k in CORPUS if not k.startswith(CHOSEN)))
def test_m14_input_estimate_is_an_upper_bound_without_a_tokenizer(key: str) -> None:
    assert tokens(chat(CORPUS[key])) >= real(key), key


@pytest.mark.parametrize("key", sorted(k for k in CORPUS if k.startswith(CHOSEN)))
def test_m14_chosen_text_stays_within_the_documented_bound(key: str) -> None:
    text = CORPUS[key]
    assert real(key) <= CHOSEN_BOUND * tokens(chat(text)), key
    assert real(key) <= len(text.encode()), key


@pytest.mark.parametrize(
    "key", sorted(k for k in CORPUS if not k.startswith((*NEEDS_THE_MARGIN, CHOSEN)))
)
def test_m14_heuristic_alone_bounds_identifiers_digits_and_symbols(key: str) -> None:
    assert heuristic_tokens(CORPUS[key]) >= real(key), key


@pytest.mark.parametrize("key", sorted(CORPUS))
def test_m14_heuristic_never_exceeds_one_token_per_utf8_byte_on_the_corpus(key: str) -> None:
    assert heuristic_tokens(CORPUS[key]) <= len(CORPUS[key].encode())


@given(st.text(max_size=300))
def test_m14_heuristic_never_exceeds_one_token_per_utf8_byte(text: str) -> None:
    # Byte-level BPE and byte-fallback tokenizers never use more tokens than bytes.
    assert 0 <= heuristic_tokens(text) <= len(text.encode())


def test_m14_mixed_pieces_cost_more_than_plain_words() -> None:
    assert heuristic_tokens("hello") == 3  # a plain word: one token per two letters
    assert heuristic_tokens("q1w2e3r4t5y6") == 12  # every letter-digit change starts a piece
    assert heuristic_tokens("getUserId") == 7  # get|User|Id at two tokens per three letters
    assert heuristic_tokens("2026") == 4  # one per digit: Gemini splits every digit
    assert heuristic_tokens("café") == 4  # caf + é at its two UTF-8 bytes
    assert heuristic_tokens("x" * 600) == 450  # an unbroken run keeps the 3/4 floor
    assert heuristic_tokens("HTTPS") == 4  # two or more capitals: split like an identifier
    assert heuristic_tokens("qxzjv") == 4  # no vowel: split like an identifier
    assert heuristic_tokens("Hello world") == 6  # the space rides on the next word


def test_m14_a_space_rides_only_on_a_word_or_punctuation() -> None:
    assert heuristic_tokens("a b") == 2
    assert heuristic_tokens("a .") == 2
    assert heuristic_tokens("a 1") == 3  # o200k and cl100k never merge a space into a number
    assert heuristic_tokens("a ²") == 4  # nor into any other numeric character (2 bytes)
    assert heuristic_tokens("a 㐀") == 5  # nor into the bytes of a lone CJK character
    assert heuristic_tokens("a 㐀㐁") == 6  # a CJK run takes it
    assert heuristic_tokens("a ") == 2  # at the end there is nothing to ride on
    assert heuristic_tokens("a  b") == 3  # only the last space of a run rides


@given(st.text(alphabet="0123456789", min_size=1, max_size=200))
def test_m14_space_separated_digits_cost_a_token_each_and_per_space(digits: str) -> None:
    assert heuristic_tokens(" ".join(digits)) >= 2 * len(digits) - 1


@given(st.characters().filter(lambda c: not c.isspace()), st.integers(min_value=1, max_value=60))
def test_m14_a_repeated_spaced_character_pays_for_every_space_it_cannot_ride(
    c: str, n: int
) -> None:
    rides = c in RIDING
    spaces = 0 if rides else n - 1
    assert heuristic_tokens(" ".join([c] * n)) >= n + spaces


@given(st.lists(st.sampled_from(["\n", "\r\n"]), min_size=1, max_size=100))
def test_m14_mixed_line_endings_cost_a_token_per_change(endings: list[str]) -> None:
    changes = 1 + sum(a != b for a, b in itertools.pairwise(endings))
    assert heuristic_tokens("".join(endings)) >= changes


@given(st.text(alphabet=WHITESPACE_THAT_DOES_NOT_MERGE, min_size=1, max_size=200))
def test_m14_whitespace_that_changes_kind_costs_a_token_per_character(text: str) -> None:
    # Only a run of one kind (spaces, tabs, newlines with indentation) merges; drop such runs to
    # one character each, and every character left is its own token at least.
    mixed = "".join(c for i, c in enumerate(text) if i == 0 or c != text[i - 1])
    assert heuristic_tokens(mixed) >= len(mixed)


@pytest.fixture(params=token_corpora.ENCODINGS)
def encoding(request: pytest.FixtureRequest) -> object:
    """A tiktoken encoding from the local cache; function scope so the socket guard is active."""
    tiktoken = pytest.importorskip("tiktoken")
    try:
        return tiktoken.get_encoding(request.param)
    except Exception as exc:  # offline and not cached: skip, never fail the suite
        pytest.skip(f"{request.param} is not cached locally: {type(exc).__name__}")


@pytest.mark.tokenizer
def test_m14_baked_counts_match_the_tokenizer(encoding: object) -> None:
    name = encoding.name  # type: ignore[attr-defined]
    for key, text in CORPUS.items():
        assert len(encoding.encode(text, disallowed_special=())) == BAKED[key][name], key  # type: ignore[attr-defined]


# --- tools ----------------------------------------------------------------------------------------
def test_m14_tool_definitions_add_the_hidden_prompt_overhead() -> None:
    tools = [
        {"type": "function", "function": {"name": "get_weather", "parameters": {}}},
        {"type": "function", "function": {"name": "get_time", "parameters": {}}},
    ]
    plain = chat("hi")
    with_tools = {**plain, "tools": tools}
    json_only = tokens({**plain, "tool_choice": tools}, safety=0)  # the same JSON, no definitions
    extra = tokens(with_tools, safety=0) - json_only
    assert extra == TOOLS_OVERHEAD_TOKENS + 2 * TOOL_OVERHEAD_TOKENS
    assert tokens({**plain, "tools": []}, safety=0) == tokens(
        {**plain, "tool_choice": []}, safety=0
    )
    responses = make_entry(
        "responses", {"input_per_mtok": "1", "output_per_mtok": "2"}, {"max_output_tokens": 9}
    )
    body = {"input": "hi", "tools": [{"type": "function", "name": "f"}]}
    counted = ok(estimate(RequestKind.RESPONSES, body, entry=responses, safety=0)).input_tokens
    assert counted >= REQUEST_OVERHEAD_TOKENS + TOOLS_OVERHEAD_TOKENS + TOOL_OVERHEAD_TOKENS
    assert MESSAGE_OVERHEAD_TOKENS < TOOL_OVERHEAD_TOKENS < TOOLS_OVERHEAD_TOKENS


# --- long context -------------------------------------------------------------------------------
def long_entry(threshold: int, provider: str = "openai") -> PriceEntry:
    """1 USD per million input tokens, 8 per million output, with the provider's long tier."""
    up_in, up_out = TIERS[provider]
    return PriceEntry.model_validate(
        {
            "id": f"{provider}/long-model",
            "mode": "chat",
            "currency": "USD",
            "effective_from": "2026-01-01",
            "source_url": "https://example.com/p",
            "verified": False,
            "meters": {"input_per_mtok": "1", "output_per_mtok": "8"},
            "limits": {"max_output_tokens": 1000},
            "long_context": {
                "threshold_input_tokens": threshold,
                "input_multiplier": up_in,
                "output_multiplier": up_out,
            },
        }
    )


def test_m14_long_context_tier_is_held_within_the_margin_of_the_threshold() -> None:
    entry = long_entry(100_000)  # 1,000 nano per input token, 2,000 at the long tier
    assert LONG_CONTEXT_MARGIN_PERCENT == 25
    # 80,000 x 1.25 reaches 100,000: the real count may well be above the threshold.
    for held in (80_000, 99_999, 100_000, 100_001):
        assert worst_case(entry, Estimate(input_tokens=held)).nano == held * 2_000, held
    assert worst_case(entry, Estimate(input_tokens=79_999)).nano == 79_999 * 1_000
    # Settlement still uses the provider's rule: the real count must be above the threshold.
    assert actual_cost(entry, Usage(input_tokens=100_000)).nano == 100_000 * 1_000


LONG_KEYS = sorted(k for k in CORPUS if k.endswith("/600") and not k.startswith(CHOSEN))


@pytest.mark.parametrize("provider", sorted(TIERS))
@pytest.mark.parametrize("key", LONG_KEYS)
def test_m14_hold_covers_a_call_whose_real_count_crosses_the_long_context_threshold(
    key: str, provider: str
) -> None:
    """The review's scenario: the estimate sat under the threshold, the real count above it."""
    count = real(key)
    entry = long_entry(count - 1, provider)  # the real call is billed at the long tier
    est = ok(estimate(RequestKind.CHAT, chat(CORPUS[key], max_tokens=1000), entry=entry))
    actual = actual_cost(entry, Usage(input_tokens=count, output_tokens=1000))
    assert worst_case(entry, est).nano >= actual.nano, key


def test_m14_long_context_tier_is_held_when_the_byte_bound_reaches_the_threshold() -> None:
    entry = long_entry(100_000)
    assert worst_case(entry, Estimate(input_tokens=1, input_tokens_bound=100_000)).nano == 2_000
    assert worst_case(entry, Estimate(input_tokens=1, input_tokens_bound=99_999)).nano == 1_000
    est = ok(estimate(RequestKind.CHAT, chat("hello world " * 100, max_tokens=1), entry=entry))
    keys = len("roleusercontent")
    assert est.input_tokens_bound == 1200 + keys + REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS


@pytest.mark.parametrize("provider", sorted(TIERS))
@pytest.mark.parametrize("key", ["chosen_words/600", "spaced_controls/600", "newlines_mixed/600"])
def test_m14_long_context_tier_is_never_missed_even_for_chosen_text(
    key: str, provider: str
) -> None:
    """`uo zyqo ...`, spaced `\\x01` and `\\n`/`\\r\\n` mixes crossing the threshold."""
    count = real(key)
    entry = long_entry(count - 1, provider)
    est = ok(estimate(RequestKind.CHAT, chat(CORPUS[key], max_tokens=1000), entry=entry))
    assert est.input_tokens_bound >= count  # no tokenizer exceeds one token per byte
    long_tier = worst_case(
        entry,
        Estimate(input_tokens=est.input_tokens, max_output_tokens=1000, input_tokens_bound=count),
    )
    assert worst_case(entry, est) == long_tier
    actual = actual_cost(entry, Usage(input_tokens=count, output_tokens=1000))
    assert actual.nano <= CHOSEN_BOUND * worst_case(entry, est).nano


# --- free-form JSON and opaque inputs -------------------------------------------------------------
def tool_use_body(value: object) -> dict[str, object]:
    call = {"type": "tool_use", "id": "toolu_1", "name": "f", "input": value}
    return {
        "max_tokens": 1000,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [call]},
        ],
    }


TOOL_INPUTS: dict[str, dict[str, list[object]]] = {
    "nulls": {"rows": [None] * 5_000},
    "empty_objects": {"rows": [{}] * 5_000},
    "zeros": {"rows": [0] * 5_000},
}


@pytest.mark.parametrize("provider", sorted(TIERS))
@pytest.mark.parametrize("name", sorted(TOOL_INPUTS))
def test_m14_tool_use_input_is_billed_as_json_and_the_long_tier_is_taken(
    name: str, provider: str
) -> None:
    """`{"rows": [null, ...]}` was nearly free: only keys and leaf strings and numbers counted."""
    value = TOOL_INPUTS[name]
    threshold = utf8_bound(json.dumps(value, indent=2))  # a real count can only be below this
    entry = long_entry(threshold, provider)
    est = ok(estimate(RequestKind.MESSAGES, tool_use_body(value), entry=entry))
    assert est.input_tokens_bound >= threshold
    assert est.input_tokens >= len(value["rows"])  # every element is at least one token
    long_tier = Estimate(
        input_tokens=est.input_tokens,
        max_output_tokens=1000,
        input_tokens_bound=threshold,
    )
    assert worst_case(entry, est) == worst_case(entry, long_tier)


@pytest.mark.tokenizer
@pytest.mark.parametrize("name", sorted(TOOL_INPUTS))
def test_m14_tool_use_bound_covers_every_rendering(name: str, encoding: object) -> None:
    value = TOOL_INPUTS[name]
    est = ok(estimate(RequestKind.MESSAGES, tool_use_body(value), entry=CHAT_MESSAGES))
    for rendering in (
        json.dumps(value, separators=(",", ":")),
        json.dumps(value),
        json.dumps(value, indent=2),
    ):
        assert est.input_tokens_bound >= len(encoding.encode(rendering))  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "part",
    [
        {"type": "thinking", "thinking": "", "signature": "S" * 4_000},
        {"type": "redacted_thinking", "data": "R" * 4_000},
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "E" * 4_000},
    ],
)
def test_m14_opaque_inputs_count_at_their_bytes(part: dict[str, object]) -> None:
    body = {"max_tokens": 10, "messages": [{"role": "assistant", "content": [part]}]}
    est = ok(estimate(RequestKind.MESSAGES, body, entry=CHAT_MESSAGES, safety=0))
    assert est.input_tokens >= 4_000 + REQUEST_OVERHEAD_TOKENS
    assert est.input_tokens_bound >= 4_000


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("response_format", {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}),
        ("response_format", {"type": "json_object"}),
        ("text", {"format": {"type": "json_schema", "name": "x", "schema": {}}}),
        ("output_config", {"format": {"type": "json_schema", "schema": {}}}),
    ],
)
def test_m14_structured_output_adds_its_hidden_prompt(key: str, value: object) -> None:
    plain = tokens(chat("hi"), safety=0)
    structured = tokens({**chat("hi"), key: value}, safety=0)
    assert structured - plain >= STRUCTURED_OUTPUT_OVERHEAD_TOKENS
    assert tokens({**chat("hi"), "response_format": {"type": "text"}}, safety=0) < plain + 600


@pytest.mark.parametrize("ceiling", [None, 1_000])
def test_m14_byte_bound_holds_each_image_at_the_catalog_image_ceiling(ceiling: int | None) -> None:
    limits = {"max_output_tokens": 100} | ({} if ceiling is None else {"max_image_tokens": ceiling})
    entry = make_entry("chat", {"input_per_mtok": "1", "output_per_mtok": "2"}, limits)
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    body = {"messages": [{"role": "user", "content": [part, part]}]}
    est = ok(estimate(RequestKind.CHAT, body, entry=entry))
    per_image = image_tokens(entry)
    assert per_image == (DEFAULT_IMAGE_TOKENS if ceiling is None else ceiling)
    assert 2 * per_image <= est.input_tokens_bound < 2 * per_image + 200
