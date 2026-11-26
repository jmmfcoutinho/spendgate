"""A stand-in price for a model the catalog lacks: an existing entry's meters times a multiplier.

Used by `spendgate catalog draft` (ADR-0028); never at serve time. The draft is an override entry
marked `verified: false` whose note names the entry it copied, so the start-up warning and every
call row show that the price is borrowed. The multiplier is at least 1, and each meter is
multiplied exactly: a product that would need rounding is refused, never rounded down.
"""

import re
from datetime import date
from decimal import Decimal, Inexact, localcontext

from spendgate.core.catalog import PriceEntry, is_catalog_id

# A plain decimal, no sign, exponent or leading zero; short, so every product stays exact.
_MULTIPLIER = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?")
_MAX_MULTIPLIER_CHARS = 20
_PRECISION = 400  # far above any meter string times a 20-character multiplier


class StandInError(ValueError):
    """The stand-in cannot be drafted; the message names no price."""


def parse_multiplier(text: str) -> Decimal:
    """A decimal string of at least 1: a stand-in may cost more than its model, never less."""
    if len(text) > _MAX_MULTIPLIER_CHARS or not _MULTIPLIER.fullmatch(text):
        raise StandInError(
            f"must be a plain decimal such as 1.25, at most {_MAX_MULTIPLIER_CHARS} characters"
        )
    value = Decimal(text)
    if value < 1:
        raise StandInError("must be at least 1 (a stand-in never costs less than its model)")
    return value


def check_ids(like: str, as_id: str) -> None:
    """Both are catalog ids `provider/model` of one provider."""
    for flag, value in (("--like", like), ("--as", as_id)):
        if not is_catalog_id(value):
            raise StandInError(f"{flag}: {value!r} is not a catalog id such as openai/gpt-x")
    if like.partition("/")[0] != as_id.partition("/")[0]:
        raise StandInError(
            f"--like and --as must name the same provider ({like.partition('/')[0]} is not "
            f"{as_id.partition('/')[0]}): a price is copied within one provider only"
        )


def note(source: PriceEntry, multiplier: str) -> str:
    return (
        f"Stand-in: priced like {source.price_id} x {multiplier}; replace with the provider's price"
    )


def _times(value: Decimal, multiplier: Decimal) -> str:
    with localcontext(prec=_PRECISION) as ctx:
        ctx.traps[Inexact] = True
        try:
            product = value * multiplier
        except Inexact:
            raise StandInError("a meter times the multiplier would need rounding") from None
    return format(product.normalize(), "f")  # plain: "1.8", "100", never an exponent


def draft(source: PriceEntry, as_id: str, multiplier: str, today: date) -> dict[str, object]:
    """The override entry for `as_id`: every price meter of `source` times `multiplier`.

    It takes effect today, or on `source`'s own date when that is later (a scheduled price).
    Mode, currency, `price_multiplier`, long-context tier and limits are copied as they are, so
    the stand-in costs at least what `source` costs on every request. Lifecycle and placement
    fields (`deprecation_date`, `region`, `deployment_type`) belong to `source` and are left out.
    """
    factor = parse_multiplier(multiplier)
    meters: dict[str, object] = {}
    for name, value in source.meters.model_dump(exclude_unset=True, exclude_none=True).items():
        # audio_granularity_seconds is a billing step, not a price: copied, never multiplied.
        meters[name] = _times(value, factor) if isinstance(value, Decimal) else value
    entry: dict[str, object] = {
        "id": as_id,
        "mode": source.mode,
        "currency": source.currency,
        "effective_from": max(today, source.effective_from).isoformat(),
        "source_url": source.source_url,
        "verified": False,
        "checked_on": today.isoformat(),
        "note": note(source, multiplier),
    }
    if source.price_multiplier != 1:
        entry["price_multiplier"] = format(source.price_multiplier, "f")
    entry["meters"] = meters
    if (tier := source.long_context) is not None:
        entry["long_context"] = {
            "threshold_input_tokens": tier.threshold_input_tokens,
            "input_multiplier": format(tier.input_multiplier, "f"),
            "output_multiplier": format(tier.output_multiplier, "f"),
        }
    if source.limits is not None:
        entry["limits"] = source.limits.model_dump(exclude_none=True)
    return entry
