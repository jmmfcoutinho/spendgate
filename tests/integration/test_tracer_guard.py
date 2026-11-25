"""A plugged-in tracer that raises never changes how a call is held, sent or settled (M-13).

Without the guard, a hold span that failed on exit turned a held call into a 500 whose hold nobody
owned any more: the sweeper then charged it in full for a call that was never sent.
"""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest

from spendgate.pipeline import tracing
from tests.integration.harness import async_test, open_stack

PLACES = ("parent", "enter", "exit", "set")


class _Span:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    def set(self, name: str, value: tracing.Attribute) -> None:
        if self._fail:
            raise RuntimeError("tracer bug")


class Raising:
    """A tracer that raises in one place: reading the parent, starting, ending or tagging a span."""

    def __init__(self, where: str) -> None:
        self.where = where
        self.spans = 0

    def parent(self, headers: Mapping[str, str]) -> object:
        if self.where == "parent":
            raise RuntimeError("tracer bug")
        return None

    def span(
        self, name: str, parent: object, attributes: Mapping[str, tracing.Attribute]
    ) -> AbstractContextManager[tracing.Span]:
        return self._span()

    @contextmanager
    def _span(self) -> Iterator[tracing.Span]:
        if self.where == "enter":
            raise RuntimeError("tracer bug")
        self.spans += 1
        yield _Span(self.where == "set")
        if self.where == "exit":
            raise RuntimeError("tracer bug")


@pytest.mark.parametrize("where", PLACES)
@async_test
async def test_m13_a_raising_tracer_leaves_the_call_answered_and_settled_once(
    tmp_path: Path, where: str
) -> None:
    tracer = Raising(where)
    async with open_stack(tmp_path, pipeline_kwargs={"tracer": tracer}) as s:
        reply = await s.chat()
        assert reply.status_code == 200, reply.text
        await s.pipeline.drain()
        spent, held = await s.totals()
        assert spent > 0 and held == 0  # settled at the actual cost, nothing left for the sweeper
        assert s.pipeline.owned() == frozenset() and s.pipeline.unsettled() == frozenset()
        assert len(s.observer.settled) == 1 and not s.observer.internal


@async_test
async def test_a_raising_tracer_never_swallows_the_steps_own_error(tmp_path: Path) -> None:
    class Swallowing(Raising):
        @contextmanager
        def _span(self) -> Iterator[tracing.Span]:
            try:
                yield _Span(False)
            except Exception:  # a buggy tracer that eats the error of the step it wraps
                return

    guarded = tracing.Guarded(Swallowing("none"))
    with pytest.raises(ValueError, match="the step failed"), guarded.span("x", None, {}):
        raise ValueError("the step failed")


def test_a_span_that_fails_to_start_leaves_the_steps_error_unchained() -> None:
    guarded = tracing.Guarded(Raising("enter"))
    span = guarded.span("x", None, {})
    with pytest.raises(ValueError, match="the step failed") as raised, span:
        raise ValueError("the step failed")
    assert raised.value.__context__ is None  # not the tracer's RuntimeError
