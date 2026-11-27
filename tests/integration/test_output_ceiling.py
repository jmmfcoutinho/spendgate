"""M-15: every adapter forwards exactly one output ceiling, the one the hold was sized with.

A chat request may name both `max_tokens` and `max_completion_tokens`. The hold takes the smaller;
a provider that honours the larger (OpenAI and Azure OpenAI prefer `max_completion_tokens`) would
otherwise bill output the hold never covered. Each case checks the ceiling in the upstream body
and that the hold equals the hold of a request that names that ceiling alone, so the two agree.
"""

from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from tests.integration.harness import CHAT, MESSAGES_PATH, MODELS, async_test, open_stack
from tests.unit.audio_builders import wav

RESPONSES = "/v1/responses"
AZURE = """\
  azure-luna:
    catalog_id: azure_openai/gpt-6-luna
    deployments:
      - provider: azure_openai
        base_url: "https://azure.upstream.test"
        credential: "env:OPENAI_KEY"
"""
USER = [{"role": "user", "content": "hi"}]

type Reader = Callable[[Mapping[str, object]], object]


def _openai_chat(body: Mapping[str, object]) -> object:
    assert "max_tokens" not in body  # never a second, larger field
    return body["max_completion_tokens"]


def _openai_responses(body: Mapping[str, object]) -> object:
    return body["max_output_tokens"]


def _anthropic(body: Mapping[str, object]) -> object:
    return body["max_tokens"]


def _bedrock(body: Mapping[str, object]) -> object:
    config = body["inferenceConfig"]
    assert isinstance(config, Mapping)
    return config["maxTokens"]


def _google(body: Mapping[str, object]) -> object:
    config = body["generationConfig"]
    assert isinstance(config, Mapping)
    return config["maxOutputTokens"]


# (route, path, the request without a ceiling, where the upstream body carries the ceiling)
ADAPTERS: list[tuple[str, str, dict[str, object], Reader]] = [
    ("alpha", CHAT, {"messages": USER}, _openai_chat),
    ("azure-luna", CHAT, {"messages": USER}, _openai_chat),
    ("bedrock-haiku", CHAT, {"messages": USER}, _bedrock),
    ("gemini", CHAT, {"messages": USER}, _google),
    ("alpha", RESPONSES, {"input": "hi"}, _openai_responses),
    ("azure-luna", RESPONSES, {"input": "hi"}, _openai_responses),
    ("haiku", MESSAGES_PATH, {"messages": USER}, _anthropic),
]
CHAT_CEILINGS: list[tuple[str, dict[str, object], int | None]] = [
    ("none", {}, None),
    ("max_tokens", {"max_tokens": 40}, 40),
    ("max_completion_tokens", {"max_completion_tokens": 40}, 40),
    ("both, larger completion", {"max_tokens": 1, "max_completion_tokens": 30_000}, 1),
    ("both, larger max_tokens", {"max_tokens": 30_000, "max_completion_tokens": 3}, 3),
]  # fmt: skip
FIELD = {CHAT: "max_completion_tokens", RESPONSES: "max_output_tokens", MESSAGES_PATH: "max_tokens"}
CASES = [
    pytest.param(route, path, base, read, extra, want, id=f"{route}{path}: {name}")
    for route, path, base, read in ADAPTERS
    for name, extra, want in (
        CHAT_CEILINGS
        if path == CHAT
        else [("none", {}, None), ("one", {FIELD[path]: 40}, 40)]
    )
]  # fmt: skip


@pytest.mark.parametrize(("route", "path", "base", "read", "extra", "want"), CASES)
def test_m15_the_forwarded_ceiling_equals_the_held_one(  # noqa: PLR0913, PLR0917 - one case row
    tmp_path: Path, route: str, path: str, base: dict[str, object], read: Reader,
    extra: dict[str, object], want: int | None,
) -> None:  # fmt: skip
    _forwarded_equals_held(tmp_path, path, base | {"model": route}, read, extra, want)


@async_test
async def _forwarded_equals_held(  # noqa: PLR0913, PLR0917 - one case row
    tmp_path: Path, path: str, base: dict[str, object], read: Reader,
    extra: dict[str, object], want: int | None,
) -> None:  # fmt: skip
    async with open_stack(tmp_path, models=MODELS + AZURE) as s:
        r = await s.post(path, base | extra)
        assert r.status_code == 200, r.text
        forwarded = read(s.fake.requests[-1].json())
        assert isinstance(forwarded, int)
        if want is not None:
            assert forwarded == want
        # The same request naming only the forwarded ceiling: the same hold, so it held that.
        alone = await s.post(path, base | {FIELD[path]: forwarded})
        assert alone.status_code == 200, alone.text
        assert read(s.fake.requests[-1].json()) == forwarded
        first, second = s.observer.held[-2:]
        assert first.hold == second.hold


AZURE_STT = """\
  azure-stt:
    catalog_id: azure_openai/gpt-4o-transcribe
    deployments:
      - provider: azure_openai
        base_url: "https://azure.upstream.test"
        credential: "env:OPENAI_KEY"
"""
TRANSCRIPT_NANO_USD = 2000 * 13_200  # the 2,000-token ceiling at 13.2 USD per million output


@async_test
async def test_m19_a_token_billed_transcription_holds_its_output_ceiling(tmp_path: Path) -> None:
    # The bundled entry bills the transcript per token, and no transcription field can bound it.
    async with open_stack(tmp_path, models=MODELS + AZURE_STT) as s:
        await s.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", wav(2.0), "audio/wav")},
            data={"model": "azure-stt"},
            headers={"Authorization": f"Bearer {s.secret}"},
        )
        (held,) = s.observer.held
        assert held.hold.nano > TRANSCRIPT_NANO_USD  # the audio alone is under 2,000 nano-USD
