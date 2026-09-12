"""Tests for artist-alias resolution and search outcome reporting."""

from typing import Optional

import pytest

from beetsplug.redacted.exceptions import RedactedAPIFailure, RedactedError
from beetsplug.redacted.search import (
    SearchOutcome,
    SearchResult,
    apply_search_result,
    candidate_artist_names,
    resolve_artist_by_name,
    search_album,
)
from beetsplug.redacted.types import (
    BeetsRedFields,
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

    response, name, saw_error = resolve_artist_by_name(client, ["Hiroshi Yoshimura", "吉村弘"], log)

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


# --- apply_search_result ----------------------------------------------------------


def test_apply_result_stamps_absence() -> None:
    """A 'not on Redacted' verdict must be recorded, and record which artist we checked."""
    album = make_album()
    result = SearchResult(
        outcome=SearchOutcome.NO_ALBUM_MATCH,
        score=0.4,
        artist_id=ARTIST_ID,
        artist_name=RED_ARTIST_NAME,
    )

    changed = apply_search_result(album, result)

    assert changed is False  # no Redacted metadata changed
    assert album.red_status == "no_album_match"
    assert album.red_checked
    assert album.red_score == pytest.approx(0.4)
    # The artist that was searched, so the verdict can be audited.
    assert album.red_artistid == ARTIST_ID
    assert album.red_artist == RED_ARTIST_NAME
    assert not album.get("red_groupid")


def test_apply_result_writes_nothing_on_error() -> None:
    """A failed lookup must never be recorded as absence."""
    album = make_album()
    changed = apply_search_result(album, SearchResult(outcome=SearchOutcome.ERROR))

    assert changed is False
    assert not album.get("red_status")
    assert not album.get("red_checked")


def test_apply_result_sets_mtime_only_when_metadata_changes() -> None:
    album = make_album()
    fields = BeetsRedFields(red_groupid=254496, red_torrentid=999)
    result = SearchResult(outcome=SearchOutcome.MATCHED, fields=fields, score=1.0)

    assert apply_search_result(album, result) is True
    first_mtime = album.red_mtime
    assert first_mtime

    # Re-applying identical fields is not a metadata change, so red_mtime holds.
    assert apply_search_result(album, result) is False
    assert album.red_mtime == first_mtime


def test_command_keeps_its_alias_provider() -> None:
    """ui.Subcommand.__init__ assigns self.aliases (the command's alternate names).

    Storing the alias provider there gets it silently overwritten with an empty tuple,
    and the command then crashes calling .aliases_for_album() on a tuple.
    """
    from beetsplug.redacted.command import RedactedCommand
    from beetsplug.redacted.utils.test_utils import FakeConfig

    class Provider:
        def aliases_for_album(self, album: object) -> list[str]:
            return ["吉村弘"]

    provider = Provider()
    command = RedactedCommand(FakeConfig(), FakeLogger(), FakeClient(), provider)

    assert command._alias_provider is provider
    # The base class's own attribute must be left alone.
    assert command.aliases == ()


def test_search_falls_through_a_dead_end_collaboration_artist(log: FakeLogger) -> None:
    """Redacted has artist pages for collaborations that hold none of the releases.

    'Harold Budd & Clive Wright' is a real Redacted artist page with no torrent groups;
    the duo's albums sit under Harold Budd. Stopping at the first name that resolves
    reported those albums as absent when they are not.
    """
    client = FakeClient()
    client.user_response = empty_user_response()

    # The combined page exists but is empty.
    client.artist_name_responses["Harold Budd & Clive Wright"] = RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=999, name="Harold Budd & Clive Wright", torrentgroup=[]
        ),
    )
    # The real release lives under the solo artist.
    client.artist_name_responses["Harold Budd"] = RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=111, name="Harold Budd", torrentgroup=[make_group("Candylion", 2009, group_id=555)]
        ),
    )

    album = make_album(
        albumartist="Harold Budd & Clive Wright", albumartist_sort="", album="Candylion", year=2009
    )
    result = search_album(album, client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.MATCHED
    assert result.fields is not None
    assert result.fields.red_groupid == 555
    # The empty collaboration page was tried first, then fallen through.
    assert client.artist_name_queries[0] == "Harold Budd & Clive Wright"


def test_search_reports_the_best_artist_when_several_are_tried(log: FakeLogger) -> None:
    """With no match anywhere, report the artist that came closest, not the last tried."""
    client = FakeClient()
    client.user_response = empty_user_response()
    client.artist_name_responses["Harold Budd & Clive Wright"] = RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=999, name="Harold Budd & Clive Wright", torrentgroup=[]
        ),
    )
    client.artist_name_responses["Harold Budd"] = RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=111, name="Harold Budd", torrentgroup=[make_group("Candylion", 2009, group_id=555)]
        ),
    )

    album = make_album(
        albumartist="Harold Budd & Clive Wright",
        albumartist_sort="",
        album="Something Else Entirely",
        year=2009,
    )
    result = search_album(album, client, log, min_score=0.75)

    assert result.outcome is SearchOutcome.NO_ALBUM_MATCH
    assert result.artist_id == 111  # Harold Budd scored higher than the empty page


def test_candidate_names_offer_an_ascii_spelling() -> None:
    """Redacted matches artist names exactly and rejects MusicBrainz's punctuation.

    "Beverly Glenn‐Copeland" (U+2010 HYPHEN, which NFKC does not fold) returns "no
    artist found", while the ASCII spelling resolves. Both must be offered.
    """
    album = make_album(albumartist="Beverly Glenn‐Copeland", albumartist_sort="")
    names = candidate_artist_names(album)

    assert "Beverly Glenn‐Copeland" in names
    assert "Beverly Glenn-Copeland" in names


def test_candidate_names_still_dedupe_nfkc_equivalents() -> None:
    """Halfwidth and fullwidth middle dots are the same string to the server."""
    album = make_album(albumartist="A･I･R", albumartist_sort="A・I・R")
    assert len(candidate_artist_names(album)) == 1
