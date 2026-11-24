"""Metric labels stay bounded whatever the client sends (issue #17).

Every label value must come from config, the ledger or a fixed enum. A value taken from the
request (the body model, the session, user or tags headers) would mint one series per string.
"""

import io
import logging
from pathlib import Path

from spendgate.core.policy import SESSION_HEADER, TAGS_HEADER, USER_HEADER
from spendgate.core.sessions import SessionDefaults
from spendgate.gateway.logs import configure_json_logging
from spendgate.gateway.metrics import Metrics
from spendgate.gateway.wiring import GatewayObserver
from tests.integration.harness import async_test, open_stack

LUNA = "openai/gpt-6-luna"


def observer() -> tuple[Metrics, GatewayObserver]:
    metrics = Metrics()
    logger = configure_json_logging(logging.StreamHandler(io.StringIO()))
    return metrics, GatewayObserver(metrics, logger)


def request_series(metrics: Metrics) -> list[str]:
    return [ln for ln in metrics.render().splitlines() if ln.startswith("spendgate_requests_total")]


@async_test
async def test_500_refusals_with_distinct_unrouted_models_make_one_unknown_series(
    tmp_path: Path,
) -> None:
    metrics, obs = observer()
    async with open_stack(tmp_path, pipeline_kwargs={"observer": obs}) as s:
        for i in range(500):
            reply = await s.chat(f"made-up-model-{i}")
            assert reply.status_code >= 400
        s.assert_no_contact()
    series = request_series(metrics)
    assert len(series) == 1, series
    assert series[0].endswith(" 500") and 'model="unknown"' in series[0]
    assert "made-up-model" not in metrics.render()


@async_test
async def test_a_refused_routed_model_is_labelled_with_its_catalog_id(tmp_path: Path) -> None:
    metrics, obs = observer()
    async with open_stack(
        tmp_path, allowed_models=("haiku",), pipeline_kwargs={"observer": obs}
    ) as s:
        await s.chat("alpha")  # alias
        await s.chat(LUNA)  # catalog id: the same model, the same series
        await s.chat("alpha-one")  # another alias of the same model
    assert metrics.requests.value(outcome="model_not_allowed", model=LUNA) == 3
    assert len(request_series(metrics)) == 1


@async_test
async def test_session_user_and_tag_headers_never_become_labels(tmp_path: Path) -> None:
    metrics, obs = observer()
    headers = {
        SESSION_HEADER: "sess-fake-4711", USER_HEADER: "user-fake-4711",
        TAGS_HEADER: "team-fake-4711",
    }  # fmt: skip
    sessions = SessionDefaults(max_steps=10)  # a session header needs a session-enabled key
    async with open_stack(
        tmp_path, session_defaults=sessions, pipeline_kwargs={"observer": obs}
    ) as s:
        body = {"model": "alpha", "max_tokens": 100, "messages": [{"role": "user", "content": "x"}]}
        ok = await s.post("/v1/chat/completions", body, headers=headers)
        refused = await s.post("/v1/chat/completions", body | {"model": "zz"}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert refused.status_code >= 400
    assert "4711" not in metrics.render()
    assert metrics.requests.value(outcome="ok", model=LUNA) == 1
