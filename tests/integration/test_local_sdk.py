"""`guard().openai()` and `.anthropic()`: the real SDKs through the local transport (httpx2)."""

import asyncio
from pathlib import Path

import openai
import pytest

from spendgate.local import Guard, guard
from tests.fakes import FakeUpstream
from tests.integration.test_local_support import fake_client, now

pytest.importorskip("openai")
pytest.importorskip("anthropic")

# pragma: allowlist nextline secret
KEYS = {"openai": "sk-test-openai-not-real", "anthropic": "sk-ant-test-not-real"}
GPT = "gpt-6-luna"
CLAUDE = "claude-haiku-5-5"


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def make(tmp_path: Path, fake: FakeUpstream, cap: str = "5 USD") -> Guard:
    return guard(
        cap, ledger_path=tmp_path / "l.db", provider_keys=KEYS, client=fake_client(fake),
        now_ms=now,
    )  # fmt: skip


def ask_openai(client: openai.OpenAI) -> str:
    raw = client.with_raw_response.chat.completions.create(
        model=GPT, max_tokens=50, messages=[{"role": "user", "content": "hi"}]
    )
    return str(raw.headers["x-spendgate-cost"])


def test_openai_sync_spends_from_the_cap_with_the_config_credential(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with make(tmp_path, fake) as g:
        cost = ask_openai(g.openai())
    assert cost.endswith(" USD")
    sent = fake.requests[0].headers["authorization"]
    assert sent == "Bearer sk-test-openai-not-real"  # pragma: allowlist secret


def test_openai_async(tmp_path: Path) -> None:
    async def main() -> str:
        g = make(tmp_path, FakeUpstream())
        try:
            raw = await g.openai(async_=True).with_raw_response.chat.completions.create(
                model=GPT, max_tokens=50, messages=[{"role": "user", "content": "hi"}]
            )
            return str(raw.headers["x-spendgate-cost"])
        finally:
            await g.aclose()

    assert asyncio.run(main()).endswith(" USD")


def test_anthropic_sync_and_async(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with make(tmp_path, fake) as g:
        sync = g.anthropic().messages.create(
            model=CLAUDE, max_tokens=50, messages=[{"role": "user", "content": "hi"}]
        )
    assert sync.usage.output_tokens == 5

    async def main() -> int:
        g2 = make(tmp_path, fake)
        try:
            reply = await g2.anthropic(async_=True).messages.create(
                model=CLAUDE, max_tokens=50, messages=[{"role": "user", "content": "hi"}]
            )
            return reply.usage.input_tokens
        finally:
            await g2.aclose()

    assert asyncio.run(main()) == 10
    assert [r.path for r in fake.requests] == ["/v1/messages", "/v1/messages"]


def test_an_exhausted_cap_is_a_402_and_never_reaches_the_provider(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with make(tmp_path, fake, cap="0.000001 USD") as g, pytest.raises(openai.APIStatusError) as e:
        ask_openai(g.openai())
    assert e.value.status_code == 402
    fake.assert_no_contact()


def test_streaming_through_the_sdk_is_relayed_and_charged(tmp_path: Path) -> None:
    fake = FakeUpstream()
    with make(tmp_path, fake) as g:
        stream = g.openai().chat.completions.create(
            model=GPT, max_tokens=50, stream=True, messages=[{"role": "user", "content": "hi"}]
        )
        text = "".join(c.choices[0].delta.content or "" for c in stream if c.choices)
        again = ask_openai(g.openai())
    assert text == "Hello"
    assert again.endswith(" USD")
