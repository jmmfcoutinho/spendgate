"""Inline media the guard can price: base64 images of four formats, nothing else.

The estimate holds a fixed token ceiling per image part. Any other inline media (video, audio,
PDF) costs the provider's own per-second or per-page rate, which the hold cannot see, so it must
be refused before routing (M-14). Every adapter that accepts inline media checks it here, so the
validator, the translator and the estimate agree on what an image is. Pure: no I/O.
"""

import re

IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
# Case-sensitive and anchored: a spelling the pattern does not know is refused, never guessed.
# The payload is base64 only, without line breaks, so a second data URL cannot ride inside it and
# every adapter reads the same bytes.
_BASE64 = r"[A-Za-z0-9+/]+={0,2}"
_INLINE_IMAGE = re.compile(rf"data:(image/(?:png|jpeg|webp|gif));base64,({_BASE64})")
_BASE64_ONLY = re.compile(_BASE64)
_REMOTE = re.compile(r"https?://", re.IGNORECASE)
IMAGE_URL_RULE = "must be an inline base64 data:image/png, jpeg, webp or gif URL"


def inline_image(url: object, allowed: frozenset[str] = IMAGE_MIME_TYPES) -> tuple[str, str] | None:
    """(MIME type, base64 payload) of an inline image data URL whose type is in `allowed` (an
    adapter passes its provider's subset); None for anything else."""
    match = _INLINE_IMAGE.fullmatch(url) if isinstance(url, str) else None
    return (match.group(1), match.group(2)) if match and match.group(1) in allowed else None


def is_base64(data: object) -> bool:
    """A base64 payload on one line, as every adapter accepts it."""
    return isinstance(data, str) and _BASE64_ONLY.fullmatch(data) is not None


def is_remote(url: str) -> bool:
    """An http(s) URL, admitted only with `allow_remote_media`: the provider fetches it and treats
    it as an image. spendgate never fetches it, so it cannot check what the URL serves."""
    return _REMOTE.match(url) is not None
