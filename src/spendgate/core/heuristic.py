"""Token count of text without a tokenizer, for the providers whose tokenizer we cannot run (M-14).

A BPE vocabulary merges common words but splits anything it has not seen, and the caller chooses
the text, so the rates come from random text, not prose (measured against o200k and cl100k in
tests/unit/test_estimate_upper_bound.py). Pure: no I/O, no imports from the rest of spendgate.

Rule                                                         Tokens
-----------------------------------------------------------  ---------------------------------
plain word: ASCII letters, a vowel, at most one capital,     1 per 2 letters
  and only the first
word that changes class (`a1b2`, `getUserId`, `HTTPS`,        each piece apart: 2 per 3 ASCII
  `qxzjv`)                                                      letters; a digit or an accented
                                                                letter its UTF-8 bytes
unbroken alphanumeric run over 12 characters                  at least 3 per 4 characters
run of CJK ideographs, kana or hangul                         5 per 2 characters
run of one kind of whitespace (spaces, tabs, `\n` or `\r\n`   1, plus 1 per 4 further
  lines with indentation)                                       characters
last space of a run, before an ASCII letter, a Latin word     free: it merges into what follows
  of 2+ letters, ASCII punctuation or a CJK run of 2+
anything else (symbols, `\r`, control characters, mixed       its UTF-8 bytes
  whitespace, other scripts, lone spaces elsewhere)

No rule costs more than one token per UTF-8 byte, which no byte-level BPE or byte-fallback
tokenizer exceeds: `utf8_bound` is that hard ceiling. Natural and random text is counted at or
above o200k and cl100k once the caller's 25% margin is added; text chosen to tokenize badly can
reach about 1.6 times that, never more than the byte bound.
"""

import re

PLAIN_WORD = (1, 2)
MIXED_PIECE = (2, 3)
LONG_RUN = 12
LONG_RUN_RATE = (3, 4)
CJK_RUN_RATE = (5, 2)
WS_PER_TOKEN = 4

_VOWELS = frozenset("aeiouAEIOU")
_CJK_RANGES = (("\u3000", "\u9fff"), ("\uac00", "\ud7af"), ("\uf900", "\ufaff"))
_CJK = "".join(f"{low}-{high}" for low, high in _CJK_RANGES)  # CJK ideographs, kana, hangul
_ACCENTED = "\u00c0-\u024f"  # Latin-1 supplement, Latin Extended-A and B
_LATIN = f"A-Za-z0-9{_ACCENTED}"
# Whitespace merges only in runs of one kind; any other whitespace or control character (`\r`,
# `\x0b`, `\x1c`, a space-tab or `\n`/`\r\n` mix) falls to `.` and costs its bytes.
_RUNS = re.compile(
    rf"(?P<word>[{_LATIN}]+)|(?P<cjk>[{_CJK}]+)|(?P<spaces> +)"
    r"|(?P<blank>\t+|\n+(?: +|\t+)?|(?:\r\n)+(?: +|\t+)?)|.",
    re.DOTALL,
)
# Pieces of a word: each digit, each non-ASCII letter, and each run of ASCII letters in one case
# (a leading capital stays with its word), so `a1b2`, `getUserId` and hex split where BPE splits.
_PIECES = re.compile(r"[0-9]|[^\x00-\x7f]|[A-Z]*[a-z]+|[A-Z]+")
# What a space merges into in o200k and cl100k: a letter run or ASCII punctuation. Never a number,
# a control character, other whitespace, or the bytes of a lone rare character.
_RIDES = re.compile(rf"[A-Za-z]|[{_ACCENTED}][A-Za-z{_ACCENTED}]|[!-/:-@\[-`{{-~]|[{_CJK}]{{2}}")
_ASCII_END = 0x80
_TWO_BYTE_END = 0x800
_BMP_END = 0x10000


def utf8_bound(text: str) -> int:
    """The hard ceiling: one token per UTF-8 byte."""
    return len(text.encode("utf-8", "surrogatepass"))


def symbol_tokens(chars: str) -> int:
    """One token per UTF-8 byte: what a byte-level tokenizer spends on a character at most."""
    total = 0
    for char in chars:
        code = ord(char)
        if code < _ASCII_END:
            total += 1
        elif code >= _BMP_END:
            total += 4
        else:
            total += 3 if code >= _TWO_BYTE_END else 2
    return total


def _rate(count: int, rate: tuple[int, int]) -> int:
    tokens, per = rate
    return -(-count * tokens // per)


def _plain(word: str) -> bool:
    """A word BPE vocabularies are built from: ASCII letters, a vowel, at most one capital.

    A capital after the first letter already splits the word into pieces in `_word_tokens`.
    """
    return (
        word.isascii()
        and word.isalpha()
        and sum(c.isupper() for c in word) <= 1
        and not _VOWELS.isdisjoint(word)
    )


def _word_tokens(word: str) -> int:
    """One alphanumeric run: plain words at the plain rate, anything else piece by piece."""
    pieces = _PIECES.findall(word)
    if len(pieces) == 1 and _plain(word):
        total = _rate(len(word), PLAIN_WORD)
    else:
        total = 0
        for piece in pieces:
            if piece.isascii() and piece.isalpha():
                total += _rate(len(piece), MIXED_PIECE)
            else:  # a digit (Gemini splits every one) or an accented letter: its UTF-8 bytes
                total += symbol_tokens(piece)
    if len(word) > LONG_RUN:
        total = max(total, _rate(len(word), LONG_RUN_RATE))
    return total


def heuristic_tokens(text: str) -> int:
    """Upper-bound token count without a tokenizer (the rules in the module table)."""
    total = 0
    for match in _RUNS.finditer(text):
        run, kind = match.group(), match.lastgroup
        if kind == "word":
            total += _word_tokens(run)
        elif kind == "cjk":
            total += _rate(len(run), CJK_RUN_RATE)
        elif kind in ("spaces", "blank"):
            rides = kind == "spaces" and _RIDES.match(text, match.end()) is not None
            total += -(-(len(run) - 1) // WS_PER_TOKEN) + (0 if rides else 1)
        else:
            total += symbol_tokens(run)
    return total
