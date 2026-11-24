"""Generated text that tokenizers split badly, with real token counts baked next to it (M-14).

The texts are built from a SHA-256 stream, not `random`, so they are identical on every Python
version and nothing random is committed for the secret scanners to flag. Only the counts and a
CRC of each text live in `tests/data/estimate/generated_counts.json`; the tests check the CRC so a
changed generator cannot be compared with stale counts.

Regenerate the counts after changing a corpus (needs the `tokenizer` extra and cached encodings):
`uv run python -m tests.unit.token_corpora`.
"""

import base64
import hashlib
import json
import zlib
from collections.abc import Callable
from pathlib import Path

COUNTS = Path(__file__).parent.parent / "data" / "estimate" / "generated_counts.json"
ENCODINGS = ("o200k_base", "cl100k_base")
SIZES = (12, 600)  # items per text: a short request and a long one
LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = LOWER.upper()
DIGITS = "0123456789"
ALNUM = LOWER + UPPER + DIGITS
SEPARATORS = (" ", " ", " ", "\n", ", ", "-", "_", ".", "/")
RARE = "qxzjvkwy"  # letters BPE vocabularies rarely merge
WHITESPACE = " \t\n\r\x0b\x0c"
CONTROL = "\x0b\x0c\x1c\x1d\x1e\x1f"


class Stream:
    """Deterministic bytes: SHA-256 of a seed and a counter."""

    def __init__(self, seed: str) -> None:
        self._seed = seed.encode()
        self._counter = 0
        self._buffer = b""

    def take(self, n: int) -> bytes:
        while len(self._buffer) < n:
            block = self._seed + self._counter.to_bytes(8, "big")
            self._buffer += hashlib.sha256(block).digest()
            self._counter += 1
        out, self._buffer = self._buffer[:n], self._buffer[n:]
        return out

    def below(self, k: int) -> int:
        return int.from_bytes(self.take(2), "big") % k

    def pick(self, alphabet: str, n: int) -> str:
        return "".join(alphabet[self.below(len(alphabet))] for _ in range(n))


def _words(alphabet: str, low: int = 1, high: int = 12) -> Callable[[Stream, int], str]:
    def build(s: Stream, n: int) -> str:
        return " ".join(s.pick(alphabet, low + s.below(high - low + 1)) for _ in range(n))

    return build


def _separated(s: Stream, n: int) -> str:
    parts = []
    for _ in range(n):
        parts.append(s.pick(ALNUM, 1 + s.below(12)))
        parts.append(SEPARATORS[s.below(len(SEPARATORS))])
    return "".join(parts)


def _xxd(s: Stream, n: int) -> str:
    lines = []
    for i in range(n):
        raw = s.take(16)
        pairs = " ".join(raw[j : j + 2].hex() for j in range(0, 16, 2))
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in raw)
        lines.append(f"{i * 16:08x}: {pairs}  {text}")
    return "\n".join(lines)


def _uuid(s: Stream) -> str:
    h = s.take(16).hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _chars(low: int, high: int) -> Callable[[Stream, int], str]:
    def build(s: Stream, n: int) -> str:
        return "".join(chr(low + s.below(high - low)) for _ in range(n * 4))

    return build


def _latin_words(s: Stream, n: int) -> str:
    return " ".join(
        "".join(chr(0xC0 + s.below(0x250 - 0xC0)) for _ in range(1 + s.below(8))) for _ in range(n)
    )


def _digit_grid(width: int) -> Callable[[Stream, int], str]:
    def build(s: Stream, n: int) -> str:
        digits = "01" if width > 32 else DIGITS
        return "\n".join(" ".join(s.pick(digits, width)) for _ in range(max(n // 4, 1)))

    return build


def _spaced_tokens(s: Stream, n: int) -> str:
    """`a  1 ` style: single letters and digits, one to three spaces apart, a trailing space."""
    return "".join(s.pick(LOWER + DIGITS, 1) + " " * (1 + s.below(3)) for _ in range(n))


def _rare_with_vowel(s: Stream, n: int) -> str:
    words = []
    for _ in range(n):
        consonants = s.pick(RARE, 2 + s.below(7))
        cut = s.below(len(consonants) + 1)
        words.append(consonants[:cut] + s.pick("aeiou", 1) + consonants[cut:])
    return " ".join(words)


def _spaced_singles(alphabet: str) -> Callable[[Stream, int], str]:
    def build(s: Stream, n: int) -> str:
        return " ".join(s.pick(alphabet, n * 2))

    return build


def _newlines(s: Stream, n: int) -> str:
    return "".join(("\n", "\r\n", "\r\n\r\n\n", "x\n")[s.below(4)] for _ in range(n * 4))


# Rare scripts and symbols whose single characters are their bytes in o200k and cl100k.
SINGLES = "".join(
    chr(c) for c in (*range(0x531, 0x557), *range(0x10D0, 0x10F0), *range(0x2600, 0x2620))
)
CONTROLS = "".join(chr(c) for c in (*range(0x01, 0x09), *range(0x0E, 0x1C), 0x7F))
# Words chosen to tokenize badly (from a search over short ASCII words): each is a plain word to
# the heuristic and two or more tokens to o200k or cl100k. Not an upper bound: the M-14 bound for
# chosen text is about 1.6x the margined estimate, never more than the UTF-8 bytes.
CHOSEN_WORDS = ("uo", "zyqo", "qxvmojzv", "aabf")

BUILDERS: dict[str, Callable[[Stream, int], str]] = {
    "words_alnum": _words(ALNUM),
    "words_alnum_3": _words(ALNUM, 3, 3),
    "words_lower": _words(LOWER),
    "words_lower_6": _words(LOWER, 6, 6),
    "words_upper": _words(UPPER),
    "words_separated": _separated,
    "words_rare": _words(RARE, 2, 8),
    "words_rare_vowel": _rare_with_vowel,
    "words_upper_one_lower": lambda s, n: " ".join(
        s.pick(UPPER, 11) + s.pick(LOWER, 1) for _ in range(n)
    ),
    "ids_digit_letter_digit": lambda s, n: " ".join(
        s.pick(DIGITS, 1) + s.pick(LOWER, 1) + s.pick(DIGITS, 1) for _ in range(n)
    ),
    "spaced_tokens": _spaced_tokens,
    "ids_letter_digit": lambda s, n: " ".join(
        "".join(s.pick(LOWER, 1) + s.pick(DIGITS, 1) for _ in range(6)) for _ in range(n)
    ),
    "ids_camel": _words("aZ3", 12, 12),
    "hex_groups_8": lambda s, n: " ".join(s.take(4).hex() for _ in range(n)),
    "hex_groups_12": lambda s, n: " ".join(s.take(6).hex() for _ in range(n)),
    "hexdump": lambda s, n: "\n".join(
        " ".join(s.take(4).hex() for _ in range(4)) for _ in range(n)
    ),
    "hexdump_xxd": _xxd,
    "hex_unbroken": lambda s, n: s.take(n * 8).hex(),
    "uuids": lambda s, n: " ".join(_uuid(s) for _ in range(n)),
    "base64": lambda s, n: base64.b64encode(s.take(n * 12)).decode(),
    "base64_lines": lambda s, n: "\n".join(
        base64.b64encode(s.take(57)).decode() for _ in range(max(n // 6, 1))
    ),
    "digits_groups": lambda s, n: " ".join(s.pick(DIGITS, 12) for _ in range(n)),
    "digits_unbroken": lambda s, n: s.pick(DIGITS, n * 8),
    "numbers": lambda s, n: ", ".join(s.pick(DIGITS, 1 + s.below(9)) for _ in range(n)),
    "digits_spaced": lambda s, n: " ".join(s.pick(DIGITS, n * 4)),
    "digit_grid": _digit_grid(30),
    "bit_grid": _digit_grid(64),
    "whitespace_mixed": lambda s, n: "x" + s.pick(WHITESPACE, n * 4) + "x",
    "control_runs": lambda s, n: s.pick(CONTROL, n * 4),
    "printable": lambda s, n: s.pick("".join(chr(c) for c in range(33, 127)), n * 8),
    "latin_letters": _latin_words,
    "cjk": _chars(0x4E00, 0x9FFF),
    "cjk_ext_a": _chars(0x3400, 0x4DB6),
    "cjk_ext_a_spaced": lambda s, n: " ".join(_chars(0x3400, 0x4DB6)(s, n)),
    "hangul": _chars(0xAC00, 0xD7A3),
    "emoji": _chars(0x1F300, 0x1F64F),
    "spaced_controls": _spaced_singles(CONTROLS),
    "spaced_rare_singles": _spaced_singles(SINGLES),
    "newlines_mixed": _newlines,
    "chosen_words": lambda s, n: " ".join(CHOSEN_WORDS[i % 4] for i in range(n * 4)),
}


def corpus() -> dict[str, str]:
    """Every generated text by `name/size`."""
    return {
        f"{name}/{size}": build(Stream(f"{name}/{size}"), size)
        for name, build in BUILDERS.items()
        for size in SIZES
    }


def crc(text: str) -> int:
    return zlib.crc32(text.encode())


def baked() -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = json.loads(COUNTS.read_text("utf-8"))
    return counts


def _bake() -> None:  # pragma: no cover - a maintainer tool, run by hand
    import tiktoken  # noqa: PLC0415

    encodings = [tiktoken.get_encoding(n) for n in ENCODINGS]
    counts = {
        key: {"crc32": crc(text)}
        | {e.name: len(e.encode(text, disallowed_special=())) for e in encodings}
        for key, text in corpus().items()
    }
    COUNTS.write_text(json.dumps(counts, indent=1, sort_keys=True) + "\n", "utf-8")


if __name__ == "__main__":  # pragma: no cover
    _bake()
