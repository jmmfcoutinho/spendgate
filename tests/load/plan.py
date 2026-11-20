"""The per-call plan a client puts in the OpenAI `user` field and the fake provider obeys.

The `user` field is forwarded to the upstream untouched, so the client decides everything random
(from its own seeded generator) and the fake stays a pure function of the request: no shared
random state across processes. Code: dot-separated tokens, e.g. `k3.l45.u80.s429.c2.o`.
"""

from dataclasses import dataclass

FIELDS = {"k": "key", "l": "latency_ms", "u": "out_tokens", "s": "status", "c": "cut_after"}


@dataclass(frozen=True, slots=True)
class Plan:
    key: int = 0  # which client key sent it (the fake counts upstream contact per key)
    latency_ms: int = 0  # real sleep before the first byte
    out_tokens: int = 5  # completion tokens the reply reports
    status: int = 200  # >= 400 makes the fake answer with an error
    cut_after: int | None = None  # stream: this many frames, then the connection is dropped
    overreport: bool = False  # report triple the output tokens (an estimator miss)

    def encode(self) -> str:
        parts = [f"k{self.key}", f"l{self.latency_ms}", f"u{self.out_tokens}"]
        if self.status != 200:
            parts.append(f"s{self.status}")
        if self.cut_after is not None:
            parts.append(f"c{self.cut_after}")
        if self.overreport:
            parts.append("o")
        return ".".join(parts)

    @classmethod
    def decode(cls, text: object) -> "Plan":
        values: dict[str, int | bool] = {}
        for piece in str(text).split("."):
            if piece == "o":
                values["overreport"] = True
            elif piece[:1] in "klusc" and piece[1:].isdigit():
                name = FIELDS[piece[0]]
                values[name] = int(piece[1:])
        return cls(**values)  # type: ignore[arg-type]
