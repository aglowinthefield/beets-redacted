"""Module for matching albums against Redacted results."""

import logging
from difflib import SequenceMatcher
from typing import Optional, Union

from beets.library import Album  # type: ignore[import-untyped]
from pydantic import Field
from pydantic.dataclasses import dataclass

from beetsplug.redacted.utils.text import artist_variants, name_variants, normalize, work_set


@dataclass
class Matchable:
    """Common fields that can be used for matching.

    ``artist`` and ``title`` are the raw display strings. Because Redacted and Beets
    frequently spell the same release differently -- romanized vs native script, with or
    without a bracketed alternate -- each is expanded into a list of normalized
    renderings at construction time, and scoring compares every rendering on one side
    against every rendering on the other.
    """

    # Required fields with defaults to avoid initialization issues
    artist: str = ""
    title: str = ""

    # Optional fields
    year: Union[int, None] = None
    media: Union[str, None] = None
    format: Union[str, None] = None

    # Additional artist names known to refer to the same artist, e.g. MusicBrainz
    # aliases. Folded into the artist variants used for scoring.
    alt_artists: list[str] = Field(default_factory=list)

    # Normalized renderings, derived in __post_init__.
    artist_forms: list[str] = Field(default_factory=list)
    title_forms: list[str] = Field(default_factory=list)
    title_works: frozenset[str] = Field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.artist_forms:
            self.artist_forms = artist_variants([self.artist, *self.alt_artists])
        if not self.title_forms:
            self.title_forms = name_variants(self.title)
        if not self.title_works:
            self.title_works = work_set(self.title)


@dataclass
class MatchResult:
    """Result of a match scoring operation."""

    total_score: float
    field_scores: dict[str, float]


def string_similarity(a: str, b: str) -> float:
    """Calculate similarity between two strings after normalization.

    Normalization decodes HTML entities, applies NFKC, folds Latin diacritics and
    strips punctuation, so ``Pier &amp; Loft`` and ``Pier & Loft`` compare equal.

    Args:
        a: First string
        b: Second string

    Returns:
        Similarity score between 0 and 1
    """
    if not a or not b:
        return 0.0
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def best_similarity(a_forms: list[str], b_forms: list[str]) -> float:
    """Similarity between two names, each given as its normalized renderings.

    A Beets album tagged ``静けさの本`` and a Redacted group named
    ``静けさの本: Static for Piano`` share a rendering, so this returns 1.0 where a
    direct string comparison returns 0.

    Sub-renderings only count when they match *exactly*. They exist to find the same
    name written differently, not to fuzzy-match fragments: splitting
    ``Diluvian 无 (Music For Self Extinction)`` yields ``music for self extinction``,
    which is 0.74 similar to an unrelated ``Music for Self Esteem`` and was enough to
    claim a false match. Every genuine cross-script case is an exact hit, so requiring
    exactness costs nothing and removes that whole class of error. Anything less falls
    back to comparing the names in full.
    """
    if not a_forms or not b_forms:
        return 0.0

    # name_variants puts the complete normalized string first.
    full = (
        1.0 if a_forms[0] == b_forms[0] else SequenceMatcher(None, a_forms[0], b_forms[0]).ratio()
    )
    if full == 1.0:
        return 1.0

    for a in a_forms:
        for b in b_forms:
            # A sub-rendering must be the *whole* of one side's name. That is what
            # distinguishes "the same release written differently" from "this work also
            # appears on that compilation": "Quiet Forest" is the entire Beets title of
            # "Quiet Forest (クワイエット・フォレスト)", but "Four Organs" is merely one
            # work of three on both "Four Organs / Phase Patterns" and "New York
            # Counterpoint / Eight Lines / Four Organs", which are different releases.
            if a == b and (a == a_forms[0] or b == b_forms[0]):
                return 1.0

    return full


def year_similarity(year1: Union[int, None], year2: Union[int, None]) -> float:
    """Calculate year similarity.

    Args:
        year1: First year
        year2: Second year

    Returns:
        Similarity score between 0 and 1
    """
    if year1 is None or year2 is None:
        return 1.0  # If either year is missing, don't penalize the match

    if year1 == year2:
        return 1.0

    if abs(year1 - year2) <= 1:
        return 0.5  # If years are within 1 year, consider it a partial match

    # Reissues of older material are routinely dated to the reissue year on one side
    # and the original recording year on the other, so decay rather than hard-zero.
    if abs(year1 - year2) <= 3:
        return 0.25

    return 0.0  # Different years are considered completely different


def extract_album_fields(album: Album, alt_artists: Optional[list[str]] = None) -> Matchable:
    """Extract normalized fields from a Beets album.

    Args:
        album: Beets album to extract fields from
        alt_artists: Additional names known to refer to the same artist (e.g. MusicBrainz
            aliases), used to match releases credited in another script.

    Returns:
        MatchableFields object with normalized fields
    """
    assert isinstance(album, Album), f"album must be a Beets Album, got {type(album)}"

    year = album.get("year", None)
    if year == 0:
        year = None

    # Every spelling Beets knows for this album's artist feeds the variant set.
    credits = [
        album.get("albumartist", "") or "",
        album.get("albumartist_credit", "") or "",
        album.get("albumartist_sort", "") or "",
    ]
    albumartists = album.get("albumartists", None)
    if isinstance(albumartists, (list, tuple)):
        credits.extend(str(a) for a in albumartists)
    elif albumartists:
        credits.append(str(albumartists))
    credits.extend(alt_artists or [])

    return Matchable(
        artist=album.get("albumartist"),
        title=album.get("album"),
        year=year,
        media=album.get("media", None) if "media" in album else None,
        format=album.get("format", None) if "format" in album else None,
        alt_artists=[c for c in credits if c],
    )


def score_match(
    item1: Matchable,
    item2: Matchable,
    log: logging.Logger,
    weights: Union[dict[str, float], None] = None,
) -> MatchResult:
    """Score the match between two items.

    Args:
        item1: First item fields
        item2: Second item fields
        log: Logger to log scoring details
        weights: Optional dictionary of field weights. Defaults to:
                 {"artist": 0.5, "title": 0.4, "year": 0.1}

    Returns:
        MatchResult containing total score and individual field scores
    """
    if weights is None:
        weights = {"artist": 0.5, "title": 0.4, "year": 0.1}

    scores: dict[str, float] = {}

    # Compare every known rendering of each side, taking the best pairing.
    scores["artist"] = best_similarity(item1.artist_forms, item2.artist_forms)
    scores["title"] = best_similarity(item1.title_forms, item2.title_forms)

    # When both titles list several works, how far the two sets agree is a better
    # signal than string similarity, which is thrown off by ordering. Only ever
    # raises the score, so a single work listed on a compilation cannot use this to
    # claim the whole release.
    works1, works2 = item1.title_works, item2.title_works
    if len(works1) > 1 and len(works2) > 1:
        overlap = len(works1 & works2) / len(works1 | works2)
        scores["title"] = max(scores["title"], overlap)

    # Calculate optional field similarities if both items have the field
    if item1.year is not None and item2.year is not None:
        scores["year"] = year_similarity(item1.year, item2.year)
    else:
        # Default to 1.0 if either doesn't have year (don't penalize)
        scores["year"] = 1.0

    # Calculate total score based on weights
    total_score = sum(scores.get(field, 0.0) * weight for field, weight in weights.items())

    # Normalize to ensure total score is between 0 and 1
    weight_sum = sum(weights.values())
    if weight_sum > 0:
        total_score /= weight_sum

    # Log scoring details
    field_details = "\n".join(f"\t{field}: {score:.2f}" for field, score in scores.items())
    log.debug(
        "Scoring {} - {} against {} - {}:\n" "{}\n" "\tTotal score: {:.2f}",
        item1.artist,
        item1.title,
        item2.artist,
        item2.title,
        field_details,
        total_score,
    )

    return MatchResult(total_score=total_score, field_scores=scores)
