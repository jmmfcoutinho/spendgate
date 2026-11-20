"""One tolerant `Retry-After` reader for every adapter (CR-08 / CR-01): it never raises."""

import pytest

from spendgate.providers.port import retry_after_ms_seconds, retry_after_seconds


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Retry-After": "7"}, 7),
        ({"retry-after": " 7 "}, 7),
        ({"retry-after": "7.2"}, 8),  # rounded up: never retry early
        ({"retry-after": "0"}, 0),
        ({"retry-after": "²"}, None),  # "²": isdigit() is True, int() raises
        (
            {"retry-after": "\N{ARABIC-INDIC DIGIT FIVE}"},
            None,
        ),  # Arabic-Indic five: not delta-seconds
        ({"retry-after": "9" * 5000}, 86_400),  # past int()'s digit limit: capped, not raised
        ({"retry-after": "999999"}, 86_400),
        ({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, None),
        ({"retry-after": "-3"}, None),
        ({"retry-after": "1e999"}, None),
        ({"retry-after": "nan"}, None),
        ({"retry-after": ""}, None),
        ({}, None),
    ],
)
def test_retry_after_seconds_is_tolerant(headers: dict[str, str], expected: int | None) -> None:
    assert retry_after_seconds(headers) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1500", 2),
        ("1", 1),
        ("0", 0),
        ("1e999", None),
        ("nan", None),
        ("-5", None),
        ("²", None),
        ("9" * 5000, None),  # float() gives inf: no hint
        ("1e30", 86_400),
        ("junk", None),
    ],
)
def test_retry_after_ms_is_tolerant(value: str, expected: int | None) -> None:
    assert retry_after_ms_seconds({"Retry-After-Ms": value}) == expected
