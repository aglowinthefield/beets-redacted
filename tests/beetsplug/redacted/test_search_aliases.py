"""Tests for artist-alias resolution and search outcome reporting."""

from typing import Optional

import pytest

from beetsplug.redacted.exceptions import RedactedAPIFailure, RedactedError
from beetsplug.redacted.search import (
    SearchOutcome,
    candidate_artist_names,
    resolve_artist_by_name,
    search_album,
)
from beetsplug.redacted.types import (
    RedArtistResponse,
    RedArtistResponseResults,
    RedArtistTorrent,
    RedArtistTorrentGroup,
    RedUserResponse,
    RedUserResponseResults,
)
from beetsplug.redacted.utils.test_utils import FakeAlbum, FakeClient, FakeLibrary, FakeLogger

ARTIST_ID = 53689
RED_ARTIST_NAME = "Hiroshi Yoshimura (吉村弘)"


@pytest.fixture
def log() -> FakeLogger:
    return FakeLogger()


def make_album(**fields: object) -> FakeAlbum:
    data: dict[str, object] = {
        "id": 0,
        "albumartist": "Hiroshi Yoshimura",
        "albumartist_sort": "Yoshimura, Hiroshi",
        "album": "Quiet Forest",
        "albumdisambig": "",
        "year": 1996,
        "media": "CD",
        "format": "FLAC",
    }
    data.update(fields)
    return FakeLibrary([data]).albums()[0]


def make_artist_response(groups: Optional[list[RedArtistTorrentGroup]] = None) -> RedArtistResponse:
    return RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=ARTIST_ID,
            name=RED_ARTIST_NAME,
            notificationsEnabled=False,
            hasBookmarked=False,
            torrentgroup=groups or [],
        ),
    )


def make_group(name: str, year: int, group_id: int = 254496) -> RedArtistTorrentGroup:
    return RedArtistTorrentGroup(
        groupId=group_id,
        groupName=name,
        groupYear=year,
        torrent=[RedArtistTorrent(id=999, groupId=group_id, size=1024, media="CD", format="FLAC")],
    )


def empty_user_response() -> RedUserResponse:
    return RedUserResponse(
        status="success",
        response=RedUserResponseResults(seeding=[], leeching=[], uploaded=[], snatched=[]),
    )


# --- candidate_artist_names -------------------------------------------------------


def test_candidate_names_puts_albumartist_first() -> None:
    names = candidate_artist_names(make_album())
    assert names[0] == "Hiroshi Yoshimura"


def test_candidate_names_include_musicbrainz_aliases() -> None:
    names = candidate_artist_names(make_album(), alt_artists=["吉村弘", "Yoshimura Hiroshi"])
    assert "吉村弘" in names


def test_candidate_names_split_multi_artist_credits() -> None:
    album = make_album(albumartist="Hiroshi Yoshimura; KOTO VORTEX", albumartist_sort="")
    names = candidate_artist_names(album)
    assert "KOTO VORTEX" in names


def test_candidate_names_deduplicate_by_normalized_form() -> None:
    """Punctuation-only differences must not cost an extra API call."""
    album = make_album(albumartist="A･I･R", albumartist_sort="A・I・R")
    assert len(candidate_artist_names(album)) == 1


def test_candidate_names_respects_limit() -> None:
    album = make_album()
    aliases = [f"Alias {i}" for i in range(50)]
    assert len(candidate_artist_names(album, alt_artists=aliases, limit=4)) == 4


# --- resolve_artist_by_name -------------------------------------------------------


def test_resolve_artist_tries_names_until_one_resolves(log: FakeLogger) -> None:
    client = FakeClient()
    client.artist_name_responses["吉村弘"] = make_artist_response()

    response, name, saw_error = resolve_artist_by_name(
        client, ["Hiroshi Yoshimura", "吉村弘"], log
    )

    assert response is not None
    assert response.response.id == ARTIST_ID
    assert name == "吉村弘"
    assert saw_error is False
    # The first name was tried and missed before falling through to the alias.
    assert client.artist_name_queries == ["Hiroshi Yoshimura", "吉村弘"]


def test_resolve_artist_returns_none_when_no_name_matches(log: FakeLogger) -> None:
    response, name, saw_error = resolve_artist_by_name(FakeClient(), ["Nobody"], log)
    assert response is None and name is None and saw_error is False


def test_resolve_artist_flags_transport_errors(log: FakeLogger) -> None:
    """A network failure must not be mistaken for 'artist is not on Redacted'."""
    client = FakeClient()
    client.error_artist_name_queries.add("Hiroshi Yoshimura")

    response, _, saw_error = resolve_artist_by_name(client, ["Hiroshi Yoshimura"], log)

    assert response is None
    assert saw_error is True


def test_api_failure_is_not_a_transport_error(log: FakeLogger) -> None:
    assert issubclass(RedactedAPIFailure, RedactedError)


# --- search_album outcomes --------------------------------------------------------


def test_search_matches_album_across_scripts(log: FakeLogger) -> None:
    """The Beets tag is 'Quiet Forest'; Redacted names it with the katakana appended."""
    client = FakeClient()
    client.user_response = empty_user_response()
    client.artist_name_responses["Hiroshi Yoshimura"] = make_artist_response(
        [make_group("Quiet Forest (クワイエット・フォレスト)", 1996)]
    )

    result = search_album(make_album(), client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.MATCHED
    assert result.fields is not None
    assert result.fields.red_groupid == 254496
    assert result.score == pytest.approx(1.0)


def test_search_reports_no_artist_when_redacted_has_none(log: FakeLogger) -> None:
    client = FakeClient()
    client.user_response = empty_user_response()

    result = search_album(make_album(), client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.NO_ARTIST
    assert result.fields is None


def test_search_reports_no_album_match_when_artist_lacks_release(log: FakeLogger) -> None:
    """Artist is on Redacted but this release isn't: the upload candidate case."""
    client = FakeClient()
    client.user_response = empty_user_response()
    client.artist_name_responses["Hiroshi Yoshimura"] = make_artist_response(
        [make_group("Green", 1986, group_id=51631)]
    )

    result = search_album(make_album(), client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.NO_ALBUM_MATCH
    assert result.fields is None
    assert result.artist_id == ARTIST_ID


def test_search_reports_error_rather_than_absence_on_failure(log: FakeLogger) -> None:
    """An unreachable tracker must never be recorded as 'not on Redacted'."""
    client = FakeClient()
    client.user_response = empty_user_response()
    client.error_artist_name_queries.update(
        {"Hiroshi Yoshimura", "Yoshimura, Hiroshi", "Hiroshi Yoshimura; KOTO VORTEX"}
    )
    client.error_queries.update({"Hiroshi Yoshimura Quiet Forest"})

    result = search_album(make_album(), client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.ERROR


def test_search_uses_aliases_to_find_artist(log: FakeLogger) -> None:
    """Beets has only the Japanese name; Redacted only answers to the romanization."""

    class Aliases:
        def aliases_for_album(self, album: FakeAlbum) -> list[str]:
            return ["Hiroshi Yoshimura"]

    client = FakeClient()
    client.user_response = empty_user_response()
    client.artist_name_responses["Hiroshi Yoshimura"] = make_artist_response(
        [make_group("Quiet Forest (クワイエット・フォレスト)", 1996)]
    )

    album = make_album(albumartist="吉村弘", albumartist_sort="吉村弘")
    result = search_album(album, client, log, min_score=0.75, aliases=Aliases())

    assert result.outcome is SearchOutcome.MATCHED
    assert result.fields is not None
    assert result.fields.red_groupid == 254496
