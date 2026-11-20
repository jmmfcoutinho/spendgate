"""Regressions found by the fuzz targets, pinned as ordinary tests (they run in `make test`).

`xfail(strict=True)`: the test fails today because the bug is open. When it is fixed the xfail turns
into an unexpected pass (a failure) and must be removed, so the pin cannot rot.
"""

from typing import Any

import pytest

from spendgate.providers import openai_shapes as openai


@pytest.mark.parametrize("bad_type", [[], {}, [1], {"a": 1}])
def test_fz01_unhashable_item_type_is_refused_not_raised(bad_type: object) -> None:
    body: dict[str, Any] = {
        "model": "m",
        "input": [{"type": bad_type, "role": "user", "content": "hi"}],
    }
    refusal = openai.validate(openai.RESPONSES, body)
    assert refusal is not None
    assert refusal.param == "input[0].type"
