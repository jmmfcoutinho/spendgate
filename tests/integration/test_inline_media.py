"""M-14: only inline images reach a provider, because only images are in the worst-case estimate.

A data URL of any other type (video, PDF, audio) used to pass as an `image_url`, be held as one
image and reach Gemini as `inlineData`, which bills the whole media. Each case goes through the
full stack with the fake upstream: refused `invalid_request` naming the field, nothing held, no
upstream contact.
"""

import asyncio
import base64
from pathlib import Path

import pytest

from tests.integration.harness import MESSAGES_PATH, open_stack

REFUSED_TYPES = ("video/mp4", "application/pdf", "audio/wav")
PAYLOAD = base64.b64encode(b"\x00" * 3_000).decode()  # small: the type alone must refuse it
CHAT_MODELS = ("alpha", "gemini", "bedrock-haiku")  # OpenAI shapes, Gemini and Converse paths


def _chat_body(model: str, url: str) -> dict[str, object]:
    part = {"type": "image_url", "image_url": {"url": url}}
    content = [{"type": "text", "text": "summarise"}, part]
    return {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": content}]}


async def _refused_without_contact(tmp_path: Path, path: str, body: dict[str, object]) -> str:
    async with open_stack(tmp_path) as s:
        r = await s.post(path, body)
        assert r.status_code == 400, r.text
        error = r.json()["error"]
        assert error["code"] == "invalid_request"
        s.assert_no_contact()
        assert await s.totals() == (0, 0)
        return str(error["param"])


@pytest.mark.parametrize("model", CHAT_MODELS)
@pytest.mark.parametrize("mime", REFUSED_TYPES)
def test_m14_non_image_data_url_in_chat_is_refused_before_upstream(
    tmp_path: Path, mime: str, model: str
) -> None:
    body = _chat_body(model, f"data:{mime};base64,{PAYLOAD}")
    param = asyncio.run(_refused_without_contact(tmp_path, "/v1/chat/completions", body))
    assert param.startswith("messages[0].content[1]")


@pytest.mark.parametrize("mime", REFUSED_TYPES)
def test_m14_non_image_data_url_in_responses_is_refused_before_upstream(
    tmp_path: Path, mime: str
) -> None:
    part = {"type": "input_image", "image_url": f"data:{mime};base64,{PAYLOAD}"}
    body = {
        "model": "alpha",
        "max_output_tokens": 10,
        "input": [{"role": "user", "content": [part]}],
    }
    param = asyncio.run(_refused_without_contact(tmp_path, "/v1/responses", body))
    assert param == "input[0].content[0].image_url"


@pytest.mark.parametrize("mime", REFUSED_TYPES)
def test_m14_non_image_media_type_in_anthropic_messages_is_refused_before_upstream(
    tmp_path: Path, mime: str
) -> None:
    source = {"type": "base64", "media_type": mime, "data": PAYLOAD}
    message = {"role": "user", "content": [{"type": "image", "source": source}]}
    body = {"model": "haiku", "max_tokens": 10, "messages": [message]}
    param = asyncio.run(_refused_without_contact(tmp_path, MESSAGES_PATH, body))
    assert param == "messages[0].content[0].source.media_type"


@pytest.mark.parametrize("model", CHAT_MODELS)
def test_m14_inline_png_is_still_forwarded(tmp_path: Path, model: str) -> None:
    async def case() -> None:
        async with open_stack(tmp_path) as s:
            r = await s.post(
                "/v1/chat/completions", _chat_body(model, f"data:image/png;base64,{PAYLOAD}")
            )
            assert r.status_code == 200, r.text
            assert s.fake.calls == 1

    asyncio.run(case())
