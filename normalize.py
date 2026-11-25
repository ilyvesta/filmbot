"""Text normalisation shared by the indexer and the search box.

Both the stored titles and whatever a user types go through the same
function, so symbols, accents, fancy Unicode fonts and casing can never
make two spellings of the same title look different.
"""
from __future__ import annotations

import re
import unicodedata

# Letters that NFKD does not decompose, folded by hand. Covers Hausa
# (ɓ ɗ ƙ ƴ), Yoruba/Igbo extras (ɛ ɔ ŋ) and a few European ones.
_FOLD = str.maketrans({
    "ɓ": "b", "ɗ": "d", "ƙ": "k", "ƴ": "y", "ŋ": "n", "ɛ": "e", "ɔ": "o",
    "ø": "o", "æ": "ae", "œ": "oe", "ł": "l", "đ": "d", "ı": "i", "ß": "ss",
})
_APOSTROPHES = re.compile(r"[’‘'`´]")
_ARTICLES = {"the", "a", "an"}


def normalize(text: str | None) -> str:
    """Lowercase, accent-free, symbol-free, single-spaced."""
    if not text:
        return ""
    # NFKC folds styled letters (𝗪𝗵𝗮𝘁, ｗｈａｔ, ﬁ) down to plain ones.
    t = unicodedata.normalize("NFKC", text).replace("&", " and ")
    t = _APOSTROPHES.sub("", t.casefold()).translate(_FOLD)
    # Drop accents, but only on Latin letters: stripping combining marks
    # from Devanagari or Arabic would mangle those words.
    out: list[str] = []
    base_is_latin = False
    for ch in unicodedata.normalize("NFKD", t):
        if unicodedata.category(ch) == "Mn":
            if base_is_latin:
                continue
        else:
            base_is_latin = ord(ch) < 0x250
        out.append(ch)
    # Keep letters, digits and combining marks (Devanagari/Arabic vowel signs
    # are marks, so a plain \W split would shred those words); all else -> space.
    kept = "".join(c if unicodedata.category(c)[0] in "LNM" else " " for c in out)
    return unicodedata.normalize("NFC", " ".join(kept.split()))


def core_key(norm: str) -> str:
    """Normalised text without a leading article ('the dark knight' -> 'dark knight')."""
    words = norm.split()
    if len(words) > 1 and words[0] in _ARTICLES:
        words = words[1:]
    return " ".join(words)
