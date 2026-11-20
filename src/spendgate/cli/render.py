"""Human output: aligned tables and money. `--json` bypasses all of this."""

from collections.abc import Sequence

type Row = Sequence[object]


def table(headers: Sequence[str], rows: Sequence[Row]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [
        max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in cells]
    return "\n".join(lines)


def money(value: object) -> str:
    """`{"amount": "1.500000000", "currency": "EUR"}` as `1.500000000 EUR`."""
    if isinstance(value, dict):
        return f"{value['amount']} {value['currency']}"
    return str(value)


def dash(value: object) -> str:
    return "-" if value is None or value == "" else str(value)
