# type: ignore
"""Tests for decoding of the HTML-escaped strings Redacted returns."""

import pytest

from beetsplug.redacted.types import (
    RedArtistResponse,
    RedArtistResponseResults,
    RedArtistTorrentGroup,
    RedSearchResponse,
)


def artist_response(group_name: str, artist_name: str = "Artist") -> RedArtistResponse:
    return RedArtistResponse(
        status="success",
        response=RedArtistResponseResults(
            id=1,
            name=artist_name,
            torrentgroup=[RedArtistTorrentGroup(groupId=1, groupName=group_name)],
        ),
    )


@pytest.mark.parametrize(
    "escaped, expected",
    [
        ("Pier &amp; Loft", "Pier & Loft"),
        ("Hiroshi Yoshimura&#39;s Ears", "Hiroshi Yoshimura's Ears"),
        ("&#38745;&#12369;&#12373;&#12398;&#26412;", "静けさの本"),
        ("A&#12539;I&#12539;R", "A・I・R"),
        ("Ben B&ouml;hmer", "Ben Böhmer"),
        # Redacted occasionally returns a doubly-escaped string.
        ("Pier &amp;amp; Loft", "Pier & Loft"),
        ("Nothing to decode", "Nothing to decode"),
    ],
)
def test_group_name_is_decoded(escaped: str, expected: str) -> None:
    response = artist_response(escaped)
    assert response.response.torrentgroup[0].groupName == expected


def test_artist_name_is_decoded() -> None:
    response = artist_response("Group", "Hiroshi Yoshimura (&#21513;&#26449;&#24344;)")
    assert response.response.name == "Hiroshi Yoshimura (吉村弘)"


def test_search_results_are_decoded() -> None:
    response = RedSearchResponse(
        **{
            "status": "success",
            "response": {
                "results": [
                    {"groupId": 1, "groupName": "Pier &amp; Loft", "artist": "Ben B&ouml;hmer"}
                ]
            },
        }
    )
    result = response.response.results[0]
    assert result.groupName == "Pier & Loft"
    assert result.artist == "Ben Böhmer"


def test_none_and_empty_are_left_alone() -> None:
    group = RedArtistTorrentGroup(groupId=1, groupName=None)
    assert group.groupName is None
    assert artist_response("").response.torrentgroup[0].groupName == ""
