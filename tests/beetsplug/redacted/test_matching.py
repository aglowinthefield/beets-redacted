"""Tests for matching.py functionality."""

from typing import Any, Union

import pytest

from beetsplug.redacted.matching import (
    Matchable,
    extract_album_fields,
    score_match,
    string_similarity,
    year_similarity,
)
from beetsplug.redacted.utils.test_utils import FakeLibrary, FakeLogger


@pytest.fixture
def log() -> FakeLogger:
    """Create a fake logger."""
    return FakeLogger()


@pytest.mark.parametrize(
    "str1, str2, expected_range",
    [
        # Exact match
        ("Test", "Test", (1.0, 1.0)),
        # Case insensitive
        ("Test", "TEST", (1.0, 1.0)),
        # Partial match
        ("Test", "Tst", (0.5, 1.0)),
        # No match
        ("Test", "Something", (0.0, 0.5)),
        # Empty string
        ("Test", "", (0.0, 0.0)),
        ("", "Test", (0.0, 0.0)),
        ("", "", (0.0, 0.0)),
    ],
)
def test_string_similarity(str1: str, str2: str, expected_range: tuple[float, float]) -> None:
    """Test string_similarity function with various string combinations."""
    min_expected, max_expected = expected_range
    similarity = string_similarity(str1, str2)
    assert min_expected <= similarity <= max_expected


@pytest.mark.parametrize(
    "year1, year2, expected",
    [
        # Exact match
        (2020, 2020, 1.0),
        # Within 1 year
        (2020, 2019, 0.5),
        (2020, 2021, 0.5),
        # Within 3 years: reissues are often dated to the reissue on one side and the
        # original recording on the other, so decay rather than hard-zero.
        (2020, 2018, 0.25),
        (2020, 2022, 0.25),
        (2020, 2023, 0.25),
        # More than 3 years difference
        (2020, 2015, 0.0),
        (2020, 2025, 0.0),
        # None values
        (None, 2020, 1.0),
        (2020, None, 1.0),
        (None, None, 1.0),
    ],
)
def test_year_similarity(year1: Union[int, None], year2: Union[int, None], expected: float) -> None:
    """Test year_similarity function with various year combinations."""
    assert year_similarity(year1, year2) == expected


@pytest.mark.parametrize(
    "test_case, album_data, expected_fields",
    [
        (
            "complete_fields",
            {
                "id": 1,
                "albumartist": "Test Artist",
                "album": "Test Album",
                "year": 2020,
                "media": "CD",
                "format": "FLAC",
            },
            Matchable(
                artist="Test Artist", title="Test Album", year=2020, media="CD", format="FLAC"
            ),
        ),
        (
            "minimal_fields",
            {
                "id": 2,
                "albumartist": "Test Artist",
                "album": "Test Album",
                # Year is intentionally omitted to test that behavior
            },
            Matchable(artist="Test Artist", title="Test Album"),
        ),
        (
            "empty_optional_fields",
            {
                "id": 3,
                "albumartist": "Test Artist",
                "album": "Test Album",
                "year": None,
                "media": "",
                "format": "",
            },
            Matchable(artist="Test Artist", title="Test Album", year=None, media="", format=""),
        ),
    ],
    ids=["complete_fields", "minimal_fields", "empty_optional_fields"],
)
def test_extract_album_fields(
    test_case: str, album_data: dict[str, Any], expected_fields: Matchable
) -> None:
    """Test extract_album_fields function with various album data combinations."""
    lib = FakeLibrary([album_data])
    album = lib.albums()[0]

    fields = extract_album_fields(album)

    assert fields.artist == expected_fields.artist
    assert fields.title == expected_fields.title
    assert fields.year == expected_fields.year
    assert fields.media == expected_fields.media
    assert fields.format == expected_fields.format


@pytest.mark.parametrize(
    "test_case, item1, item2, expected_scores, weights",
    [
        (
            "exact_match",
            Matchable(artist="Test Artist", title="Test Album", year=2020),
            Matchable(artist="Test Artist", title="Test Album", year=2020),
            {
                "total": (0.99, 1.0),
                "artist": (0.99, 1.0),
                "title": (0.99, 1.0),
                "year": (0.99, 1.0),
            },
            None,
        ),
        (
            "partial_match",
            Matchable(artist="Test Artist", title="Test Album", year=2020),
            Matchable(artist="Test Artst", title="Test Albm", year=2019),
            {"total": (0.7, 1.0), "artist": (0.8, 1.0), "title": (0.8, 1.0), "year": (0.5, 0.5)},
            None,
        ),
        (
            "missing_year",
            Matchable(artist="Test Artist", title="Test Album", year=2020),
            Matchable(artist="Test Artist", title="Test Album"),
            {"total": (0.9, 1.0), "artist": (0.99, 1.0), "title": (0.99, 1.0), "year": (1.0, 1.0)},
            None,
        ),
        (
            "custom_weights",
            Matchable(artist="Test Artist", title="Test Album", year=2020),
            Matchable(artist="Test Artst", title="Test Albm", year=2019),
            {"total": (0.7, 1.0)},
            {"artist": 0.7, "title": 0.2, "year": 0.1},
        ),
    ],
    ids=["exact_match", "partial_match", "missing_year", "custom_weights"],
)
def test_score_match(
    log: FakeLogger,
    test_case: str,
    item1: Matchable,
    item2: Matchable,
    expected_scores: dict[str, tuple[float, float]],
    weights: Union[dict[str, float], None],
) -> None:
    """Test score_match function with various match scenarios."""
    result = score_match(item1, item2, log, weights)

    # Check total score
    min_total, max_total = expected_scores["total"]
    assert min_total <= result.total_score <= max_total

    # Check individual field scores if provided
    for field, (min_score, max_score) in expected_scores.items():
        if field != "total" and field in result.field_scores:
            assert min_score <= result.field_scores[field] <= max_score


# Title pairs checked by hand against Redacted: the left is the Beets tag, the right the
# group name. SAME pairs are the same release, DIFFERENT pairs are not. Kept together
# because the threshold between them is what decides whether an album is reported as an
# upload candidate, and because character similarity alone does not separate them --
# "City Life / Proverb" (same) scores 0.67 while "Hide (Amtrac Remix)" (different)
# scores 0.72.
SAME_RELEASE = [
    ("Quiet Forest", "Quiet Forest (クワイエット・フォレスト)"),
    ("静けさの本", "静けさの本: Static for Piano"),
    ("MUSIC FOR NINE POST CARDS", "Music for Nine Post Cards (Wave Notation 1)"),
    ("KOTO VORTEX・I", "Koto Vortex I: Works By Hiroshi Yoshimura"),
    ("Campo / Outside With You", "Campo"),
    ("Postman & Magazine", "Postman b/w Magazine"),
    ("Boo Boo", "Boo Boo (Chopped & Screwed)"),
    ("Wind in Lonely Fences: 1970–2011", "Wind In Lonely Fences 1970 - 2011"),
    (
        "Clapping Music / Music for Pieces of Wood / Sextet",
        "Sextet - Music for Pieces of Wood - Clapping Music",
    ),
    ("City Life / Proverb", "Proverb / Nagoya Marimbas / City Life"),
    ("Jane 1-11", "Jane 1 - 11"),
    ("Lyin, Parts 1 - 4 / Would Be", "Lyin (Pt. 1-4) b/w Would Be"),
    ("Music for 3 Pianos", "Music For Three Pianos"),
    # A spelling difference in a short title must survive the token gate.
    ("Colours", "Colors"),
]

DIFFERENT_RELEASE = [
    ("Diluvian 无 (Music For Self Extinction)", "Music for Self Esteem"),
    ("Live / Electric Music", "The Desert Music (Live)"),
    ("Four Organs / Phase Patterns", "New York Counterpoint / Eight Lines / Four Organs"),
    ("Eight Lines / City Life", "Sextet / Piano Phase / Eight Lines"),
    ("Revise", "Everis"),
    ("Hide (Amtrac Remix)", "UNKLE x Amtrac - Remixes"),
    ("American Classics: Minimalism", "Minimalism - Winter Landscapes"),
]

ARTIST_WEIGHTS = {"artist": 0.2, "title": 0.7, "year": 0.1}
DEFAULT_MIN_SCORE = 0.75


@pytest.mark.parametrize("beets_title, red_title", SAME_RELEASE)
def test_same_release_scores_above_threshold(
    log: FakeLogger, beets_title: str, red_title: str
) -> None:
    result = score_match(
        Matchable(artist="An Artist", title=beets_title, year=2000),
        Matchable(artist="An Artist", title=red_title, year=2000),
        log,
        ARTIST_WEIGHTS,
    )
    assert result.total_score >= DEFAULT_MIN_SCORE


@pytest.mark.parametrize("beets_title, red_title", DIFFERENT_RELEASE)
def test_different_release_scores_below_threshold(
    log: FakeLogger, beets_title: str, red_title: str
) -> None:
    result = score_match(
        Matchable(artist="An Artist", title=beets_title, year=2000),
        Matchable(artist="An Artist", title=red_title, year=2000),
        log,
        ARTIST_WEIGHTS,
    )
    assert result.total_score < DEFAULT_MIN_SCORE
