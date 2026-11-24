"""The output ceiling the pipeline hands an adapter, for tests that build a `CleanRequest` by hand.

The pipeline resolves one ceiling, holds it and sets `CleanRequest.output_ceiling`; adapters forward
only that value (M-15). Adapter tests have no pipeline, so they pick the same value here.
"""

from collections.abc import Mapping

# What a test request holds when its body names no ceiling (the pipeline would inject one).
DEFAULT_CEILING = 256
_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")


def ceiling_of(body: Mapping[str, object]) -> int:
    """The smaller client value, as the pipeline resolves it, else `DEFAULT_CEILING`."""
    values = [v for k in _FIELDS if isinstance(v := body.get(k), int) and not isinstance(v, bool)]
    return min(values) if values else DEFAULT_CEILING
