"""Vertex AI with application default credentials (`credential: adc`): fakes only, no network."""

from dataclasses import replace

import google.auth
import pytest

from spendgate.providers.google import GoogleProvider, _application_default
from tests.contract.providers.google.helpers import (
    TOKEN,
    USER,
    FakeCredentials,
    request,
    vertex_deployment,
)

pytestmark = pytest.mark.anyio


class AdcCredentials(FakeCredentials):
    """Workload identity or metadata-server credentials: no `project_id` of their own."""

    def __init__(self) -> None:
        super().__init__(project_id=None)
        del self.project_id


def test_adc_takes_the_project_google_auth_found_and_asks_for_cloud_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}
    creds = AdcCredentials()

    def default(scopes: list[str]) -> tuple[AdcCredentials, str]:
        seen["scopes"] = scopes
        return creds, "adc-project"

    monkeypatch.setattr(google.auth, "default", default)
    made = _application_default()
    assert seen["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]
    assert made.project_id == "adc-project" and not made.valid
    made.refresh(object())
    assert made.valid and made.token == TOKEN and creds.refreshes == 1


async def test_an_adc_deployment_signs_with_the_ambient_token_after_prepare() -> None:
    creds = FakeCredentials(project_id="ambient-project")
    calls: list[str] = []

    def service_account(deployment: object) -> FakeCredentials:
        raise AssertionError("an adc deployment never reads a service-account key")

    def ambient() -> FakeCredentials:
        calls.append("default")
        return creds

    adapter = GoogleProvider(service_account, ambient=ambient)
    deployment = replace(vertex_deployment(), credential="", ambient=True)
    await adapter.prepare(deployment)
    await adapter.prepare(deployment)  # a valid token is reused: no second default() or refresh
    up = adapter.translate(request({"messages": [USER]}), deployment)
    assert up.headers["authorization"] == f"Bearer {TOKEN}"
    assert "/projects/ambient-project/locations/europe-west4/" in up.url
    assert calls == ["default"] and creds.refreshes == 1
    creds.expire()
    await adapter.prepare(deployment)
    assert creds.refreshes == 2  # refreshed again, in prepare's worker thread


def test_adc_without_a_project_is_refused() -> None:
    adapter = GoogleProvider(ambient=lambda: FakeCredentials(project_id=None))
    deployment = replace(vertex_deployment(), credential="", ambient=True)
    with pytest.raises(ValueError, match="project"):
        adapter.translate(request({"messages": [USER]}), deployment)


def test_service_account_and_adc_deployments_keep_separate_tokens() -> None:
    sa, adc = FakeCredentials("sa-token-0000"), FakeCredentials("adc-token-0000")
    adapter = GoogleProvider(lambda d: sa, ambient=lambda: adc)
    plain = adapter.translate(request({"messages": [USER]}), vertex_deployment())
    ambient = replace(vertex_deployment(), credential="", ambient=True)
    via_adc = adapter.translate(request({"messages": [USER]}), ambient)
    assert plain.headers["authorization"] == "Bearer sa-token-0000"
    assert via_adc.headers["authorization"] == "Bearer adc-token-0000"
