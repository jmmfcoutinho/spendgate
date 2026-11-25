"""A provider redirect is never followed to another host (C-09, T-SSRF-02).

Upstream hosts come from config only. A 3xx from a configured provider pointing anywhere else (an
internal address, a cloud metadata endpoint) is an unexpected answer: the gateway does not send
the request, or the provider credential, to the new location. It cannot tell whether the provider
did work, so the attempt is charged the hold and the call fails closed without failover.
"""

from pathlib import Path

import httpx
import pytest

from spendgate.cli.serve import upstream
from tests.integration.harness import async_test, open_stack

INTERNAL = "http://169.254.169.254/latest/meta-data/iam/"


class Redirecting:
    """Every configured upstream answers with a redirect; any other host is recorded as reached."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.hosts: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        return httpx.Response(self.status, headers={"location": INTERNAL})


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@async_test
async def test_t_ssrf_02_a_redirect_to_another_host_is_not_followed(
    tmp_path: Path, status: int, stream: bool
) -> None:
    fake = Redirecting(status)
    sender = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    async with open_stack(tmp_path, pipeline_kwargs={"sender": sender}) as s:
        r = await s.chat(stream=stream)
        assert r.status_code == 502, r.text
        assert r.json()["error"]["code"] == "upstream_unavailable"
        assert "169.254" not in r.text and "location" not in r.headers
        assert fake.hosts == ["a.upstream.test"]  # one attempt; no redirect, no failover
        spent, held = await s.totals()
        assert spent > 0 and held == 0  # unknown whether the provider worked: the hold
    await sender.aclose()


@async_test
async def test_t_ssrf_02_the_servers_provider_client_does_not_follow_redirects(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s, upstream(s.settings) as (client, _tracer):
        assert isinstance(client, httpx.AsyncClient)
        assert client.follow_redirects is False
