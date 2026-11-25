"""A model with one deployment survives a transient connect failure (pre-switch eval finding).

Alias `alpha-one` has a single deployment. A connect failure used to put it into a 5 s cooldown, so
every call after it was refused with 503; now it only counts towards the circuit breaker. A
provider 429 still cools, and `keys update` changes a live key's limits.
"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from spendgate.config.schema import Routing
from spendgate.core.outcomes import Outcome
from spendgate.local.runtime import Runtime
from tests.fakes import FakeUpstream, RefusingTransport, Script
from tests.integration.harness import ENV, async_test, open_stack
from tests.integration.test_admin_cli import cli
from tests.integration.test_admin_support import open_admin
from tests.unit.test_pipeline_steps import settings as make_settings


@async_test
async def test_single_deployment_connect_failure_is_not_refused_on_the_next_call(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    refusing = RefusingTransport(fake.transport(), refusals=1)
    sender = httpx.AsyncClient(transport=refusing)
    async with open_stack(tmp_path, fake=fake, pipeline_kwargs={"sender": sender}) as s:
        first = await s.chat("alpha-one")  # retried on the same deployment: one request, 200
        assert first.status_code == 200, first.text
        assert refusing.refused == 1
        assert await s.totals() == (3500, 0)  # the refused attempt cost nothing
        assert [a.cooldown_ms for a in s.observer.attempts] == [None, None]
    await sender.aclose()


@async_test
async def test_a_provider_429_still_cools_a_single_deployment(tmp_path: Path) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, retry_after="2"))
        first = await s.chat("alpha-one")
        assert first.status_code == 429
        assert [a.cooldown_ms for a in s.observer.attempts] == [2000]  # Retry-After, as given
        calls = s.fake.calls
        second = await s.chat("alpha-one")  # cooling: refused without contacting the provider
        assert second.status_code == 429 and s.fake.calls == calls
        s.clock.advance(seconds=6)
        assert (await s.chat("alpha-one")).status_code == 200


@async_test
async def test_repeated_connect_failures_open_the_breaker_then_a_probe_recovers(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    refusing = RefusingTransport(fake.transport(), refusals=5)
    sender = httpx.AsyncClient(transport=refusing)
    async with open_stack(tmp_path, fake=fake, pipeline_kwargs={"sender": sender}) as s:
        for _ in range(2):  # two attempts each: four failures so far
            assert (await s.chat("alpha-one")).status_code == 502
        # The fifth failure opens the breaker, so the same-deployment retry is not attempted.
        assert (await s.chat("alpha-one")).status_code == 502
        refused = await s.chat("alpha-one")  # open: refused without an attempt
        assert refused.status_code == 503 and refusing.refused == 5
        s.clock.advance(seconds=6)  # every candidate is cooling from the breaker: one probe goes
        probe = await s.chat("alpha-one")
        assert probe.status_code == 200 and refusing.refused == 5
        assert (await s.chat("alpha-one")).status_code == 200  # closed again
        outcomes = [a.outcome for a in s.observer.attempts]
        assert outcomes[:5] == [Outcome.UNREACHABLE] * 5 and outcomes[5:] == [Outcome.OK] * 2
    await sender.aclose()


@async_test
async def test_connect_timeout_from_config_reaches_the_client(tmp_path: Path) -> None:
    base = make_settings(tmp_path)
    assert Routing().connect_timeout_seconds == 10
    routing = Routing(connect_timeout_seconds=3)
    config = base.config.model_copy(update={"routing": routing})
    runtime = Runtime(replace(base, config=config), tmp_path / "l.db", None)
    timeout = runtime._client.timeout
    await runtime.aclose()
    assert (timeout.connect, timeout.read) == (3.0, 600.0)


@async_test
async def test_keys_update_changes_limits_and_is_audited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SPENDGATE_ADMIN_KEY", raising=False)
    async with open_admin(tmp_path, rpm=300, max_concurrent=16) as a:
        config = tmp_path / "spendgate.yaml"
        listed = await a.ok("GET", "/admin/v1/keys")
        key_id = str(listed["data"][0]["id"])  # type: ignore[index]
        code, out, _ = await cli(
            capsys, config, "keys", "update", "--local", "--json", key_id,
            "--rpm", "1200", "--max-concurrent", "64",
        )  # fmt: skip
        assert code == 0, out
        limits = json.loads(out)["limits"]
        assert (limits["rpm"], limits["max_concurrent"]) == (1200, 64)
        code, out, _ = await cli(
            capsys, config, "keys", "update", "--local", "--json", key_id, "--tpm", "9000"
        )
        limits = json.loads(out)["limits"]  # an untouched limit is kept
        assert (limits["rpm"], limits["tpm"], limits["max_concurrent"]) == (1200, 9000, 64)
        code, _, err = await cli(capsys, config, "keys", "update", "--local", key_id)
        assert code != 0 and "nothing to change" in err
        audit = await a.ok("GET", "/admin/v1/audit", params={"actor": "cli"})
        assert [e["action"] for e in audit["data"]] == ["key.update"] * 2  # type: ignore[attr-defined]


@async_test
async def test_retry_never_sent_on_the_same_deployment_is_one_reservation(tmp_path: Path) -> None:
    fake = FakeUpstream()
    sender = httpx.AsyncClient(transport=RefusingTransport(fake.transport(), refusals=1))
    async with open_stack(tmp_path, fake=fake, pipeline_kwargs={"sender": sender}) as s:
        response = await s.chat("alpha-one")
        assert response.status_code == 200, response.text
        assert [a.outcome for a in s.observer.attempts] == [Outcome.UNREACHABLE, Outcome.OK]
        assert [a.not_processed for a in s.observer.attempts] == [True, False]
        assert fake.calls == 1
        assert await s.totals() == (3500, 0)  # charged once, nothing held
    await sender.aclose()


@async_test
async def test_retry_never_sent_stops_at_max_attempts_and_releases_the_hold(
    tmp_path: Path,
) -> None:
    fake = FakeUpstream()
    refusing = RefusingTransport(fake.transport(), refusals=10)
    sender = httpx.AsyncClient(transport=refusing)
    async with open_stack(tmp_path, fake=fake, pipeline_kwargs={"sender": sender}) as s:
        response = await s.chat("alpha-one")
        assert response.status_code == 502
        assert refusing.refused == 2 and fake.calls == 0  # max_attempts, both never sent
        assert [a.outcome for a in s.observer.attempts] == [Outcome.UNREACHABLE] * 2
        assert await s.totals() == (0, 0)  # released at 0
    await sender.aclose()


@async_test
async def test_retry_never_sent_does_not_retry_a_429_on_the_same_deployment(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=429, retry_after="2"))
        response = await s.chat("alpha-one")
        assert response.status_code == 429
        assert s.fake.calls == 1 and len(s.observer.attempts) == 1
        assert await s.totals() == (0, 0)


@async_test
async def test_retry_never_sent_never_retries_a_call_that_may_have_been_sent(
    tmp_path: Path,
) -> None:
    async with open_stack(tmp_path) as s:
        s.fake.enqueue(Script(status=500))
        response = await s.chat("alpha-one")
        assert response.status_code == 502
        assert s.fake.calls == 1 and len(s.observer.attempts) == 1
