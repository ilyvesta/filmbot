"""Hand a search from one bot to another through a t.me/<bot>?start=<payload> link.

Telegram allows only A-Z a-z 0-9 _ - in a start payload, at most 64 characters, so the search text is
base64url-encoded (no padding) behind a short prefix and cut at a word boundary if it's too long.
"""
from __future__ import annotations

import base64
import binascii

PREFIX = "q_"
MAX_PAYLOAD = 64


def encode_search(text: str) -> str:
    words = " ".join((text or "").split()).split(" ")
    while words:
        raw = base64.urlsafe_b64encode(" ".join(words).encode("utf-8")).decode().rstrip("=")
        if len(PREFIX) + len(raw) <= MAX_PAYLOAD:
            return PREFIX + raw
        words.pop()                                   # drop the last word and try again
    return ""


def decode_search(payload: str | None) -> str | None:
    """The search text, or None for anything that isn't a valid payload from us."""
    if not payload or not payload.startswith(PREFIX) or len(payload) > MAX_PAYLOAD:
        return None
    body = payload[len(PREFIX):]
    try:
        text = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    text = " ".join(text.split())
    return text[:100] or None
