"""`Pipeline(...)` keeps an explicit keyword-only signature; `assemble` builds its parts."""

import inspect
from typing import Any

import pytest

from spendgate.pipeline.assembly import Knobs, PipelineOptions
from spendgate.pipeline.pipeline import Pipeline

PORTS = ("settings", "ledger", "keys", "providers", "sender")


def test_the_signature_lists_every_port_and_knob_by_keyword() -> None:
    parameters = list(inspect.signature(Pipeline).parameters.values())
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters)
    names = [p.name for p in parameters]
    assert tuple(names[: len(PORTS)]) == PORTS
    assert names[len(PORTS) :] == list(Knobs.__annotations__)  # one list, in the same order
    assert set(names) == set(PipelineOptions.__annotations__)


def test_an_unknown_keyword_is_a_type_error() -> None:
    arguments: dict[str, Any] = {name: None for name in PORTS} | {"no_such_knob": 1}
    with pytest.raises(TypeError, match="no_such_knob"):
        Pipeline(**arguments)


def test_a_missing_port_is_a_type_error() -> None:
    arguments: dict[str, Any] = {name: None for name in PORTS if name != "sender"}
    with pytest.raises(TypeError, match="sender"):
        Pipeline(**arguments)
