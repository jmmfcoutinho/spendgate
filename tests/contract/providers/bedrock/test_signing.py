"""SigV4 signing (botocore's signer) and credential handling."""

import logging
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest import mock

import pytest

from spendgate.providers.bedrock import AwsCredentials, Bedrock, CredentialsUnavailable, sign
from spendgate.providers.port import UpstreamRequest
from tests.contract.providers.bedrock.helpers import EXAMPLE, adapter, deployment, request

NOW = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)


@contextmanager
def frozen_clock() -> Iterator[None]:
    # botocore reads the clock inside `add_auth`; this is its single time source.
    with mock.patch("botocore.auth.get_current_datetime", return_value=NOW):
        yield


# Vectors from the AWS Signature Version 4 test suite (docs.aws.amazon.com, "Signature Version 4
# test suite"): service "service", region us-east-1, 2015-08-30T12:36:00Z, the example key above.
# `get-vanilla` is a bodiless GET; `post-x-www-form-urlencoded` signs a body and a content type.
GET_VANILLA = "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
POST_FORM = "ff11897932ad3f4e8b18135d722051e5ac45fc38421b1da7b9d196a0fe09473a"


def test_sigv4_matches_the_aws_test_suite_get_vanilla_vector() -> None:
    with frozen_clock():
        signed = sign(
            UpstreamRequest("GET", "https://example.amazonaws.com/", {}, b""),
            EXAMPLE, "us-east-1", "service",
        )  # fmt: skip
    assert signed.headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
        f"SignedHeaders=host;x-amz-date, Signature={GET_VANILLA}"
    )
    assert signed.headers["X-Amz-Date"] == "20150830T123600Z"


def test_sigv4_matches_the_aws_test_suite_signed_body_vector() -> None:
    form = {"Content-Type": "application/x-www-form-urlencoded"}
    with frozen_clock():
        signed = sign(
            UpstreamRequest("POST", "https://example.amazonaws.com/", form, b"Param1=value1"),
            EXAMPLE, "us-east-1", "service",
        )  # fmt: skip
    assert signed.headers["Authorization"].endswith(
        f"SignedHeaders=content-type;host;x-amz-date, Signature={POST_FORM}"
    )


def test_session_token_is_signed_and_the_body_is_bound_to_the_signature() -> None:
    creds = AwsCredentials("AKIDEXAMPLE", "secret-example", "session-token-example")
    bedrock = Bedrock(lambda: creds)
    with frozen_clock():
        a = bedrock.translate(
            request({"messages": [{"role": "user", "content": "a"}]}), deployment()
        )
        b = bedrock.translate(
            request({"messages": [{"role": "user", "content": "b"}]}), deployment()
        )
    assert a.headers["X-Amz-Security-Token"] == "session-token-example"
    assert "x-amz-security-token" in a.headers["Authorization"]
    assert a.headers["Authorization"] != b.headers["Authorization"]


def test_credentials_come_from_the_injected_provider_on_every_call() -> None:
    keys = iter(["AKIDONE", "AKIDTWO"])  # a rotating role: each call asks the provider again
    bedrock = Bedrock(lambda: AwsCredentials(next(keys), "s"))
    first = bedrock.translate(
        request({"messages": [{"role": "user", "content": "x"}]}), deployment()
    )
    second = bedrock.translate(
        request({"messages": [{"role": "user", "content": "x"}]}), deployment()
    )
    assert "Credential=AKIDONE/" in first.headers["Authorization"]
    assert "Credential=AKIDTWO/" in second.headers["Authorization"]


def test_no_credentials_in_reprs_logs_or_the_request_repr(caplog: pytest.LogCaptureFixture) -> None:
    creds = AwsCredentials("AKIDSECRETKEY", "SECRETACCESSKEYVALUE", "SESSIONTOKENVALUE")
    bedrock = Bedrock(lambda: creds)
    with caplog.at_level(logging.DEBUG):  # botocore logs at DEBUG; it must not leak either
        up = bedrock.translate(
            request({"messages": [{"role": "user", "content": "x"}]}), deployment()
        )
    shown = repr(creds) + repr(bedrock) + repr(up) + caplog.text
    for secret in ("AKIDSECRETKEY", "SECRETACCESSKEYVALUE", "SESSIONTOKENVALUE"):
        assert secret not in shown
    assert "x-amz-security-token" not in repr(up).lower()


def test_missing_credentials_fail_without_naming_secrets() -> None:
    def none() -> AwsCredentials:
        raise CredentialsUnavailable("no AWS credentials found by the standard chain")

    with pytest.raises(CredentialsUnavailable):
        Bedrock(none).translate(
            request({"messages": [{"role": "user", "content": "x"}]}), deployment()
        )


def test_importing_the_adapter_and_translating_requests_never_loads_botocore_until_signing() -> (
    None
):
    code = (
        "import sys\n"
        "import spendgate.providers.bedrock as b, spendgate.providers.bedrock_request as r\n"
        "r.converse_body({'messages': [{'role': 'user', 'content': 'x'}]}, 64)\n"
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'botocore'))\n"
    )
    out = subprocess.run(  # noqa: S603 - fixed interpreter and code, no user input
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_adapter_is_constructed_without_credentials_resolution() -> None:
    assert repr(adapter()) == "Bedrock()"
