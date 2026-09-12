"""Text normalization helpers for matching Beets albums against Redacted groups.

Redacted returns HTML-escaped strings and, by site convention, often carries both a
romanized and a native-script form of the same name in one field::

    Hiroshi Yoshimura (吉村弘)
    静けさの本: Static for Piano
    Quiet Forest (クワイエット・フォレスト)

Beets, meanwhile, stores whichever single form the tags happened to use. Comparing the
two raw strings scores ~0, so this module exists to (a) undo the escaping, (b) fold the
two sides onto a common normal form, and (c) enumerate the sub-names hiding inside a
combined string so either half can match.
"""

import html
import re
import unicodedata
from typing import Iterable

# Separators that join two renderings of the same name, e.g. "Native: Romanized".
_PART_SEPARATORS = (":", " - ", " / ", "|", "–", "—", "・", "/")

# Bracket pairs whose contents are frequently an alternate rendering of the outer text.
_BRACKET_PAIRS = (
    ("(", ")"),
    ("[", "]"),
    ("（", "）"),
    ("「", "」"),
    ("『", "』"),
    ("【", "】"),
    ("〈", "〉"),
    ("《", "》"),
)

# Strings that separate distinct artists rather than alternate names for one artist.
_ARTIST_SEPARATORS = re.compile(
    r"\s*(?:;|,|&|\+|/|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\bvs\.?\b|\band\b|、|，)\s*",
    re.IGNORECASE,
)

# Edition/format noise that appears on one side of a match but not the other.
_EDITION_NOISE = re.compile(
    r"\b("
    r"remaster(?:ed)?|re-?issue|deluxe|expanded|special|limited|anniversary|"
    r"bonus\s+track(?:s)?|digital|web|vinyl|cassette|explicit|clean|mono|stereo|"
    r"original\s+(?:soundtrack|mix)|ost|"
    r"\d+(?:st|nd|rd|th)\s+anniversary"
    r")\b",
    re.IGNORECASE,
)


def unescape(text: str) -> str:
    """Fully decode HTML entities, including double-encoded ones.

    Redacted returns e.g. ``Pier &amp; Loft`` and ``&#38745;&#12369;&#12373;&#12398;&#26412;``.
    Some fields are escaped twice (``&amp;amp;``), so iterate until the string settles.
    """
    if not text:
        return ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def fold_diacritics(text: str) -> str:
    """Strip combining marks from Latin characters only.

    Folding unconditionally would turn Japanese ``が`` into ``か`` (the dakuten is a
    combining mark), changing the word, so only marks attached to a Latin or ASCII base
    are dropped: ``Kankyō`` -> ``Kankyo`` while ``が`` is left alone.
    """
    out: list[str] = []
    latin_base = False
    for ch in unicodedata.normalize("NFD", text):
        if unicodedata.combining(ch):
            if not latin_base:
                out.append(ch)
            continue
        latin_base = ch.isascii() or "LATIN" in unicodedata.name(ch, "")
        out.append(ch)
    return unicodedata.normalize("NFC", "".join(out))


def normalize(text: str) -> str:
    """Reduce a name to a comparable normal form.

    Unescapes entities, applies NFKC (which folds halfwidth ``･`` onto ``・`` and
    fullwidth Latin onto ASCII), folds Latin diacritics, casefolds, and reduces every
    non-alphanumeric character to a single space. CJK characters are alphanumeric, so
    they survive; only punctuation is dropped.
    """
    if not text:
        return ""
    s = unescape(text)
    s = unicodedata.normalize("NFKC", s)
    s = fold_diacritics(s)
    s = s.casefold()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def strip_edition_noise(text: str) -> str:
    """Remove edition/format words that only one side of a comparison usually carries."""
    return re.sub(r"\s+", " ", _EDITION_NOISE.sub(" ", text)).strip()


def _split_brackets(text: str) -> list[str]:
    """Split ``Outer (Inner)`` into ``["Outer", "Inner"]``."""
    for open_ch, close_ch in _BRACKET_PAIRS:
        if open_ch not in text or not text.rstrip().endswith(close_ch):
            continue
        depth = 0
        for idx, ch in enumerate(text):
            if ch == open_ch:
                if depth == 0:
                    start = idx
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth == 0 and idx >= len(text.rstrip()) - 1:
                    outer = text[:start].strip()
                    inner = text[start + 1 : idx].strip()
                    return [p for p in (outer, inner) if p]
    return []


def _split_separators(text: str) -> list[str]:
    for sep in _PART_SEPARATORS:
        if sep in text:
            parts = [p.strip() for p in text.split(sep)]
            parts = [p for p in parts if p]
            if len(parts) > 1:
                return parts
    return []


def name_variants(text: str, max_depth: int = 3) -> list[str]:
    """Enumerate normalized renderings of a combined name, most complete first.

    ``"Quiet Forest (クワイエット・フォレスト)"`` yields the whole string plus
    ``"quiet forest"`` and ``"クワイエット フォレスト"``, so a Beets album tagged with
    either form scores 1.0 against the Redacted group.
    """
    if not text:
        return []

    seen: list[str] = []
    frontier = [text.strip()]
    for _ in range(max_depth):
        if not frontier:
            break
        next_frontier: list[str] = []
        for candidate in frontier:
            norm = normalize(candidate)
            # Require two characters so fragments like "I" or "&" don't match everything.
            if norm and len(norm) > 1 and norm not in seen:
                seen.append(norm)
            for part in _split_brackets(candidate) + _split_separators(candidate):
                if part and part != candidate:
                    next_frontier.append(part)
        frontier = next_frontier

    # An edition-stripped form of the full string, for "Album (2015 Remaster)" cases.
    if seen:
        stripped = strip_edition_noise(seen[0])
        if stripped and len(stripped) > 1 and stripped not in seen:
            seen.append(stripped)
    return seen


def split_artists(text: str) -> list[str]:
    """Split a multi-artist credit into individual artist names.

    ``"Hiroshi Yoshimura; KOTO VORTEX"`` and
    ``"Akio Suzuki/Takehisa Kosugi/Riri Shimada/Hiroshi Yoshimura"`` both become lists,
    so each credited artist can be looked up on Redacted independently.
    """
    if not text:
        return []
    parts = [p.strip() for p in _ARTIST_SEPARATORS.split(text)]
    return [p for p in parts if p and len(p) > 1]


def artist_variants(values: Iterable[str]) -> list[str]:
    """Build the full candidate set of normalized artist names from raw credits."""
    out: list[str] = []
    for value in values:
        if not value:
            continue
        for whole in name_variants(value):
            if whole not in out:
                out.append(whole)
        for artist in split_artists(value):
            for variant in name_variants(artist):
                if variant not in out:
                    out.append(variant)
    return out
