"""`transport()` and `transport2()` stay plain functions with the public signature and help text.

Callers read them with `help()`, `inspect` and IDEs; a callable object in their place would lose
the signature, `__name__` and module even if every call still worked.
"""

import inspect
from collections.abc import Callable

import pytest

from spendgate.local.transport import transport
from spendgate.pipeline.assembly import system_ms

PARAMETERS = [
    ("settings_or_path", inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.empty),
    ("ledger_path", inspect.Parameter.KEYWORD_ONLY, None),
    ("async_", inspect.Parameter.KEYWORD_ONLY, False),
    ("cap", inspect.Parameter.KEYWORD_ONLY, None),
    ("client", inspect.Parameter.KEYWORD_ONLY, None),
    ("now_ms", inspect.Parameter.KEYWORD_ONLY, system_ms),
]

TRANSPORT_DOC = """\
An httpx transport that applies the gateway's rules in this process.

`settings_or_path` is a loaded `Settings` or a path to a `spendgate.yaml` (its `bootstrap`
section is not applied; scope and budget come from `cap`). `ledger_path` defaults to the
config's SQLite path. `cap` (for example `"5 USD"`) creates the shared budget on a new ledger;
without it the ledger must already have one, or the transport refuses to start."""

TRANSPORT2_DOC = (
    "`transport()` for `httpx2` clients; provider calls still use an `httpx.AsyncClient`."
)


def _check(fn: Callable[..., object], name: str, module: str, doc: str) -> None:
    assert inspect.isfunction(fn)
    assert (fn.__name__, fn.__module__) == (name, module)
    params = inspect.signature(fn).parameters.values()
    assert [(p.name, p.kind, p.default) for p in params] == PARAMETERS
    assert inspect.getdoc(fn) == doc


def test_transport_is_a_function_with_its_signature_and_docstring() -> None:
    _check(transport, "transport", "spendgate.local.transport", TRANSPORT_DOC)


def test_transport2_is_a_function_with_its_signature_and_docstring() -> None:
    pytest.importorskip("httpx2")
    from spendgate.local.transport_httpx2 import transport2  # noqa: PLC0415 - needs httpx2

    _check(transport2, "transport2", "spendgate.local.transport_httpx2", TRANSPORT2_DOC)
