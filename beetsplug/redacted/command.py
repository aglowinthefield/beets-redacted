#!/usr/bin/env python3
"""Command implementation for the redacted plugin."""

import logging
import sys
import time
from optparse import OptionParser, Values
from typing import Any, Optional

import enlighten  # type: ignore[import-untyped]
from beets import ui  # type: ignore[import-untyped]
from beets.library import Library  # type: ignore[import-untyped]

from beetsplug.redacted.aliases import AliasProvider
from beetsplug.redacted.client import Client
from beetsplug.redacted.search import SearchOutcome, search_album


def command_func(
    _: "RedactedCommand",
    lib: Library,
    opts: Values,
    args: list[str],
    client: Client,
    log: logging.Logger,
    aliases: Optional[AliasProvider] = None,
) -> dict[str, int]:
    """Execute the redacted command.

    Args:
        lib: Beets library instance
        opts: Command line options
        args: Command line arguments

    Returns:
        Dictionary with counts of modified and unmodified albums
    """
    # Get the Beets database query from args
    query = " ".join(args) if args else ""

    # Without --force, skip albums that already matched and albums already looked up
    # and found absent. red_checked is what makes the second half possible: previously
    # every run re-searched every unmatched album, including ones Redacted simply does
    # not have.
    if not opts.ensure_value("force", False):
        query = f"{query} red_groupid::^$ red_checked::^$"

    # Get the albums to process from the Beets database
    query = query.strip()
    albums = lib.albums(ui.decargs(query))

    n_modified = 0
    n_total = len(albums)
    outcomes: dict[str, int] = {}

    # Set up progress bars with a shared manager. Disable progress bars in tests,
    # as they fail during pytest on Windows. The output terminal in Windows under
    # pytest cause issues, and the progress bars are not needed for testing.
    with enlighten.get_manager(enabled="pytest" not in sys.modules) as manager:
        # Process each album, searching for both torrents and requests
        with manager.counter(
            total=n_total, desc="Processing albums", unit="albums", color="white"
        ) as c_unmodified:
            c_modified = c_unmodified.add_subcounter("blue")

            for album in albums:
                result = search_album(album, client, log, opts.min_score, aliases)
                outcomes[result.outcome.value] = outcomes.get(result.outcome.value, 0) + 1

                # An ERROR leaves the album's status genuinely unknown, so record
                # nothing and let the next run try again.
                if result.outcome is SearchOutcome.ERROR:
                    c_unmodified.update()
                    continue

                with lib.transaction():
                    old = album.copy()

                    # Stamp the outcome even when nothing matched: that is the record
                    # saying "Redacted was asked about this album and did not have it".
                    album.red_checked = time.time()
                    album.red_status = result.outcome.value
                    album.red_score = result.score

                    # Only real Redacted metadata counts as a modification. The stamp
                    # above changes on every run, so folding it in would report every
                    # album as modified and print a diff for each one.
                    modified = False
                    if result.fields is not None:
                        for key, value in result.fields.__dict__.items():
                            if album.get(key) != value:
                                album[key] = value
                                modified = True

                    if not modified:
                        if not opts.pretend:
                            album.store()
                        c_unmodified.update()
                        continue

                    ui.show_model_changes(album, old)

                    if opts.pretend:
                        c_unmodified.update()
                    else:
                        c_modified.update()
                        n_modified += 1
                        album.red_mtime = time.time()
                        album.store()
    if outcomes:
        log.info(
            "redacted: {0}",
            ", ".join(f"{name}: {count}" for name, count in sorted(outcomes.items())),
        )

    return {
        "modified": n_modified,
        "unmodified": n_total - n_modified,
        "total": n_total,
        **outcomes,
    }


class RedactedCommand(ui.Subcommand):
    """Command for searching and updating Redacted information for library albums."""

    def __init__(
        self,
        config: Any,
        log: logging.Logger,
        client: Optional[Client] = None,
        aliases: Optional[AliasProvider] = None,
    ) -> None:
        """Initialize the command.

        Args:
            config: Configuration object from beets
            log: Logger instance for logging messages
            client: RedactedClient instance
        """
        self.config = config
        self.log = log
        self.client = client
        self.aliases = aliases

        default_opts = {
            "min_score": config["min_score"].as_number(),
            "pretend": False,
            "force": False,
        }

        # Create the command parser
        parser = OptionParser(usage="beet redacted [options] [QUERY...]")
        parser.add_option(
            "--min-score",
            dest="min_score",
            type="float",
            default=default_opts["min_score"],
            help="Minimum match score to consider (0-1, default: 0.75)",
        )
        parser.add_option(
            "-p",
            "--pretend",
            action="store_true",
            default=default_opts["pretend"],
            help="Show what would be updated without making changes",
        )
        parser.add_option(
            "-f",
            "--force",
            action="store_true",
            default=default_opts["force"],
            help="Search all albums even if they already have a red_groupid",
        )

        # Initialize the parent class
        super().__init__(
            name="redacted",
            parser=parser,
            help="Search Redacted and update library albums with matching information",
        )

        # Bind the logger and client to the command function
        if self.client:

            def ensure_values(opts: Values, default: dict[str, Any]) -> Values:
                for key, value in default.items():
                    opts.ensure_value(key, value)
                return opts

            def func(lib: Library, opts: Values, args: list[str]) -> dict[str, int]:
                assert self.client is not None

                opts = ensure_values(opts, default_opts)
                return command_func(self, lib, opts, args, self.client, self.log, self.aliases)

            self.func = func
        else:

            def func(lib: Library, opts: Values, args: list[str]) -> dict[str, int]:
                raise ui.UserError(
                    "Could not construct Client for Redacted API requests. Have you set "
                    "the required 'api_key' in the redacted plugin configuration?"
                )

            self.func = func
