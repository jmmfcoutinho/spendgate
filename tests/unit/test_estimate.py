"""Request estimation: output ceiling (M-09), counted inputs, measured audio, corpus rates."""

import base64
import json
import random
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from spendgate.core.catalog import PriceEntry
from spendgate.core.cost import Estimate, UnpricedUsage, Usage, actual_cost, worst_case
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.estimate import (
    AUDIO_TOKENS_PER_SECOND,
    DEFAULT_IMAGE_TOKENS,
    DEFAULT_SAFETY_PERCENT,
    MESSAGE_OVERHEAD_TOKENS,
    REQUEST_OVERHEAD_TOKENS,
    Body,
    RequestKind,
    Tokenizer,
    estimate,
    resolve_output_ceiling,
)
from spendgate.core.heuristic import heuristic_tokens
from tests.unit import audio_builders as fx

CORPUS = Path(__file__).parent.parent / "data" / "estimate" / "corpus.json"
ENCODINGS = ("o200k_base", "cl100k_base")


def make_entry(
    mode: str, meters: Mapping[str, object], limits: dict[str, int] | None = None
) -> PriceEntry:
    raw: dict[str, object] = {
        "id": "openai/test-model",
        "mode": mode,
        "currency": "USD",
        "effective_from": "2026-01-01",
        "source_url": "https://example.com/p",
        "verified": False,
        "meters": meters,
    }
    if limits is not None:
        raw["limits"] = limits
    return PriceEntry.model_validate(raw)


TEXT_METERS = {"input_per_mtok": "1", "output_per_mtok": "2"}
CHAT = make_entry("chat", TEXT_METERS, {"max_output_tokens": 4096})
# The catalog loader requires a cap for text kinds, so only an unvalidated copy can lack one: this
# is the defence in depth behind M-09.
CHAT_NO_CAP = CHAT.model_copy(update={"limits": None})
VISION = make_entry("chat", TEXT_METERS | {"per_image": "0.01"}, {"max_output_tokens": 4096})
EMBED = make_entry("embedding", {"input_per_mtok": "0.1"})
WHISPER = make_entry(
    "transcription", {"audio_input_per_minute": "0.006", "audio_granularity_seconds": 15}
)
TRANSCRIBE_TOKENS = make_entry(
    "transcription", {"audio_input_per_mtok": "6", "input_per_mtok": "2"}
)
# gpt-4o-transcribe on Azure: audio and prompt in, and the transcript out, all billed per token.
TRANSCRIPT_TOKENS = make_entry(
    "transcription",
    {"audio_input_per_mtok": "6", "input_per_mtok": "2.5", "output_per_mtok": "10"},
    {"max_output_tokens": 2000},
)
SPEECH_CHARS = make_entry("speech", {"per_1k_characters": "0.3"})
SPEECH_TOKENS = make_entry(
    "speech", {"input_per_mtok": "0.6", "audio_output_per_mtok": "12"}, {"max_output_tokens": 2000}
)


def chat(text: str = "hello", **extra: object) -> dict[str, object]:
    return {"model": "m", "messages": [{"role": "user", "content": text}], **extra}


def ok(result: Estimate | Refusal) -> Estimate:
    assert isinstance(result, Estimate), result
    return result


def refused(result: Estimate | Refusal) -> Refusal:
    assert isinstance(result, Refusal), result
    return result


# --- output ceiling -----------------------------------------------------------------------------
def test_ceiling_comes_from_the_catalog_when_nothing_else_is_set() -> None:
    assert ok(estimate(RequestKind.CHAT, chat(), entry=CHAT)).max_output_tokens == 4096


def test_ceiling_key_cap_beats_a_higher_catalog_cap() -> None:
    result = estimate(RequestKind.CHAT, chat(), entry=CHAT, key_max_output_tokens=1000)
    assert ok(result).max_output_tokens == 1000


def test_ceiling_request_may_lower_but_never_raise() -> None:
    body = chat(max_tokens=200)
    assert (
        ok(
            estimate(RequestKind.CHAT, body, entry=CHAT, key_max_output_tokens=1000)
        ).max_output_tokens
        == 200
    )
    raised = refused(
        estimate(RequestKind.CHAT, chat(max_tokens=2000), entry=CHAT, key_max_output_tokens=1000)
    )
    assert (raised.code, raised.param) == (ErrorCode.REQUEST_TOO_LARGE, "max_tokens")
    assert raised.http_status == 413


def test_ceiling_both_chat_fields_the_smaller_applies() -> None:
    body = chat(max_tokens=500, max_completion_tokens=300)
    assert ok(estimate(RequestKind.CHAT, body, entry=CHAT)).max_output_tokens == 300


def test_m15_a_given_output_ceiling_is_held_as_is() -> None:
    # The pipeline passes the ceiling it forwards; the body's fields are not read again.
    body = chat(max_tokens=500, max_completion_tokens=9999)
    held = ok(estimate(RequestKind.CHAT, body, entry=CHAT, output_ceiling=300))
    assert held.max_output_tokens == 300
    assert held.input_tokens == ok(estimate(RequestKind.CHAT, chat(), entry=CHAT)).input_tokens


def test_ceiling_names_the_field_that_is_too_large() -> None:
    body = chat(max_tokens=100, max_completion_tokens=9999)
    assert refused(estimate(RequestKind.CHAT, body, entry=CHAT)).param == "max_completion_tokens"


def test_m09_missing_output_ceiling_is_refused_naming_the_param() -> None:
    chat_refusal = refused(estimate(RequestKind.CHAT, chat(), entry=CHAT_NO_CAP))
    assert (chat_refusal.code, chat_refusal.param) == (ErrorCode.INVALID_REQUEST, "max_tokens")
    body: Body = {"model": "m", "input": "hi"}
    resp = refused(
        estimate(
            RequestKind.RESPONSES,
            body,
            entry=make_entry("responses", TEXT_METERS, {"max_output_tokens": 9}).model_copy(
                update={"limits": None}
            ),
        )
    )
    assert resp.param == "max_output_tokens"
    msg = refused(
        estimate(
            RequestKind.MESSAGES,
            chat(),
            entry=make_entry("messages", TEXT_METERS, {"max_output_tokens": 9}).model_copy(
                update={"limits": None}
            ),
        )
    )
    assert msg.param == "max_tokens"


def test_ceiling_request_value_alone_is_enough_when_no_cap_exists() -> None:
    assert (
        ok(estimate(RequestKind.CHAT, chat(max_tokens=77), entry=CHAT_NO_CAP)).max_output_tokens
        == 77
    )


def test_resolve_output_ceiling_is_public_for_injection() -> None:
    assert resolve_output_ceiling(RequestKind.CHAT, chat(), entry=CHAT) == 4096
    assert resolve_output_ceiling(RequestKind.CHAT, chat(max_tokens=9), entry=CHAT) == 9


@pytest.mark.parametrize("value", [0, -5, True, 1.5, "100"])
def test_ceiling_request_value_must_be_a_positive_integer(value: object) -> None:
    result = refused(estimate(RequestKind.CHAT, chat(max_tokens=value), entry=CHAT))
    assert (result.code, result.param) == (ErrorCode.INVALID_REQUEST, "max_tokens")


def test_kinds_without_output_tokens_have_no_ceiling() -> None:
    assert ok(estimate(RequestKind.EMBEDDING, {"input": "x"}, entry=EMBED)).max_output_tokens == 0
    speech = ok(estimate(RequestKind.SPEECH, {"input": "x"}, entry=SPEECH_CHARS))
    assert speech.max_output_tokens == 0


def test_speech_billed_per_audio_token_needs_and_uses_a_cap() -> None:
    result = ok(estimate(RequestKind.SPEECH, {"input": "hello"}, entry=SPEECH_TOKENS))
    assert result.max_output_tokens == 2000
    capped = ok(
        estimate(
            RequestKind.SPEECH, {"input": "hello"}, entry=SPEECH_TOKENS, key_max_output_tokens=50
        )
    )
    assert capped.max_output_tokens == 50
    no_cap = SPEECH_TOKENS.model_copy(update={"limits": None})
    assert (
        refused(estimate(RequestKind.SPEECH, {"input": "hello"}, entry=no_cap)).param
        == "max_output_tokens"
    )


@pytest.mark.parametrize("bad", [-1, True, 1.5, "5"])
def test_bad_programmer_inputs_raise(bad: object) -> None:
    with pytest.raises(ValueError, match="safety"):
        estimate(RequestKind.CHAT, chat(), entry=CHAT, safety=bad)  # type: ignore[arg-type]


def test_bad_key_cap_raises() -> None:
    with pytest.raises(ValueError, match="key_max_output_tokens"):
        estimate(RequestKind.CHAT, chat(), entry=CHAT, key_max_output_tokens=0)


# --- text input ---------------------------------------------------------------------------------
def tokens(
    body: Body,
    *,
    entry: PriceEntry = CHAT,
    kind: RequestKind = RequestKind.CHAT,
    tokenizer: Tokenizer | None = None,
    safety: int = DEFAULT_SAFETY_PERCENT,
) -> int:
    result = estimate(kind, body, entry=entry, tokenizer=tokenizer, safety=safety)
    return ok(result).input_tokens


def test_every_billable_part_adds_tokens() -> None:
    base = tokens(chat("hello"))
    assert tokens({**chat("hello"), "system": "You are a careful assistant."}) > base
    with_tools = {
        **chat("hello"),
        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    }
    assert tokens(with_tools) > base
    assert (
        tokens(chat("hello", response_format={"type": "json_schema", "json_schema": {"name": "x"}}))
        > base
    )
    parts = [{"type": "text", "text": "a"}, {"type": "text", "text": "b" * 300}]
    assert tokens({"messages": [{"role": "user", "content": parts}]}) > base


def test_overhead_is_per_message_and_per_request() -> None:
    empty = tokens({"messages": []}, safety=0)
    assert empty == REQUEST_OVERHEAD_TOKENS
    one = tokens({"messages": [{}]}, safety=0)
    assert one == REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS


def test_safety_percentage_is_applied_to_counted_text_only() -> None:
    body = chat("word " * 100)
    raw = tokens(body, safety=0)
    assert tokens(body, safety=25) == -(-raw * 125 // 100)
    assert tokens(body) == tokens(body, safety=DEFAULT_SAFETY_PERCENT)
    image = chat("x") | {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
            }
        ]
    }
    assert (
        tokens(image, safety=100) - tokens(image, safety=0) < DEFAULT_IMAGE_TOKENS
    )  # no margin on pixels


def test_numbers_and_keys_in_tool_calls_are_counted() -> None:
    call = {
        "role": "assistant",
        "content": [{"type": "tool_use", "input": {"quantity": 12345, "flag": True}}],
    }
    assert (
        tokens({"messages": [call]}, safety=0)
        > REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS + 2
    )


def test_configuration_fields_are_not_input() -> None:
    plain = tokens(chat("hello"))
    assert tokens(chat("hello", stream=True, store=False, max_tokens=100)) == plain


def test_images_count_as_tokens_and_per_image_when_priced() -> None:
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}}
    body = {"messages": [{"role": "user", "content": [part, part]}]}
    plain = ok(estimate(RequestKind.CHAT, body, entry=CHAT))
    priced = ok(estimate(RequestKind.CHAT, body, entry=VISION))
    assert plain.input_tokens >= 2 * DEFAULT_IMAGE_TOKENS
    assert (
        plain.input_tokens < 2 * DEFAULT_IMAGE_TOKENS + 200
    )  # the base64 payload is never read as text
    assert (plain.images, priced.images) == (0, 2)  # only a priced meter, or worst_case would fail
    worst_case(CHAT, plain)
    worst_case(VISION, priced)


def test_m14_each_image_is_held_at_the_entry_ceiling_or_the_conservative_default() -> None:
    part = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100_000}}
    body = {"messages": [{"role": "user", "content": [part, part]}]}
    tight = make_entry("chat", TEXT_METERS, {"max_output_tokens": 4096, "max_image_tokens": 1})
    wide = make_entry("chat", TEXT_METERS, {"max_output_tokens": 4096, "max_image_tokens": 6_000})
    base = ok(estimate(RequestKind.CHAT, body, entry=tight)).input_tokens - 2
    # An entry without a ceiling gets the largest documented per-image cost plus a margin.
    assert DEFAULT_IMAGE_TOKENS == 61_000
    assert ok(estimate(RequestKind.CHAT, body, entry=CHAT)).input_tokens == base + 2 * 61_000
    assert ok(estimate(RequestKind.CHAT, body, entry=wide)).input_tokens == base + 2 * 6_000


DISGUISE_LABELS = ["image", "image_url", "input_image", "input_audio"]
PAYLOAD = "word " * 20_000  # about 20,000 tokens of text behind an image label


@pytest.mark.parametrize("label", DISGUISE_LABELS)
def test_m14_an_image_label_in_anthropic_tool_use_input_is_counted_as_text(label: str) -> None:
    call = {"type": "tool_use", "id": "t1", "name": "f", "input": {"type": label, "x": PAYLOAD}}
    body = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [call]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1"}]},
        ]
    }
    held = ok(estimate(RequestKind.MESSAGES, body, entry=VISION))
    assert held.images == 0
    assert held.input_tokens >= heuristic_tokens(PAYLOAD)


@pytest.mark.parametrize("label", DISGUISE_LABELS)
def test_m14_an_image_label_in_chat_tool_call_arguments_is_counted_as_text(label: str) -> None:
    function = {"name": "f", "arguments": {"type": label, "x": PAYLOAD}}
    call = {"id": "c1", "type": "function", "function": function}
    body = {"messages": [{"role": "assistant", "content": None, "tool_calls": [call]}]}
    held = ok(estimate(RequestKind.CHAT, body, entry=VISION))
    assert held.images == 0
    assert held.input_tokens >= heuristic_tokens(PAYLOAD)


def test_m14_images_count_in_every_content_position() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A"}}
    result = {"type": "tool_result", "tool_use_id": "t1", "content": [image]}
    anthropic = {"messages": [{"role": "user", "content": [image, result]}]}
    assert ok(estimate(RequestKind.MESSAGES, anthropic, entry=VISION)).images == 2
    picture = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
    output = {"type": "function_call_output", "call_id": "c", "output": [picture]}
    responses = {"input": [{"role": "user", "content": [picture]}, output]}
    assert ok(estimate(RequestKind.RESPONSES, responses, entry=VISION)).images == 2


@pytest.mark.parametrize("part_type", ["input_image", "image", "image_url"])
def test_every_image_part_shape_is_counted(part_type: str) -> None:
    body = {"messages": [{"role": "user", "content": [{"type": part_type}]}]}
    assert ok(estimate(RequestKind.CHAT, body, entry=VISION)).images == 1


def test_input_audio_part_is_measured_not_trusted() -> None:
    wav = base64.b64encode(fx.wav(4.0)).decode()
    part = {"type": "input_audio", "input_audio": {"data": wav, "format": "wav", "duration": 1}}
    audio_entry = make_entry(
        "chat", TEXT_METERS | {"audio_input_per_mtok": "5"}, {"max_output_tokens": 100}
    )
    body = {"messages": [{"role": "user", "content": [part]}]}
    result = ok(estimate(RequestKind.CHAT, body, entry=audio_entry))
    assert result.audio_seconds == Decimal(4)
    assert result.audio_input_tokens == 4 * AUDIO_TOKENS_PER_SECOND


@pytest.mark.parametrize(
    "inner",
    [{}, {"data": 5}, {"data": "***not base64***"}, {"data": base64.b64encode(b"x" * 99).decode()}],
)
def test_bad_input_audio_is_refused(inner: dict[str, object]) -> None:
    body = {
        "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": inner}]}]
    }
    result = refused(estimate(RequestKind.CHAT, body, entry=CHAT))
    assert result.code is ErrorCode.INVALID_REQUEST


def test_unserialisable_or_too_deep_input_is_refused() -> None:
    bad = refused(estimate(RequestKind.CHAT, chat(tools={"x": {1, 2}}), entry=CHAT))
    assert (bad.code, bad.param) == (ErrorCode.INVALID_REQUEST, "tools")
    deep: object = "leaf"
    for _ in range(150):
        deep = [deep]
    nested = refused(estimate(RequestKind.CHAT, {"messages": deep}, entry=CHAT))
    assert nested.code is ErrorCode.INVALID_REQUEST


def test_injected_tokenizer_replaces_the_heuristic() -> None:
    seen: list[str] = []

    def counter(text: str) -> int:
        seen.append(text)
        return 1

    body = chat("x" * 3000)
    with_tokenizer = tokens(body, tokenizer=counter, safety=0)
    assert "x" * 3000 in seen
    assert with_tokenizer < tokens(body, safety=0)
    assert with_tokenizer >= REQUEST_OVERHEAD_TOKENS + MESSAGE_OVERHEAD_TOKENS


def test_tokenizer_that_refuses_text_or_returns_nonsense_falls_back_to_the_heuristic() -> None:
    def raises(text: str) -> int:
        raise ValueError("disallowed special token")

    body = chat("hello world " * 50)
    expected = tokens(body)
    assert tokens(body, tokenizer=raises) == expected
    assert tokens(body, tokenizer=lambda _t: -1) == expected
    assert tokens(body, tokenizer=lambda _t: True) == expected


# --- embeddings ---------------------------------------------------------------------------------
def test_embedding_inputs_string_list_and_token_arrays() -> None:
    kind = RequestKind.EMBEDDING
    assert tokens({"input": "hello"}, entry=EMBED, kind=kind) > REQUEST_OVERHEAD_TOKENS
    assert tokens({"input": ["a", "b"]}, entry=EMBED, kind=kind) > tokens(
        {"input": "a"}, entry=EMBED, kind=kind
    )
    ids = [list(range(1000)), [1, 2, 3]]
    assert (
        tokens({"input": ids}, entry=EMBED, kind=kind, safety=0) == REQUEST_OVERHEAD_TOKENS + 1003
    )
    assert (
        tokens({"input": [5, 6, 7]}, entry=EMBED, kind=kind, safety=0)
        == REQUEST_OVERHEAD_TOKENS + 3
    )


@pytest.mark.parametrize("value", [None, [], 5, [["a"]], [{"a": 1}], [True]])
def test_embedding_bad_input_is_refused(value: object) -> None:
    body: Body = {"input": value}
    result = refused(estimate(RequestKind.EMBEDDING, body, entry=EMBED))
    assert (result.code, result.param) == (ErrorCode.INVALID_REQUEST, "input")


# --- transcription ------------------------------------------------------------------------------
def test_transcription_duration_is_measured_from_the_bytes() -> None:
    body = {"file": fx.wav(95.3), "duration": 1, "seconds": "1"}
    result = ok(estimate(RequestKind.TRANSCRIPTION, body, entry=WHISPER))
    assert result.audio_seconds == Decimal("95.3")
    assert result.audio_input_tokens == 0  # no token meter on this entry
    assert result.input_tokens == 0  # text rides free on a minute-priced model
    # Granularity rounding happens once, in cost.py: 95.3 s at 15 s steps is 105 s.
    assert worst_case(WHISPER, result).nano == 10_500_000


def test_transcription_token_priced_model_gets_audio_tokens_and_prompt_text() -> None:
    body = {"file": fx.wav(10.0), "prompt": "Names: Ana, Bruno"}
    result = ok(estimate(RequestKind.TRANSCRIPTION, body, entry=TRANSCRIBE_TOKENS))
    assert result.audio_input_tokens == 10 * AUDIO_TOKENS_PER_SECOND
    assert result.input_tokens > REQUEST_OVERHEAD_TOKENS
    with pytest.raises(UnpricedUsage):
        worst_case(
            WHISPER, result
        )  # a minute-only entry has no price for the token count... yet audio_seconds is set
    assert result.audio_seconds == 10


def test_m19_a_token_billed_transcription_holds_its_output_ceiling() -> None:
    body = {"file": fx.wav(180.0), "max_output_tokens": 5, "max_tokens": 5}
    result = ok(estimate(RequestKind.TRANSCRIPTION, body, entry=TRANSCRIPT_TOKENS))
    assert result.max_output_tokens == 2000  # the catalog's ceiling, whatever the form says
    # No transcription field bounds the transcript, so a key cap cannot lower the hold either.
    capped = estimate(
        RequestKind.TRANSCRIPTION, body, entry=TRANSCRIPT_TOKENS, key_max_output_tokens=10
    )
    assert ok(capped).max_output_tokens == 2000
    assert resolve_output_ceiling(RequestKind.TRANSCRIPTION, body, entry=TRANSCRIPT_TOKENS) == 2000
    # The lab case of SG-PT-04: 9,000 audio tokens in and 2,000 out never settle above the hold.
    billed = Usage(audio_input_tokens=9000, output_tokens=2000)
    assert worst_case(TRANSCRIPT_TOKENS, result) >= actual_cost(TRANSCRIPT_TOKENS, billed)
    audio_only = Estimate(audio_input_tokens=result.audio_input_tokens)
    assert worst_case(TRANSCRIPT_TOKENS, result) > worst_case(TRANSCRIPT_TOKENS, audio_only)
    # An entry that bills no transcript tokens holds none.
    assert ok(estimate(RequestKind.TRANSCRIPTION, body, entry=WHISPER)).max_output_tokens == 0


def test_m19_a_token_billed_transcription_without_a_ceiling_is_refused() -> None:
    # The catalog loader requires the ceiling, so only an unvalidated copy can lack one.
    no_cap = TRANSCRIPT_TOKENS.model_copy(update={"limits": None})
    body = {"file": fx.wav(1.0)}
    for key_cap in (None, 4096):  # a key cap is no substitute: it cannot be sent upstream
        result = estimate(
            RequestKind.TRANSCRIPTION, body, entry=no_cap, key_max_output_tokens=key_cap
        )
        assert refused(result).code is ErrorCode.MODEL_NOT_PRICED


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"file": b""},
        {"file": "text"},
        {"file": b"not audio at all, long enough to look"},
        {"file": fx.wav(0.0)},
    ],
)
def test_unmeasurable_or_missing_file_is_refused(body: Body) -> None:
    result = refused(estimate(RequestKind.TRANSCRIPTION, body, entry=WHISPER))
    assert (result.code, result.param) == (ErrorCode.INVALID_REQUEST, "file")


def test_transcription_prompt_must_be_text() -> None:
    result = refused(
        estimate(RequestKind.TRANSCRIPTION, {"file": fx.wav(1.0), "prompt": 5}, entry=WHISPER)
    )
    assert result.param == "prompt"


# --- speech -------------------------------------------------------------------------------------
def test_speech_bills_characters_not_bytes() -> None:
    result = ok(estimate(RequestKind.SPEECH, {"input": "olá, mundo 🌍"}, entry=SPEECH_CHARS))
    assert result.characters == len("olá, mundo 🌍")
    assert result.input_tokens == 0
    assert worst_case(SPEECH_CHARS, result).nano > 0


def test_speech_token_priced_model_counts_text_not_characters() -> None:
    result = ok(
        estimate(
            RequestKind.SPEECH, {"input": "hello", "instructions": "calm"}, entry=SPEECH_TOKENS
        )
    )
    assert result.characters == 0
    assert result.input_tokens > REQUEST_OVERHEAD_TOKENS


@pytest.mark.parametrize("body", [{}, {"input": ""}, {"input": 5}])
def test_speech_needs_input_text(body: Body) -> None:
    assert refused(estimate(RequestKind.SPEECH, body, entry=SPEECH_CHARS)).param == "input"


# --- worst_case integration ---------------------------------------------------------------------
def test_estimates_price_without_unpriced_usage_for_every_kind() -> None:
    cases: list[tuple[RequestKind, Body, PriceEntry]] = [
        (RequestKind.CHAT, chat(), CHAT),
        (RequestKind.EMBEDDING, {"input": "x"}, EMBED),
        (RequestKind.TRANSCRIPTION, {"file": fx.wav(2.0)}, WHISPER),
        (RequestKind.TRANSCRIPTION, {"file": fx.wav(2.0)}, TRANSCRIBE_TOKENS),
        (RequestKind.TRANSCRIPTION, {"file": fx.wav(2.0)}, TRANSCRIPT_TOKENS),
        (RequestKind.SPEECH, {"input": "hi"}, SPEECH_CHARS),
        (RequestKind.SPEECH, {"input": "hi"}, SPEECH_TOKENS),
    ]
    for kind, body, entry in cases:
        assert worst_case(entry, ok(estimate(kind, body, entry=entry))).nano > 0


# --- heuristic and corpus -----------------------------------------------------------------------
def load_corpus() -> list[dict[str, object]]:
    corpus: list[dict[str, object]] = json.loads(CORPUS.read_text("utf-8"))
    return corpus


def under_estimates(safety: int) -> tuple[int, int]:
    """(requests estimated below the real count, comparisons) over the corpus and both encodings."""
    under = total = 0
    for doc in load_corpus():
        counts = doc["tokens"]
        assert isinstance(counts, dict)
        est = tokens(chat(str(doc["text"])), safety=safety)
        for name in ENCODINGS:
            total += 1
            under += est < counts[name]
    return under, total


def test_heuristic_corpus_under_estimate_rate_is_below_one_percent() -> None:
    under, total = under_estimates(DEFAULT_SAFETY_PERCENT)
    assert total >= 200
    assert under / total < 0.01, f"{under}/{total} under-estimates at {DEFAULT_SAFETY_PERCENT}%"
    assert under == 0  # measured: none on this corpus; a regression should be seen, not absorbed


def test_heuristic_without_a_margin_is_close_but_not_enough_alone() -> None:
    under0, total = under_estimates(0)
    assert under0 / total < 0.05  # the structure of the heuristic is already conservative


def test_heuristic_is_conservative_per_script() -> None:
    assert heuristic_tokens("") == 0
    assert heuristic_tokens("word") == 2  # ceil(4/2)
    assert heuristic_tokens("a b c") == 3  # one token per short word, spaces ride free
    assert heuristic_tokens("x" * 600) == 450  # unbroken runs look like hashes
    assert heuristic_tokens("    x") == 2  # indentation
    assert heuristic_tokens("你好") == 5  # ceil(2 x 5/2)
    assert heuristic_tokens("😀") == 4  # never more than the UTF-8 bytes
    assert heuristic_tokens("é") == 2  # its UTF-8 bytes


@pytest.mark.parametrize("text", ["ก" * 20, "क" * 20, "\U0001f468‍\U0001f469‍\U0001f467" * 5])
def test_heuristic_never_exceeds_utf8_length_for_symbols(text: str) -> None:
    assert heuristic_tokens(text) <= len(text.encode())


@pytest.fixture(params=ENCODINGS)
def encoding(request: pytest.FixtureRequest) -> object:
    """A tiktoken encoding from the local cache; function scope so the socket guard is active."""
    tiktoken = pytest.importorskip("tiktoken")
    try:
        return tiktoken.get_encoding(request.param)
    except Exception as exc:  # offline and not cached: skip, never fail the suite
        pytest.skip(f"{request.param} is not cached locally: {type(exc).__name__}")


def random_docs() -> list[str]:
    """Hashes and identifiers, the worst case for tokenizers; generated so scanners stay quiet."""
    rng = random.Random(7)  # noqa: S311 - deterministic test data
    alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    docs = []
    for n in (60, 200, 600):
        docs.append("".join(rng.choice(alnum) for _ in range(n)))
        docs.append(base64.b64encode(rng.randbytes(n)).decode())
        docs.append(rng.randbytes(n // 2).hex())
    return docs


@pytest.mark.tokenizer
def test_with_the_real_tokenizer_nothing_is_under_estimated(encoding: object) -> None:
    def count(text: str) -> int:
        return len(encoding.encode(text))  # type: ignore[attr-defined]

    texts = [str(d["text"]) for d in load_corpus()] + random_docs()
    under = [t[:30] for t in texts if tokens(chat(t), tokenizer=count, safety=0) < count(t)]
    assert under == []


@pytest.mark.tokenizer
def test_baked_counts_match_the_tokenizer_and_the_heuristic_covers_random_text(
    encoding: object,
) -> None:
    name = next(n for n in ENCODINGS if encoding.name == n)  # type: ignore[attr-defined]
    for doc in load_corpus():
        assert len(encoding.encode(str(doc["text"]))) == doc["tokens"][name]  # type: ignore[attr-defined,index]
    for text in random_docs():
        assert tokens(chat(text)) >= len(encoding.encode(text))  # type: ignore[attr-defined]


def test_cr_11_audio_token_ceiling_rounds_up_a_fraction_of_a_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.0000123 s is 0.000615 tokens: the hold must count one token, not truncate it to zero."""
    from spendgate.core import estimate as module  # noqa: PLC0415

    monkeypatch.setattr(module, "_measure_audio", lambda raw, param: Decimal("0.0000123"))
    body = {"file": b"x", "model": "m"}
    result = ok(estimate(RequestKind.TRANSCRIPTION, body, entry=TRANSCRIBE_TOKENS))
    assert result.audio_input_tokens == 1
