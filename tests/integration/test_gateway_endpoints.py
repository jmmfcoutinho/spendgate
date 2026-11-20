"""One happy path per endpoint and provider adapter, with the cost header equal to the catalog
price of the usage the fake reported, plus the shapes the protocol promises (request id, models,
Anthropic errors, rebuilt upstream request)."""

from decimal import Decimal
from pathlib import Path

import httpx

from spendgate.core.cost import Usage, actual_cost
from tests.fakes import FakeUsage, Script
from tests.integration.harness import NOW_MS, Stack, async_test, open_stack
from tests.unit.audio_builders import wav

USER = [{"role": "user", "content": "hi"}]


def priced(s: Stack, catalog_id: str, usage: Usage) -> str:
    """The header value the catalog dictates: price the usage, convert, format."""
    provider, _, model = catalog_id.partition("/")
    entry = s.settings.catalog.lookup(provider, model, NOW_MS)
    return s.settings.catalog.in_ledger(actual_cost(entry, usage)).format()


async def settled_ok(s: Stack, r: httpx.Response, catalog_id: str, usage: Usage) -> None:
    assert r.status_code == 200, r.text
    assert r.headers["x-spendgate-cost"] == priced(s, catalog_id, usage)
    assert r.headers["x-spendgate-request-id"].startswith("sg_req_")
    spent, held = await s.totals()
    assert held == 0 and spent > 0
    assert r.headers["x-spendgate-budget-scope"] == "org:acme/user:u1"
    assert r.headers["x-spendgate-budget-remaining"].endswith(" USD")


@async_test
async def test_chat_openai(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.chat("alpha")
        await settled_ok(s, r, "openai/gpt-6-luna", Usage(input_tokens=10, output_tokens=5))
        sent = s.fake.requests[0]
        # The upstream sees the gateway's credential and a rebuilt body, never the client's.
        assert sent.headers["authorization"] == "Bearer sk-test-openai-not-real"
        assert s.secret not in str(sent.headers) and "x-spendgate" not in str(sent.headers)
        assert sent.json()["model"] == "gpt-6-luna" and sent.json()["store"] is False


@async_test
async def test_missing_output_ceiling_is_injected_and_priced(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body: dict[str, object] = {"model": "alpha", "messages": USER}
        r = await s.post("/v1/chat/completions", body)
        assert r.status_code == 200
        assert s.fake.requests[0].json()["max_completion_tokens"] == 128_000  # the catalog ceiling
        assert s.observer.held[0].hold.nano > 60_000_000  # the hold covers that many output tokens


@async_test
async def test_responses_openai(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body: dict[str, object] = {"model": "alpha", "input": "hi", "max_output_tokens": 50}
        r = await s.post("/v1/responses", body)
        await settled_ok(s, r, "openai/gpt-6-luna", Usage(input_tokens=10, output_tokens=5))


@async_test
async def test_embeddings_openai(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.post("/v1/embeddings", {"model": "embed", "input": "hello"})
        await settled_ok(s, r, "openai/text-embedding-3-small", Usage(input_tokens=10))


@async_test
async def test_messages_anthropic_with_x_api_key(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body = {"model": "haiku", "max_tokens": 50, "messages": USER}
        r = await s.client.post("/v1/messages", json=body, headers={"x-api-key": s.secret})
        await settled_ok(
            s, r, "anthropic/claude-haiku-5-5", Usage(input_tokens=10, output_tokens=5)
        )
        assert s.fake.requests[0].headers["x-api-key"] == "sk-ant-test-not-real"


@async_test
async def test_messages_error_is_anthropic_shaped(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body = {"model": "nope", "max_tokens": 5, "messages": USER}
        r = await s.client.post("/v1/messages", json=body, headers={"x-api-key": s.secret})
        assert r.status_code == 403
        err = r.json()
        assert err["type"] == "error" and err["error"]["type"] == "permission_error"
        assert err["request_id"] == r.headers["x-spendgate-request-id"]
        s.assert_no_contact()


@async_test
async def test_chat_bedrock(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.chat("bedrock-haiku")
        await settled_ok(
            s, r, "bedrock/anthropic.claude-haiku-5-5", Usage(input_tokens=10, output_tokens=5)
        )
        assert r.json()["object"] == "chat.completion"  # Converse answer in the OpenAI shape
        assert r.json()["model"] == "bedrock-haiku"  # the gateway name, as requested


@async_test
async def test_chat_google(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.chat("gemini")
        await settled_ok(s, r, "google/gemini-4-carbon", Usage(input_tokens=10, output_tokens=5))
        assert r.json()["object"] == "chat.completion"


@async_test
async def test_transcription_is_priced_from_the_measured_audio(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(usage=FakeUsage(audio_seconds=Decimal(2))))
        r = await s.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", wav(2.0), "audio/wav")},
            data={"model": "whisper"},
            headers={"Authorization": f"Bearer {s.secret}"},
        )
        await settled_ok(s, r, "openai/whisper-test", Usage(audio_seconds=Decimal(2)))
        assert r.json()["text"] == "hello"


@async_test
async def test_transcription_with_unmeasurable_audio_is_refused(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        r = await s.client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"not audio at all", "audio/wav")},
            data={"model": "whisper"},
            headers={"Authorization": f"Bearer {s.secret}"},
        )
        assert r.status_code == 400 and r.json()["error"]["param"] == "file"
        s.assert_no_contact()


@async_test
async def test_speech_returns_audio_bytes_priced_by_characters(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        body: dict[str, object] = {"model": "tts", "input": "hello world", "voice": "alloy"}
        r = await s.post("/v1/audio/speech", body)
        await settled_ok(s, r, "openai/tts-test", Usage(characters=11))
        assert r.headers["content-type"] == "audio/mpeg" and r.content.startswith(b"ID3")


@async_test
async def test_models_list_shows_only_priced_allowed_aliases(tmp_path: Path) -> None:
    async with open_stack(tmp_path, allowed_models=("alpha", "haiku")) as s:
        auth = {"Authorization": f"Bearer {s.secret}"}
        listed = (await s.client.get("/v1/models", headers=auth)).json()
        assert listed["object"] == "list"
        assert sorted(m["id"] for m in listed["data"]) == ["alpha", "haiku"]
        one = await s.client.get("/v1/models/alpha", headers=auth)
        assert one.json()["id"] == "alpha"
        assert (await s.client.get("/v1/models/embed", headers=auth)).status_code == 404
        theirs = await s.client.get(
            "/v1/models", headers=auth | {"anthropic-version": "2023-06-01"}
        )
        assert theirs.json()["data"][0]["type"] == "model"
        assert (await s.client.get("/v1/models")).status_code == 401
        s.assert_no_contact()


@async_test
async def test_observability_endpoints_are_mounted(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        assert (await s.client.get("/livez")).status_code == 200
        ready = await s.client.get("/readyz")
        assert ready.status_code == 200 and ready.json()["status"] == "ready"
