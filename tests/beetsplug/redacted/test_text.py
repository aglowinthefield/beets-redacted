"""Tests for text normalization and name-variant generation.

The cases here are drawn from real Redacted responses (artist 53689, Hiroshi
Yoshimura) and the matching Beets tags, which is where the original bugs showed up.
"""

import pytest

from beetsplug.redacted.matching import best_similarity, string_similarity
from beetsplug.redacted.utils.text import (
    fold_diacritics,
    name_variants,
    normalize,
    split_artists,
    unescape,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Pier &amp; Loft", "Pier & Loft"),
        ("Hiroshi Yoshimura&#39;s Ears", "Hiroshi Yoshimura's Ears"),
        ("&#38745;&#12369;&#12373;&#12398;&#26412;", "静けさの本"),
        ("A&#12539;I&#12539;R", "A・I・R"),
        ("Ben B&ouml;hmer", "Ben Böhmer"),
        # Double-encoded, which Redacted occasionally returns.
        ("Pier &amp;amp; Loft", "Pier & Loft"),
        ("", ""),
    ],
)
def test_unescape(raw: str, expected: str) -> None:
    assert unescape(raw) == expected


def test_fold_diacritics_folds_latin() -> None:
    assert fold_diacritics("Kankyō Ongaku") == "Kankyo Ongaku"
    assert fold_diacritics("Ben Böhmer") == "Ben Bohmer"


def test_fold_diacritics_leaves_kana_alone() -> None:
    """Dakuten are combining marks; folding them would change the word."""
    assert fold_diacritics("がぎぐげご") == "がぎぐげご"
    assert fold_diacritics("ヴォーカル") == "ヴォーカル"


@pytest.mark.parametrize(
    "raw, expected",
    [
        # NFKC folds the halfwidth katakana middle dot onto the fullwidth one.
        ("A･I･R", "a i r"),
        ("A・I・R", "a i r"),
        ("Pier &amp; Loft", "pier loft"),
        ("Continuous Performance No. 3 - 24.3.1977", "continuous performance no 3 24 3 1977"),
        ("Kankyō Ongaku", "kankyo ongaku"),
        ("吉村弘の耳「音の島」", "吉村弘の耳 音の島"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw, expected_variant",
    [
        ("Hiroshi Yoshimura (吉村弘)", "吉村弘"),
        ("Quiet Forest (クワイエット・フォレスト)", "quiet forest"),
        ("静けさの本: Static for Piano", "静けさの本"),
        ("Music for Nine Post Cards (Wave Notation 1)", "music for nine post cards"),
        ("Koto Vortex I: Works By Hiroshi Yoshimura", "koto vortex i"),
    ],
)
def test_name_variants_extracts_alternate_rendering(raw: str, expected_variant: str) -> None:
    assert expected_variant in name_variants(raw)


def test_name_variants_keeps_full_string_first() -> None:
    variants = name_variants("Quiet Forest (クワイエット・フォレスト)")
    assert variants[0] == normalize("Quiet Forest (クワイエット・フォレスト)")


def test_name_variants_drops_single_character_fragments() -> None:
    """Fragments that short would match almost anything."""
    assert "i" not in name_variants("Koto Vortex: I")


@pytest.mark.parametrize(
    "raw, expected",
    [
        (
            "Akio Suzuki/Takehisa Kosugi/Riri Shimada/Hiroshi Yoshimura",
            ["Akio Suzuki", "Takehisa Kosugi", "Riri Shimada", "Hiroshi Yoshimura"],
        ),
        ("Hiroshi Yoshimura; KOTO VORTEX", ["Hiroshi Yoshimura", "KOTO VORTEX"]),
        ("Nujabes feat. Shing02", ["Nujabes", "Shing02"]),
        ("Solo Artist", ["Solo Artist"]),
    ],
)
def test_split_artists(raw: str, expected: list[str]) -> None:
    assert split_artists(raw) == expected


# Each pair is a real album that the plugin previously failed to match: the Beets
# tag on the left, the Redacted group name (as returned, escaped) on the right.
REAL_FALSE_NEGATIVES = [
    ("Continuous Performance no. 3 24.3.1977", "Continuous Performance No. 3 - 24.3.1977"),
    ("A･I･R: AIR IN RESORT", "A&#12539;I&#12539;R (Air In Resort)"),
    (
        "吉村弘の耳「音の島」",
        "Hiroshi Yoshimura&#39;s Ears &#39;Island of Sound&#39; "
        "(&#21513;&#26449;&#24344;&#12398;&#32819; &#12300;&#38899;&#12398;&#23798;&#12301;)",
    ),
    (
        "Quiet Forest",
        "Quiet Forest (&#12463;&#12527;&#12452;&#12456;&#12483;&#12488;&#12539;"
        "&#12501;&#12457;&#12524;&#12473;&#12488;)",
    ),
    ("KOTO VORTEX・I", "Koto Vortex I: Works By Hiroshi Yoshimura"),
    ("静けさの本", "&#38745;&#12369;&#12373;&#12398;&#26412;: Static for Piano"),
    ("Pier & Loft", "Pier &amp; Loft"),
    ("MUSIC FOR NINE POST CARDS", "Music for Nine Post Cards (Wave Notation 1)"),
]


@pytest.mark.parametrize("beets_title, red_title", REAL_FALSE_NEGATIVES)
def test_real_false_negatives_now_match(beets_title: str, red_title: str) -> None:
    """Every one of these is on Redacted and previously scored too low to match."""
    score = best_similarity(name_variants(beets_title), name_variants(red_title))
    assert score == pytest.approx(1.0), f"{beets_title!r} vs {red_title!r} scored {score}"


@pytest.mark.parametrize("beets_artist", ["Hiroshi Yoshimura", "吉村弘"])
def test_artist_matches_redacted_bilingual_name(beets_artist: str) -> None:
    """Redacted names this artist 'Hiroshi Yoshimura (吉村弘)'; either tag should match."""
    score = best_similarity(
        name_variants(beets_artist), name_variants("Hiroshi Yoshimura (吉村弘)")
    )
    assert score == pytest.approx(1.0)


def test_unrelated_names_still_score_low() -> None:
    """Variant expansion must not make everything match everything."""
    assert best_similarity(name_variants("Green"), name_variants("Wet Land")) < 0.5
    assert string_similarity("Hiroshi Yoshimura", "Susumu Yokota") < 0.6


def test_partial_variant_does_not_create_a_false_match() -> None:
    """Splitting a bracketed title must not fuzzy-match an unrelated release.

    'Diluvian 无 (Music For Self Extinction)' is not on Redacted, but its bracketed
    fragment is 0.74 similar to the same artist's 'Music for Self Esteem', which was
    enough to claim a match and silently drop a real upload candidate.
    """
    score = best_similarity(
        name_variants("Diluvian 无 (Music For Self Extinction)"),
        name_variants("Music for Self Esteem"),
    )
    assert score < 0.7

    # The real release still matches itself exactly.
    assert best_similarity(
        name_variants("Music For Self Esteem"), name_variants("Music for Self Esteem")
    ) == pytest.approx(1.0)


# Classical and compilation titles use "/" to separate the works on a release, not to
# give an alternate rendering of one name. A shared work must not make two different
# releases look like the same one.
SHARED_WORK_NOT_SAME_RELEASE = [
    ("Live / Electric Music", "The Desert Music (Live)"),
    ("Four Organs / Phase Patterns", "New York Counterpoint / Eight Lines / Four Organs"),
    ("Eight Lines / City Life", "Sextet / Piano Phase / Eight Lines"),
]


@pytest.mark.parametrize("beets_title, red_title", SHARED_WORK_NOT_SAME_RELEASE)
def test_shared_work_is_not_a_match(beets_title: str, red_title: str) -> None:
    score = best_similarity(name_variants(beets_title), name_variants(red_title))
    assert score < 0.9, f"{beets_title!r} wrongly matched {red_title!r} at {score}"


@pytest.mark.parametrize(
    "beets_title, red_title",
    [
        # The fragment is the whole of the Beets title.
        ("Quiet Forest", "Quiet Forest (クワイエット・フォレスト)"),
        # The fragment is the whole of the Redacted title (an A-side/B-side single).
        ("Campo / Outside With You", "Campo"),
    ],
)
def test_fragment_that_is_a_whole_title_still_matches(beets_title: str, red_title: str) -> None:
    score = best_similarity(name_variants(beets_title), name_variants(red_title))
    assert score == pytest.approx(1.0)
