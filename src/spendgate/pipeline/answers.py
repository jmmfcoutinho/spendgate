"""Pure assembly of the answers the pipeline gives: the finished-call reply and the model list."""

from spendgate.config import Settings
from spendgate.core.errors import ErrorCode, Refusal
from spendgate.core.policy import KeyPolicy, model_allowed
from spendgate.pipeline import replies
from spendgate.pipeline.closing import Call
from spendgate.pipeline.model import Reply
from spendgate.pipeline.routing import Answered, Failed
from spendgate.pipeline.settle import Closed

INTERNAL = Refusal(ErrorCode.INTERNAL_ERROR, "The gateway failed; the request was not completed.")


def finished_reply(call: Call, result: Answered | Failed | None, closed: Closed) -> Reply:
    """Step 10: the provider's answer or error, with the cost header the ledger booked."""
    if result is None:  # unreachable: a cancel re-raises before this point
        result = Failed(INTERNAL, 500)
    # The cost header states what the ledger booked; if it took nothing, there is no number.
    charged = closed.decision.charge if closed.booked else None
    if isinstance(result, Failed):
        return replies.error_reply(
            result.refusal, call.inbound.endpoint.shape, call.id, status=result.status,
            charged=charged, should_retry=result.should_retry,
        )  # fmt: skip
    left = closed.remaining
    return replies.success_reply(
        result.response, call.id, charged, left[0] if left else None, left[1] if left else None
    )


def listable(settings: Settings, key: KeyPolicy, now: int) -> list[str]:
    """Priced aliases the key may use."""
    names: list[str] = []
    for alias, route in settings.routes.items():
        slug, _, model = route.catalog_id.partition("/")
        try:
            settings.catalog.lookup(slug, model, now)
        except LookupError:
            continue
        if model_allowed(key, alias) or model_allowed(key, route.catalog_id):
            names.append(alias)
    return names
