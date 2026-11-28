"""Which price entry a deployment's Region resolves to (ADR-0028), on already-validated entries.

Region-scoped entries (`regions: [...]`) price only the Regions they list; region-less entries
price every other deployment, including one without a `region`. The tier is chosen first and the
date second, so a newer region-less entry never displaces an older Region-scoped one: a scoped
price is never used for a Region it does not list, and a listed Region never falls back while a
scoped entry for it is in force.

`listed_only` drops the region-less tier: a deployment that must never take it (a Vertex AI one,
whose region-less entries are the Gemini API's prices, ADR-0034) is priced by a Region-scoped
entry that lists its Region or by nothing.
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Protocol

type Region = str | None  # a deployment's `region`; None when it has none


class Scoped(Protocol):
    """What resolution reads from a price entry."""

    @property
    def id(self) -> str: ...
    @property
    def effective_from(self) -> date: ...
    @property
    def regions(self) -> tuple[str, ...] | None: ...


def pick[E: Scoped](
    entries: Sequence[E], region: Region, today: date, *, listed_only: bool = False
) -> E | None:
    """The entry that prices `region` on `today`, or None. `entries`: one id, newest first."""
    in_force = [e for e in entries if e.effective_from <= today]
    scoped = [e for e in in_force if e.regions is not None and region in e.regions]
    if listed_only:
        return scoped[0] if scoped else None
    tier = scoped or [e for e in in_force if e.regions is None]
    return tier[0] if tier else None


def can_price(entries: Iterable[Scoped], region: Region, *, listed_only: bool = False) -> bool:
    """Whether any entry of the id could ever price `region` (date-independent)."""
    if listed_only:
        return any(e.regions is not None and region in e.regions for e in entries)
    return any(e.regions is None or region in e.regions for e in entries)


def overlap(entries: Iterable[Scoped]) -> str | None:
    """Why entries sharing an id and `effective_from` are ambiguous for some Region, or None.

    Lookup must always find at most one entry per (id, date, Region): Region sets on one date are
    disjoint and at most one entry of that date omits `regions`.
    """
    seen: dict[tuple[str, date], set[str]] = {}
    plain: set[tuple[str, date]] = set()
    for e in entries:
        key = (e.id, e.effective_from)
        where = f"{e.id}@{e.effective_from.isoformat()}"
        if e.regions is None:
            if key in plain:
                return f"{where} has two entries without regions"
            plain.add(key)
            continue
        taken = seen.setdefault(key, set())
        if shared := taken.intersection(e.regions):
            return f"{where} lists region {sorted(shared)[0]} in two entries"
        taken.update(e.regions)
    return None


def split[E: Scoped](
    entries: Sequence[E],
    regions: Iterable[Region],
    today: date | None,
    *,
    listed_only: bool = False,
) -> Mapping[Region, E] | None:
    """The first date on which `regions` resolve to different entries, as Region -> entry.

    Checked on `today` and on every later `effective_from` (when a new entry comes into force),
    or on every `effective_from` when `today` is None. A Region that resolves to nothing is left
    out here: the unpriced check reports it. None means one price for all, on every such date.
    """
    wanted = tuple(dict.fromkeys(regions))
    dates = {e.effective_from for e in entries if today is None or e.effective_from > today}
    if today is not None:
        dates.add(today)
    for day in sorted(dates):
        found = {
            r: e
            for r in wanted
            if (e := pick(entries, r, day, listed_only=listed_only)) is not None
        }
        if len({id(e) for e in found.values()}) > 1:
            return found
    return None
