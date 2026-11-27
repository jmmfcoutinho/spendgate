"""A model is sent only to endpoints of its catalog mode; others are refused before upstream.

Nothing used to check the endpoint against the routed entry's `mode`, so a chat alias sent to
/v1/embeddings or /v1/audio/speech was forwarded, held for its input only (H-06). OpenAI answers it
with an unbilled 403 or 404, which also fed the shared circuit breaker (SG-PT-03, ADR-0033). The
text modes (chat, responses, messages) are one family: each text adapter decides which text
endpoints it carries.
"""

from pathlib import Path

import pytest

from tests.integration.harness import CHAT, async_test, open_stack
from tests.unit.audio_builders import wav

MSG = [{"role": "user", "content": "hi"}]
# (alias, endpoint, body): each alias is priced by an entry of another mode than the endpoint's.
MISMATCHES = [
    ("alpha-one", "/v1/embeddings", {"input": "hi"}),
    ("alpha-one", "/v1/audio/speech", {"input": "hi", "voice": "alloy"}),
    ("embed", CHAT, {"messages": MSG, "max_tokens": 16}),
    ("embed", "/v1/responses", {"input": "hi", "max_output_tokens": 16}),
    ("tts", CHAT, {"messages": MSG, "max_tokens": 16}),
    ("whisper", "/v1/audio/speech", {"input": "hi", "voice": "alloy"}),
]


@pytest.mark.parametrize(("alias", "path", "body"), MISMATCHES)
@async_test
async def test_t_dos_06_a_model_on_an_endpoint_of_another_mode_is_refused_before_upstream(
    tmp_path: Path, alias: str, path: str, body: dict[str, object]
) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.post(path, {"model": alias} | body)
        assert r.status_code == 400, r.text
        error = r.json()["error"]
        assert (error["code"], error["param"]) == ("invalid_request", "model")
        assert path in error["message"]
        s.assert_no_contact()
        assert await s.totals() == (0, 0)  # nothing held, nothing charged


@async_test
async def test_t_dos_06_a_chat_alias_on_the_embeddings_endpoint_is_refused_before_upstream(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        for _ in range(10):  # the lab's attack, twice the breaker's threshold
            r = await s.post("/v1/embeddings", {"model": "alpha-one", "input": "hi"})
            assert r.status_code == 400
        s.assert_no_contact()
        assert s.observer.attempts == []
        assert (await s.chat("alpha-one")).status_code == 200  # the deployment was never touched


@async_test
async def test_t_dos_06_a_chat_alias_on_the_transcription_endpoint_is_refused_before_upstream(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", wav(1.0), "audio/wav")},
            data={"model": "alpha-one"},
            headers={"Authorization": f"Bearer {s.secret}"},
        )
        assert r.status_code == 400 and r.json()["error"]["param"] == "model"
        s.assert_no_contact()


@async_test
async def test_text_models_still_serve_every_text_endpoint_their_adapter_carries(
    tmp_path: Path,
) -> None:
    """A `responses` entry on chat and Responses, a `messages` entry (Bedrock) on chat."""
    async with open_stack(tmp_path) as s:
        assert (await s.chat("alpha-one")).status_code == 200
        responses = await s.post("/v1/responses", {"model": "alpha-one", "input": "hi"})
        assert responses.status_code == 200
        assert (await s.chat("bedrock-haiku")).status_code == 200
