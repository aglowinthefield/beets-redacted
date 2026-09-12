#!/usr/bin/env python3
"""Beets plugin for Redacted integration."""

import os
import time
from pkgutil import extend_path
from typing import Optional

import frozendict
from beets.dbcore import types as dbtypes  # type: ignore[import-untyped]
from beets.importer import ImportTask  # type: ignore[import-untyped]
from beets.library import Library  # type: ignore[import-untyped]
from beets import ui  # type: ignore[import-untyped]
from beets.plugins import BeetsPlugin  # type: ignore[import-untyped]

from beetsplug.redacted.aliases import AliasProvider
from beetsplug.redacted.client import Client
from beetsplug.redacted.command import RedactedCommand, RedactedMissingCommand
from beetsplug.redacted.http import CachedRequestsClient, HTTPClient
from beetsplug.redacted.search import SearchOutcome, search_album

__path__ = extend_path(__path__, __name__)


class RedactedPlugin(BeetsPlugin):
    """Plugin for searching Redacted requests."""

    BASE_URL = "https://redacted.sh/ajax.php"

    album_types = frozendict.frozendict(
        {
            "red_groupid": dbtypes.INTEGER,
            "red_mtime": dbtypes.FLOAT,
            # When this album was last looked up, and what came back. A missing
            # red_groupid alone can't distinguish "not on Redacted" from "never
            # searched" or "the search errored", which is exactly the distinction
            # needed to find albums worth uploading.
            "red_checked": dbtypes.FLOAT,
            "red_status": dbtypes.STRING,
            "red_score": dbtypes.FLOAT,
            "red_torrentid": dbtypes.INTEGER,
            "red_remastered": dbtypes.BOOLEAN,
            "red_remasteryear": dbtypes.INTEGER,
            "red_remastercatalogue": dbtypes.STRING,
            "red_remastertitle": dbtypes.STRING,
            "red_media": dbtypes.STRING,
            "red_encoding": dbtypes.STRING,
            "red_format": dbtypes.STRING,
            "red_haslog": dbtypes.BOOLEAN,
            "red_logscore": dbtypes.INTEGER,
            "red_hascue": dbtypes.BOOLEAN,
            "red_scene": dbtypes.BOOLEAN,
            "red_vanityhouse": dbtypes.BOOLEAN,
            "red_filecount": dbtypes.INTEGER,
            "red_time": dbtypes.STRING,
            "red_size": dbtypes.INTEGER,
            "red_snatches": dbtypes.INTEGER,
            "red_seeders": dbtypes.INTEGER,
            "red_leechers": dbtypes.INTEGER,
            "red_isfreeleech": dbtypes.BOOLEAN,
            "red_isneutralleech": dbtypes.BOOLEAN,
            "red_isfreeload": dbtypes.BOOLEAN,
            "red_ispersonalfreeleech": dbtypes.BOOLEAN,
            "red_trumpable": dbtypes.BOOLEAN,
            "red_canusetoken": dbtypes.BOOLEAN,
            "red_requestid": dbtypes.INTEGER,
            "red_bounty": dbtypes.INTEGER,
        }
    )

    default_config = frozendict.frozendict(
        {
            "api_key": None,
            "user_id": None,
            "min_score": 0.75,
            "auto": False,
            # Look up artist aliases on MusicBrainz, so a release credited in one
            # script matches a Redacted artist credited in another.
            "aliases": True,
            "alias_cache_dir": None,
        }
    )

    def __init__(self) -> None:
        """Initialize the plugin."""
        super().__init__()

        self.config.add(self.default_config)
        self._min_score = self.config["min_score"].as_number()

        self._http_client = CachedRequestsClient(self.BASE_URL, self._log)
        self._client = self._get_client(self._http_client)

        cache_dir = self.config["alias_cache_dir"].get() or os.path.expanduser(
            "~/.cache/redacted/aliases"
        )
        self._aliases = AliasProvider(
            self._log, cache_dir=str(cache_dir), enabled=self.config["aliases"].get(bool)
        )

        # Register import stage if 'auto' is True
        if self._client and self.config["auto"].get(bool):
            self.import_stages = [self.import_stage]

        self.register_listener("cli_exit", self.cleanup)

    def _get_client(self, http_client: HTTPClient) -> Optional[Client]:
        """Get or create the RedactedClient instance."""
        api_key = self.config["api_key"].get() or os.environ.get("RED_API_KEY")
        if not api_key:
            self._log.warning("redacted: api_key not set in configuration.")
            return None

        user_id = self.config["user_id"].get()
        if not user_id:
            self._log.warning(
                "redacted: user_id not set in configuration. "
                "Will not be able to consider snatched torrents."
            )

        return Client(http_client, self._log, api_key, user_id=user_id)

    def cleanup(self) -> None:
        """Clean up resources when Beets is shutting down."""
        self._http_client.close()
        self._aliases.close()

    def import_stage(self, _: Library, task: ImportTask) -> bool:
        """Process an album during import.

        Adds Redacted metadata to the album.

        Args:
            _: Library instance
            album: Album instance

        Returns:
            True if the album was modified, False otherwise
        """
        if not task.is_album or not task.album or not self._client:
            return False

        album = task.album
        result = search_album(album, self._client, self._log, self._min_score, self._aliases)

        # Record that a lookup happened and what it concluded, even when nothing
        # matched, so albums absent from Redacted are distinguishable from unsearched
        # ones without re-querying the API.
        stamped = False
        if result.outcome is not SearchOutcome.ERROR:
            album.red_checked = time.time()
            album.red_status = result.outcome.value
            album.red_score = result.score
            stamped = True

        # red_mtime records when the Redacted metadata last changed, so it must not be
        # bumped by the bookkeeping stamp above, which changes on every single run.
        fields_changed = False
        if result.fields:
            for key, value in result.fields.__dict__.items():
                if album.get(key) != value:
                    album[key] = value
                    fields_changed = True

        if fields_changed:
            album.red_mtime = time.time()

        return fields_changed or stamped

    def commands(self) -> list[ui.Subcommand]:
        """Get the commands provided by this plugin.

        Returns:
            List of commands.
        """
        return [
            RedactedCommand(self.config, self._log, self._client, self._aliases),
            RedactedMissingCommand(self._log),
        ]
