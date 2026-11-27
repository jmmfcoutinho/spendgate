"""Which endpoint kinds a catalog entry's `mode` may be sent (ADR-0033).

Pure: one rule over two enums, checked before routing so that a model never reaches an endpoint of
another mode (H-06). The provider would only answer such a request with a 4xx, which a caller could
use to open a deployment's shared circuit breaker, or, behind a server that serves both, bill it
against a hold sized for the wrong endpoint.
"""

from spendgate.core.estimate import RequestKind

_TEXT_KINDS = frozenset({RequestKind.CHAT, RequestKind.RESPONSES, RequestKind.MESSAGES})


def serves(mode: str, kind: RequestKind) -> bool:
    """Whether a model whose catalog entry has `mode` may be sent a request of `kind`.

    The text modes are one family: chat, responses and messages say how an entry is billed, and
    each text adapter refuses the text endpoints it cannot carry (a Bedrock `messages` model is
    served on /v1/chat/completions). Every other kind needs an entry of its own mode.
    """
    if kind in _TEXT_KINDS:
        return mode in _TEXT_KINDS
    return mode == kind
