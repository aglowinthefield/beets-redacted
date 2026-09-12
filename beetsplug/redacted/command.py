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
        # Not self.aliases: ui.Subcommand.__init__ sets that to the command's alternate
        # names, which would silently overwrite the provider and leave an empty tuple
        # here by the time the command runs.
        self._alias_provider = aliases

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
                return command_func(
                    self, lib, opts, args, self.client, self.log, self._alias_provider
                )

            self.func = func
        else:

            def func(lib: Library, opts: Values, args: list[str]) -> dict[str, int]:
                raise ui.UserError(
                    "Could not construct Client for Redacted API requests. Have you set "
                    "the required 'api_key' in the redacted plugin configuration?"
                )

            self.func = func


# Statuses that are positive evidence a release is not on Redacted. ERROR is
# deliberately excluded: a failed lookup says nothing about whether the tracker has it.
ABSENT_STATUSES = (SearchOutcome.NO_ARTIST.value, SearchOutcome.NO_ALBUM_MATCH.value)

DEFAULT_MISSING_FORMAT = "$albumartist - $album ($year) [$red_status]"


def unchecked_query(query: str) -> str:
    """Beets query for albums whose Redacted status is genuinely unknown.

    An album that already has a red_groupid was matched by an earlier version that
    did not record red_checked. It is not an unknown and not an upload candidate, so
    counting it here would badly overstate how much work is left.
    """
    return f"{query} red_groupid::^$ red_checked::^$".strip()


def missing_command_func(
    lib: Library, opts: Values, args: list[str], log: logging.Logger
) -> dict[str, int]:
    """List albums that Redacted was asked about and does not have.

    This reads only the Beets database -- the API work was already done by
    `beet redacted` -- so it is instant and safe to run repeatedly.
    """
    query = " ".join(args) if args else ""

    if opts.ensure_value("unchecked", False):
        albums = list(lib.albums(ui.decargs(unchecked_query(query))))
    else:
        wanted = opts.ensure_value("status", None)
        statuses: list[str] = [wanted] if wanted else list(ABSENT_STATUSES)
        albums = []
        seen: set[int] = set()
        for status in statuses:
            selector = f"{query} red_status:{status}".strip()
            for album in lib.albums(ui.decargs(selector)):
                if album.id is not None and album.id not in seen:
                    seen.add(album.id)
                    albums.append(album)

    fmt = opts.ensure_value("format", None) or DEFAULT_MISSING_FORMAT
    for album in albums:
        ui.print_(format(album, fmt))

    total = len(albums)
    ui.print_("")
    ui.print_(f"{total} album(s).")

    # Unchecked albums are the blind spot: they are not evidence of absence, and
    # silently omitting them would overstate how complete the picture is.
    if not opts.ensure_value("unchecked", False):
        n_unchecked = len(lib.albums(ui.decargs(unchecked_query(query))))
        if n_unchecked:
            ui.print_(
                f"{n_unchecked} album(s) have never been looked up; "
                "run 'beet redacted' first, or list them with 'beet redmissing --unchecked'."
            )

    return {"total": total}


class RedactedMissingCommand(ui.Subcommand):
    """List albums that are not on Redacted, i.e. candidates to upload."""

    def __init__(self, log: logging.Logger) -> None:
        """Initialize the command."""
        self.log = log

        parser = OptionParser(usage="beet redmissing [options] [QUERY...]")
        parser.add_option(
            "-s",
            "--status",
            dest="status",
            default=None,
            help=(
                "Only list albums with this red_status: 'no_artist' (Redacted has no "
                "such artist) or 'no_album_match' (the artist is there, this release "
                "is not). Default: both."
            ),
        )
        parser.add_option(
            "-u",
            "--unchecked",
            action="store_true",
            dest="unchecked",
            default=False,
            help="Instead list albums that have never been looked up on Redacted",
        )
        parser.add_option(
            "-f", "--format", dest="format", default=None, help="Beets format string for each line"
        )

        super().__init__(
            name="redmissing", parser=parser, help="List library albums that are not on Redacted"
        )

        def func(lib: Library, opts: Values, args: list[str]) -> dict[str, int]:
            for key, value in {"status": None, "unchecked": False, "format": None}.items():
                opts.ensure_value(key, value)
            return missing_command_func(lib, opts, args, self.log)

        self.func = func
