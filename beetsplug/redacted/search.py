"""Module for searching and matching albums against Redacted torrents."""

import dataclasses
import enum
import functools
import itertools
import logging
import time
from collections.abc import Generator, Iterable
from typing import Callable, Optional, Union

from beets.library import Album  # type: ignore[import-untyped]
from pydantic import ValidationError
from ratelimit import RateLimitException  # type: ignore[import-untyped]

from beetsplug.redacted.aliases import AliasProvider
from beetsplug.redacted.client import Client
from beetsplug.redacted.exceptions import RedactedAPIFailure, RedactedError
from beetsplug.redacted.matching import Matchable, extract_album_fields, score_match
from beetsplug.redacted.types import (
    BeetsRedFields,
    RedArtistResponse,
    RedArtistResponseResults,
    RedArtistTorrent,
    RedArtistTorrentGroup,
    RedSearchResult,
    RedUserTorrent,
    TorrentType,
)
from beetsplug.redacted.utils.search_utils import normalize_query
from beetsplug.redacted.utils.text import normalize, split_artists


@dataclasses.dataclass
class RedTorrent:
    # Whether this torrent is preferred for matching, e.g. was drawn from the user's snatched
    # torrents, making it more likely to be in their library.
    preferred: bool = False

    artist_id: Optional[int] = None
    artist: Optional[str] = None

    group_id: Optional[int] = None
    group: Optional[str] = None
    year: Optional[int] = None

    torrent_id: Optional[int] = None
    edition_id: Optional[int] = None
    remastered: Optional[bool] = None
    remaster_year: Optional[int] = None
    remaster_catalogue_number: Optional[str] = None
    remaster_title: Optional[str] = None
    media: Optional[str] = None
    encoding: Optional[str] = None
    format: Optional[str] = None
    has_log: Optional[bool] = None
    log_score: Optional[int] = None
    has_cue: Optional[bool] = None
    scene: Optional[bool] = None
    vanity_house: Optional[bool] = None
    file_count: Optional[int] = None
    torrent_time: Optional[str] = None
    size: Optional[int] = None
    snatches: Optional[int] = None
    seeders: Optional[int] = None
    leechers: Optional[int] = None
    is_freeleech: Optional[bool] = None
    is_neutral_leech: Optional[bool] = None
    is_freeload: Optional[bool] = None
    is_personal_freeleech: Optional[bool] = None
    trumpable: Optional[bool] = None
    can_use_token: Optional[bool] = None

    tags: Optional[list[str]] = None
    bookmarked: Optional[bool] = None
    release_type: Optional[str] = None
    group_time: Optional[int] = None

    @classmethod
    def from_search_result(
        cls, result: RedSearchResult, log: logging.Logger
    ) -> Generator["RedTorrent", None, None]:
        log.debug("Creating RedTorrent from search result: {0}", result)
        base = cls(
            preferred=False,
            group_id=result.groupId,
            group=result.groupName,
            artist=result.artist,
            tags=result.tags,
            bookmarked=result.bookmarked,
            vanity_house=result.vanityHouse,
            year=result.groupYear,
            release_type=result.releaseType,
            group_time=result.groupTime,
        )
        if result.torrents is None:
            yield base
        else:
            for torrent in result.torrents:
                copy = dataclasses.replace(
                    base,
                    torrent_id=torrent.torrentId,
                    artist_id=torrent.artists[0].id if torrent.artists else None,
                    edition_id=torrent.editionId,
                    remastered=torrent.remastered,
                    remaster_year=torrent.remasterYear,
                    remaster_catalogue_number=torrent.remasterCatalogueNumber,
                    remaster_title=torrent.remasterTitle,
                    media=torrent.media,
                    encoding=torrent.encoding,
                    format=torrent.format,
                    has_log=torrent.hasLog,
                    log_score=torrent.logScore,
                    has_cue=torrent.hasCue,
                    scene=torrent.scene,
                    vanity_house=torrent.vanityHouse,
                    file_count=torrent.fileCount,
                    torrent_time=torrent.time,
                    size=torrent.size,
                    snatches=torrent.snatches,
                    seeders=torrent.seeders,
                    leechers=torrent.leechers,
                    is_freeleech=torrent.isFreeleech,
                    is_neutral_leech=torrent.isNeutralLeech,
                    is_freeload=torrent.isFreeload,
                    is_personal_freeleech=torrent.isPersonalFreeleech,
                    trumpable=torrent.trumpable,
                    can_use_token=torrent.canUseToken,
                )
                yield copy

    @classmethod
    def from_user_torrent(cls, torrent: RedUserTorrent, log: logging.Logger) -> "RedTorrent":
        log.debug("Creating RedTorrent from user torrent: {0}", torrent)
        return cls(
            preferred=True,
            artist_id=torrent.artistId,
            artist=torrent.artistName,
            group_id=torrent.groupId,
            group=torrent.name,
            torrent_id=torrent.torrentId,
        )


def red_torrent_matchable(torrent: RedTorrent) -> Optional[Matchable]:
    """Extract normalized fields from a Redacted torrent.

    Args:
        torrent: Torrent to extract fields from

    Returns:
        MatchableFields object with normalized fields
    """
    if not torrent.artist or not torrent.group:
        return None
    return Matchable(artist=torrent.artist, title=torrent.group, year=torrent.year)


def artist_torrent_group_matchable(
    group: RedArtistTorrentGroup, artist_name: Union[str, None]
) -> Union[Matchable, None]:
    """Extract normalized fields from a Redacted artist torrent group.

    Args:
        group: Artist torrent group to extract fields from
        artist_name: Artist name to use for the match fields

    Returns:
        MatchableFields object with normalized fields
    """
    if not group.groupName or not artist_name:
        return None
    return Matchable(artist=artist_name, title=group.groupName, year=group.groupYear)


def match_album(
    album: Album, results: Iterable[RedTorrent], log: logging.Logger
) -> tuple[Optional[RedTorrent], float]:
    """Check if an album exists in search results.

    The matching algorithm uses a weighted scoring system:
    - Artist name similarity: 50% weight
    - Album name similarity: 40% weight
    - Year similarity: 10% weight

    This weighting prioritizes exact artist matches while allowing for some flexibility
    in album names and years. The year similarity is particularly lenient, allowing
    matches within 1 year of difference.

    Args:
        album: Beets album to match
        results: Search results from Redacted API
        log: Logger instance for logging messages

    Returns:
        Tuple of (group, torrent) if found with sufficient similarity,
        None otherwise. The group contains the album information and the torrent
        contains the specific release information.
    """
    # Extract album fields for matching
    album_fields = extract_album_fields(album)

    # Find the best match among all groups
    best_match: Union[RedTorrent, None] = None
    best_match_score: float = 0.0
    weights = {"artist": 0.5, "title": 0.4, "year": 0.1}

    # Score all the groups, keeping track of the best match
    for torrent in results:
        group_fields = red_torrent_matchable(torrent)
        if not group_fields:
            log.debug(
                "Could not extract matching fields from torrent {:d}, skipping", torrent.torrent_id
            )
            continue

        log.debug(
            "Matching torrent {0} - {1} ({2:d}) against album {3} - {4} ({5:d})",
            torrent.artist,
            torrent.group,
            torrent.year or 0,
            album.albumartist,
            album.album,
            album.year or 0,
        )
        match_result = score_match(album_fields, group_fields, log, weights)

        if match_result.total_score > best_match_score:
            best_match = torrent
            best_match_score = match_result.total_score

    if not best_match:
        log.debug(
            "No match found in search results for {} - {} ({:d})",
            album.albumartist,
            album.album,
            album.year,
        )
        return None, 0.0

    log.debug(
        "Found match for {} - {} ({:d}): {} - {} (score: {:.2f})",
        album_fields.artist,
        album_fields.title,
        album_fields.year,
        best_match.artist,
        best_match.group,
        best_match_score,
    )
    return best_match, best_match_score


def match_artist_album_scored(
    album: Album,
    artist_response: RedArtistResponse,
    preferred_torrents: list[RedTorrent],
    log: logging.Logger,
    min_score: float,
    alt_artists: Optional[list[str]] = None,
) -> tuple[Union[RedArtistTorrentGroup, None], Union[RedArtistTorrent, None], float]:
    """Match an album against an artist's torrent groups, returning the match score.

    Args:
        album: Beets album to match
        artist_response: Artist data from Redacted API
        preferred_torrents: Torrents already known to be in the user's collection
        log: Logger instance for logging messages
        min_score: Minimum similarity score to consider a match (0-1)
        alt_artists: Additional names for the album's artist, e.g. MusicBrainz aliases

    Returns:
        Tuple of (matching group, matching torrent, score). The group and torrent are
        None when nothing scored above min_score.
    """
    album_fields = extract_album_fields(album, alt_artists=alt_artists)

    artist_data = artist_response.response
    torrent_groups = artist_data.torrentgroup

    if not torrent_groups:
        log.debug("Artist {:s} has no torrent groups", artist_data.name)
        return None, None, 0.0

    # Find the best match among all artist's torrent groups
    best_group: Union[RedArtistTorrentGroup, None] = None
    best_torrent: Union[RedArtistTorrent, None] = None
    best_match_score: float = 0.0

    # Title and year weights are more important when artist is already known
    #
    # TODO: Make these weights configurable
    weights = {"artist": 0.2, "title": 0.7, "year": 0.1}

    pt_ids = set(torrent.torrent_id for torrent in preferred_torrents)

    # Score all the groups, keeping track of the best match
    for group in torrent_groups:
        if not group.torrent:
            log.debug("Artist group {:s} has no torrents, skipping", group.groupName)
            continue

        # If this group has the preferred torrent ID, use it as the best match
        preferred_torrent = next((t for t in group.torrent if t.id in pt_ids), None)
        if preferred_torrent:
            return group, preferred_torrent, 1.0

        group_fields = artist_torrent_group_matchable(group, artist_data.name)
        if not group_fields:
            log.debug(
                "Could not extract matching fields from artist group {0}, skipping", group.groupName
            )
            continue

        # Score match considering that we know we're matching against the correct artist
        match_result = score_match(album_fields, group_fields, log, weights)

        # Choose the best matching torrent amongst the group (largest size)
        #
        # TODO: We should consider other factors like format, media, and encoding
        match_best_torrent = max(group.torrent, key=lambda x: x.size or 0)
        if not match_best_torrent:
            log.debug("Artist group {:s} has no torrents, skipping", group.groupName)
            continue

        if match_result.total_score > best_match_score:
            best_match_score = match_result.total_score
            best_torrent = match_best_torrent
            best_group = group

    if not best_group or not best_torrent:
        log.debug(
            "No match with torrents found in artist {0}'s groups (checked {1:d} groups, "
            "best score: {2:.2f}). Artist response: id={3:d}, name={4}",
            artist_data.name,
            len(torrent_groups),
            best_match_score,
            artist_data.id,
            artist_data.name,
        )
        return None, None, best_match_score

    if best_match_score < min_score:
        log.debug(
            "Best match for {0} in artist's groups was {1} (score: {2:.2f}, "
            "below threshold {3:.2f})",
            album_fields.title,
            best_group.groupName,
            best_match_score,
            min_score,
        )
        return None, None, best_match_score

    log.debug(
        "Best match for {0} from artist's groups: {1} (score: {2:.2f})",
        album_fields.title,
        best_group.groupName,
        best_match_score,
    )

    return best_group, best_torrent, best_match_score


def match_artist_album(
    album: Album,
    artist_response: RedArtistResponse,
    preferred_torrents: list[RedTorrent],
    log: logging.Logger,
    min_score: float,
    alt_artists: Optional[list[str]] = None,
) -> tuple[Union[RedArtistTorrentGroup, None], Union[RedArtistTorrent, None]]:
    """Match an album against artist's torrent groups.

    Thin wrapper over match_artist_album_scored that drops the score.
    """
    group, torrent, _ = match_artist_album_scored(
        album, artist_response, preferred_torrents, log, min_score, alt_artists
    )
    return group, torrent


def beets_fields_from_artist_torrent_groups(
    artist: RedArtistResponseResults,
    group: RedArtistTorrentGroup,
    torrent: RedArtistTorrent,
    log: logging.Logger,
) -> Union[BeetsRedFields, None]:
    """Extract fields from an artist group and torrent match.

    Args:
        artist: Artist result containing the match
        group: Group containing the match
        torrent: Matching torrent
        log: Logger instance for logging messages
    Returns:
        RedTorrentFields object with fields to update on the album, or None if validation fails
    """
    fields = None

    for field in dataclasses.fields(BeetsRedFields):
        from_meta = field.metadata.get("from")
        if not from_meta:
            continue

        source_cls = from_meta.get_source_cls()
        source_obj: Union[
            RedArtistResponseResults, RedArtistTorrentGroup, RedArtistTorrent, None
        ] = None
        if source_cls == RedArtistResponseResults:
            source_obj = artist
        elif source_cls == RedArtistTorrentGroup:
            source_obj = group
        elif source_cls == RedArtistTorrent:
            source_obj = torrent
        else:
            log.debug("Unsupported source class: {0}", source_cls)
            continue

        value = from_meta.get_value(source_obj)
        if not value:
            if field.metadata.get("required", False):
                log.debug("Field {0} is required but has no value, skipping match.", field.name)
                return None
            else:
                continue

        try:
            if fields is None:
                fields = BeetsRedFields()
            setattr(fields, field.name, value)
        except (ValueError, TypeError, ValidationError) as e:
            log.debug(
                "Error mapping field {0} from source ({1}) to Beets field ({2}).\n"
                "    Source class: {3}, value: {4}\n"
                "    Error: {5}",
                field.name,
                source_cls,
                field.name,
                source_cls,
                value,
                e,
            )
            return None

    return fields


def best_match_from_snatched(
    client: Client, album: Album, log: logging.Logger
) -> tuple[Optional[RedTorrent], float]:
    # Look up the user's snatched torrents. We will always add these in to the set of torrents
    # to match.
    log.debug("Retrieving user's snatched torrents")
    user_response = client.user(TorrentType.SNATCHED)
    if not (user_response and user_response.response and user_response.response.snatched):
        log.debug("No snatched torrents found")
        return None, 0.0

    snatched_torrents = [
        RedTorrent.from_user_torrent(torrent, log) for torrent in user_response.response.snatched
    ]

    # Is there a good match in the user's snatched torrents?
    return match_album(album, snatched_torrents, log)


def best_match_from_search(
    client: Client, album: Album, c_artist: str, c_album: str, log: logging.Logger
) -> tuple[Optional[RedTorrent], float]:
    log.debug("Searching for torrents matching {0} - {1}", c_artist, c_album)
    search_query = normalize_query(c_artist, c_album, log)
    if not search_query:
        log.debug("Could not construct search query for {0} - {1}", c_artist, c_album)
        return None, 0.0

    log.debug("Searching for torrents with query: {0}", search_query)
    search_response = client.search(search_query)

    if not (search_response and search_response.response and search_response.response.results):
        log.debug("No search results found")
        return None, 0.0

    results = [
        RedTorrent.from_search_result(group, log) for group in search_response.response.results
    ]

    return match_album(album, itertools.chain.from_iterable(results), log)


class SearchOutcome(enum.Enum):
    """Why a search ended the way it did.

    The distinction matters for finding albums that are *not* on Redacted: a missing
    red_groupid on its own is ambiguous between "the tracker does not have this" and
    "the lookup failed". Only NO_ARTIST and NO_ALBUM_MATCH are positive evidence of
    absence; ERROR means the album's status is still unknown and should be retried.
    """

    MATCHED = "matched"
    NO_ALBUM_MATCH = "no_album_match"  # Artist is on Redacted, this release is not
    NO_ARTIST = "no_artist"  # Redacted has no such artist under any known name
    ERROR = "error"  # Lookup failed; nothing can be concluded


@dataclasses.dataclass
class SearchResult:
    """Outcome of searching Redacted for one album."""

    outcome: SearchOutcome
    fields: Optional[BeetsRedFields] = None
    score: float = 0.0
    artist_id: Optional[int] = None
    artist_name: Optional[str] = None


def candidate_artist_names(
    album: Album, alt_artists: Optional[list[str]] = None, limit: int = 8
) -> list[str]:
    """Build an ordered list of names to look this album's artist up under.

    Beets' own spellings come first, then MusicBrainz aliases (which supply the other
    script for e.g. Hiroshi Yoshimura / 吉村弘), then the individual members of a
    multi-artist credit. Names are de-duplicated by normalized form so trivial
    punctuation differences don't cost an API call.
    """
    raw: list[str] = []

    def add(value: object) -> None:
        if not value:
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
            return
        text = str(value).strip()
        if text:
            raw.append(text)

    add(album.get("albumartist", None))
    add(alt_artists or [])
    add(album.get("albumartists", None))
    add(album.get("albumartist_credit", None))
    add(album.get("albumartist_sort", None))

    # A credit like "Hiroshi Yoshimura; KOTO VORTEX" won't resolve as a whole, but its
    # members will.
    expanded = list(raw)
    for name in raw:
        expanded.extend(split_artists(name))

    out: list[str] = []
    seen: set[str] = set()
    for name in expanded:
        key = normalize(name)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def resolve_artist_by_name(
    client: Client, names: list[str], log: logging.Logger
) -> tuple[Optional[RedArtistResponse], Optional[str], bool]:
    """Find an artist on Redacted by trying each candidate name in turn.

    Redacted resolves its own artist aliases, so any registered spelling reaches the
    same artist page. Returns the first hit, the name that found it, and whether any
    attempt failed for a reason other than "no such artist" (which would make a
    negative result untrustworthy).
    """
    saw_error = False
    for name in names:
        try:
            response = client.get_artist_by_name(name)
        except RedactedAPIFailure:
            log.debug("Redacted has no artist named {0}", name)
            continue
        except (RedactedError, RateLimitException) as e:
            log.debug("Error looking up Redacted artist by name {0}: {1}", name, e)
            saw_error = True
            continue

        if response and response.response and response.response.id:
            log.debug(
                "Resolved Redacted artist {0} (id {1}) from name {2}",
                response.response.name,
                response.response.id,
                name,
            )
            return response, name, saw_error

    return None, None, saw_error


def search_album(
    album: Album,
    client: Client,
    log: logging.Logger,
    min_score: float,
    aliases: Optional[AliasProvider] = None,
) -> SearchResult:
    """Search Redacted for an album, reporting why the search ended as it did.

    Resolution is artist-first: look the artist up by each name we know for them, which
    is both cheaper and far more reliable than free-text search, then match the release
    within that artist's discography. The old free-text 'browse' search remains as a
    fallback for artists Redacted has under a name we don't hold.
    """
    alt_artists: list[str] = []
    if aliases is not None:
        alt_artists = aliases.aliases_for_album(album)

    saw_error = False

    # Snatched torrents are strong evidence: the user already has this release, so a
    # hit here both identifies the artist and pins the exact torrent.
    preferred_torrents: list[RedTorrent] = []
    snatched_artist_id: Optional[int] = None
    try:
        snatched_match, snatched_score = best_match_from_snatched(client, album, log)
        # Only trust a snatch that clears the threshold: the best of the user's snatched
        # torrents is still returned when it is a poor match, and following its artist id
        # would send the discography lookup to the wrong artist entirely.
        if snatched_match and snatched_score >= min_score:
            preferred_torrents.append(snatched_match)
            snatched_artist_id = snatched_match.artist_id
    except (RedactedError, RateLimitException) as e:
        log.debug("Error retrieving snatched torrents: {0}", e)
        saw_error = True

    # Primary strategy: resolve the artist directly by name.
    names = candidate_artist_names(album, alt_artists)
    log.debug("Artist name candidates for {0}: {1}", album.get("album"), names)
    artist_response, matched_name, name_error = resolve_artist_by_name(client, names, log)
    saw_error = saw_error or name_error

    # Fallback: free-text search, used only to discover an artist id.
    if artist_response is None:
        artist_id: Optional[int] = snatched_artist_id

        if artist_id is None:
            for c_artist, c_album in itertools.product(
                (album.albumartist, album.albumartist_credit, album.albumartist_sort),
                (album.album, album.albumdisambig),
            ):
                if not c_artist or not c_album:
                    continue
                try:
                    match, score = best_match_from_search(client, album, c_artist, c_album, log)
                except (RedactedError, RateLimitException) as e:
                    log.debug("Error searching for {0} - {1}: {2}", c_artist, c_album, e)
                    saw_error = True
                    continue
                if match and score >= min_score and match.artist_id:
                    artist_id = match.artist_id
                    break

        if artist_id is None:
            outcome = SearchOutcome.ERROR if saw_error else SearchOutcome.NO_ARTIST
            log.debug(
                "No Redacted artist found for {0} - {1} ({2})",
                album.get("albumartist"),
                album.get("album"),
                outcome.value,
            )
            return SearchResult(outcome=outcome)

        try:
            artist_response = client.get_artist(artist_id)
        except (RedactedError, RateLimitException) as e:
            log.debug("Error looking up artist {0}: {1}", artist_id, e)
            return SearchResult(outcome=SearchOutcome.ERROR)

    artist_data = artist_response.response
    group, torrent, score = match_artist_album_scored(
        album, artist_response, preferred_torrents, log, min_score, alt_artists
    )

    if group and torrent:
        fields = beets_fields_from_artist_torrent_groups(artist_data, group, torrent, log)
        if fields is not None:
            return SearchResult(
                outcome=SearchOutcome.MATCHED,
                fields=fields,
                score=score,
                artist_id=artist_data.id,
                artist_name=artist_data.name,
            )
        log.debug("Could not build Beets fields for {0}, treating as no match", album.get("album"))

    log.debug(
        "Redacted artist {0} has no release matching {1} (best score {2:.2f})",
        artist_data.name,
        album.get("album"),
        score,
    )
    return SearchResult(
        outcome=SearchOutcome.ERROR if saw_error else SearchOutcome.NO_ALBUM_MATCH,
        score=score,
        artist_id=artist_data.id,
        artist_name=artist_data.name,
    )


def search(
    album: Album,
    client: Client,
    log: logging.Logger,
    min_score: float,
    aliases: Optional[AliasProvider] = None,
) -> Union[BeetsRedFields, None]:
    """Search for Redacted torrents matching an album.

    Returns the fields to set on the album, or None if there was no match. Callers that
    need to tell "not on Redacted" apart from "lookup failed" should use search_album.
    """
    return search_album(album, client, log, min_score, aliases).fields


def apply_search_result(album: Album, result: SearchResult) -> bool:
    """Write a search outcome onto an album. Returns True if metadata changed.

    An ERROR writes nothing at all: the album's status is unknown, and recording it
    would make a network failure indistinguishable from the tracker not having the
    release.

    The bookkeeping stamp (red_checked/red_status/red_score) is deliberately excluded
    from the return value, which reports only whether real Redacted metadata changed.
    red_checked changes on every run, so folding it in would mark every album modified.
    """
    if result.outcome is SearchOutcome.ERROR:
        return False

    album.red_checked = time.time()
    album.red_status = result.outcome.value
    album.red_score = result.score

    # Record which artist was searched even when the release wasn't found, so a
    # "not on Redacted" verdict can be audited: Redacted resolves its own aliases, and
    # a name can legitimately resolve to an artist page under a different name.
    if result.artist_id:
        album.red_artistid = result.artist_id
        if result.artist_name:
            album.red_artist = result.artist_name

    changed = False
    if result.fields is not None:
        for key, value in result.fields.__dict__.items():
            # Skip unset fields. BeetsRedFields leaves anything the API didn't supply
            # as None, including red_mtime, which it never populates -- copying those
            # over would wipe good data and make every already-matched album look
            # modified on every run.
            if value is None:
                continue
            if album.get(key) != value:
                album[key] = value
                changed = True

    if changed:
        album.red_mtime = time.time()
    return changed
