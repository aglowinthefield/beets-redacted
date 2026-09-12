"""Artist alias resolution via MusicBrainz.

Redacted resolves its own artist aliases server-side, so looking an artist up by name
usually finds them in either script. But that only works for aliases someone has actually
registered on Redacted, and it only helps if the name Beets holds is one of them.

MusicBrainz maintains a far more complete alias set -- native script, romanizations,
legal names, common misspellings -- so when the library has ``吉村弘`` and Redacted only
knows ``Hiroshi Yoshimura`` (or vice versa), MusicBrainz supplies the bridge. Nearly
every album in a well-tagged library carries ``mb_albumartistid``, which makes this a
direct lookup rather than a search.

Results are cached on disk and keyed by MBID. Artists repeat constantly across a
library, and MusicBrainz asks for no more than one request per second, so the cache is
what makes scanning a whole library practical.
"""

import logging
from typing import Any, Optional

from beets.library import Album  # type: ignore[import-untyped]

try:  # pragma: no cover - musicbrainzngs ships with beets
    import musicbrainzngs  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    musicbrainzngs = None  # type: ignore[assignment]

try:  # pragma: no cover
    import diskcache  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    diskcache = None  # type: ignore[assignment]

# Cached for a month: alias sets are effectively static, and a negative result
# (artist has no aliases) is just as worth keeping as a positive one.
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60

_USER_AGENT = ("beets-redacted", "0.2.0", "https://github.com/judas-red/beets-redacted")


class AliasProvider:
    """Looks up alternate names for an artist, with an on-disk cache."""

    def __init__(
        self, log: logging.Logger, cache_dir: Optional[str] = None, enabled: bool = True
    ) -> None:
        self.log = log
        self.enabled = enabled and musicbrainzngs is not None
        self._cache: Any = None
        self._configured = False

        if not self.enabled:
            if musicbrainzngs is None:
                self.log.debug("redacted: musicbrainzngs unavailable, artist aliases disabled")
            return

        if cache_dir and diskcache is not None:
            try:
                self._cache = diskcache.Cache(cache_dir)
            except Exception as e:  # pragma: no cover - cache is best-effort
                self.log.debug("redacted: could not open alias cache at {0}: {1}", cache_dir, e)

    def _configure(self) -> None:
        """Set the MusicBrainz user agent once, lazily."""
        if self._configured:
            return
        musicbrainzngs.set_useragent(*_USER_AGENT)
        self._configured = True

    def aliases_for_mbid(self, mbid: str) -> list[str]:
        """Return alternate names for a MusicBrainz artist ID.

        Returns an empty list on any failure: aliases only ever widen matching, so a
        MusicBrainz outage should degrade the results, never break the run.
        """
        if not self.enabled or not mbid:
            return []

        if self._cache is not None:
            cached = self._cache.get(mbid)
            if cached is not None:
                return list(cached)

        names: list[str] = []
        try:
            self._configure()
            result = musicbrainzngs.get_artist_by_id(mbid, includes=["aliases"])
            artist = result.get("artist", {})

            for key in ("name", "sort-name"):
                value = artist.get(key)
                if value:
                    names.append(value)

            for alias in artist.get("alias-list", []) or []:
                for key in ("alias", "sort-name"):
                    value = alias.get(key)
                    if value:
                        names.append(value)
        except Exception as e:
            # Network error, 404, rate limit: all non-fatal.
            self.log.debug("redacted: MusicBrainz alias lookup failed for {0}: {1}", mbid, e)
            return []

        # De-duplicate, preserving order.
        deduped: list[str] = []
        for name in names:
            if name not in deduped:
                deduped.append(name)

        if self._cache is not None:
            try:
                self._cache.set(mbid, deduped, expire=CACHE_TTL_SECONDS)
            except Exception as e:  # pragma: no cover
                self.log.debug("redacted: could not cache aliases for {0}: {1}", mbid, e)

        self.log.debug("redacted: {0} aliases for MusicBrainz artist {1}", len(deduped), mbid)
        return deduped

    def aliases_for_album(self, album: Album) -> list[str]:
        """Return alternate artist names for a Beets album, via its album-artist MBID."""
        mbid = album.get("mb_albumartistid", None)
        if not mbid:
            return []
        return self.aliases_for_mbid(str(mbid))

    def close(self) -> None:
        """Close the on-disk cache, if one is open."""
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:  # pragma: no cover
                pass
