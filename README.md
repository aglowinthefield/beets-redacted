# Redacted Beets Plugin

A [Beets](https://beets.io/) plugin for matching albums to torrents on [Redacted](https://redacted.sh).

> **This is a fork** of [judas-red/beets-redacted](https://github.com/judas-red/beets-redacted),
> which has been unmaintained since May 2025. It is aimed at one job in particular: working
> out which albums in a library are *not* on Redacted, so they can be uploaded. See
> [What this fork changes](#what-this-fork-changes).

## What this fork changes

**Cross-script and alternate artist names.** Redacted HTML-escapes every string it
returns and routinely carries two renderings of one name in a single field --
`Hiroshi Yoshimura (吉村弘)`, `静けさの本: Static for Piano`,
`Quiet Forest (クワイエット・フォレスト)`. Upstream compared those raw against Beets
tags, so anything credited in another script scored near zero. Names are now decoded,
Unicode-normalized and expanded into variants, and artists are resolved through
Redacted's own alias system plus MusicBrainz aliases.

Measured on the 15 Hiroshi Yoshimura albums in a real library, against the live API:
**9 of 15 matched before, 15 of 15 after**, with the group IDs checked by hand against
the artist's discography. All six that had been missing are genuinely on Redacted --
i.e. every one was a duplicate upload waiting to happen.

**Knowing *why* nothing matched.** A missing `red_groupid` used to conflate three
different states: never searched, searched and absent, and lookup failed. Only the
middle one means "worth uploading". Every album now records what happened:

| `red_status` | meaning |
| --- | --- |
| `matched` | found on Redacted |
| `no_album_match` | the artist is on Redacted, this release is not |
| `no_artist` | Redacted has no such artist under any name we know |
| *(unset)* | never looked up, or the lookup failed -- **not** evidence of absence |

Errors deliberately leave the album unstamped, so a network failure is never recorded
as "not on Redacted". `beet redmissing` lists the candidates.

**Artist-first lookup.** Redacted's `artist` endpoint accepts `artistname` and resolves
aliases server-side, so the artist is now looked up directly under each name known for
them instead of being inferred from a free-text search. The old search remains a
fallback.

**Fixes.** Definitive "no artist found" responses (HTTP 404 with a JSON failure body)
are no longer retried eight times with exponential backoff, and cached error responses
are actually reused -- `requests.Response.__bool__` is its `.ok`, so the old truthiness
check silently refetched every one. Two beets 2.x incompatibilities that had the test
suite red are fixed. The HTTP cache moved out of `/tmp`, and the API key may come from
`$RED_API_KEY`.

## Installation

1. Install the redacted plugin via Pip with `pip install beets-redacted`. If you installed Beets via PipX, then you can use `pipx inject beets beets-redacted`.
1. Create a Redacted API key. Go to your user's 'Settings' page, then to the 'Access Settings' section. Under 'API Keys', enter a name for your new API key (perhaps 'beets-redacted'), copy the key in the 'API Key' field for your Beets config, check only the 'Torrents' checkbox, and check the 'Confirm API Key' checkbox. Then, save your profile. The new API Key should then be present with the name you gave it.
1. Update your Beets configuration. Enable the `redacted` plugin, and configure the API Key you generated above:

```yaml
plugins:
  - redacted

redacted:
  api_key: <your new api key>
```

## Configuration

The configuration options are

- **api_key** (required): Your Redacted API key, with at least 'torrents' scope. If `user_id` is also set, the API key must also have the 'User' scope.
- **user_id** (default: empty): Your Redacted user id, used only for looking up your snatches to improve torrent matching. To find your Redacted user id, go to your profile and copy the "id" parameter from the URL bar. It will likely be a number between 1 and 100,000.
- **auto** (default: no): When enabled, search for matching torrents during import.
- **force** (default: no): When enabled, search for matching torrents even for albums that already have a match in the database.
- **pretend** (default: no): When enabled, show the changes that would be made to the database, but do not modify the database.
- **min_score** (experimental, default: 0.75): Minimum match score needed to consider a torrent to be a match to an album. Likely to be changed soon.
- **aliases** (default: yes): Look up artist aliases on MusicBrainz, via each album's `mb_albumartistid`, so a release credited in one script matches a Redacted artist credited in another. Results are cached on disk for a month. Failures are non-fatal: aliases only ever widen matching.
- **alias_cache_dir** (default: `~/.cache/redacted/aliases`): Where to keep the alias cache.

The API key may also be supplied in the `RED_API_KEY` environment variable, which takes effect when `api_key` is unset.

For example,

```yaml
redacted:
  api_key: ...
  user_id: ...
  auto: no
  force: no
  pretend: no
  min_score: 0.75
```

Some of these parameters can also be set on the command line, with `-f` / `--force`, `-p` / `--pretend`, and `-m 0.75` / `--min-score=0.75`.

## Usage

Match torrents for the entire library:

```shell
beet redacted
```

Match torrents for Radiohead albums:

```shell
beet redacted Radiohead
```

Match torrents for Radiohead albums, and overwrite the existing matches:

```shell
beet redacted -f Radiohead
```

List albums that Redacted was asked about and does not have -- i.e. candidates to upload:

```shell
beet redmissing
```

Only the artist, or only the release, is missing:

```shell
beet redmissing --status no_artist        # Redacted has no such artist
beet redmissing --status no_album_match   # artist is there, this release is not
```

Albums that have never been looked up. These are *not* upload candidates; run
`beet redacted` over them first:

```shell
beet redmissing --unchecked
```

Narrow with any Beets query, and choose the output format:

```shell
beet redmissing -f '$albumartist - $album ($year) $format' format:FLAC
```

**Matching is fuzzy and imperfect. Review every album before uploading it -- you are
responsible for your own uploads.** `red_status` records what the tracker answered, not
a guarantee; a release can be on Redacted under a name neither Beets nor MusicBrainz
knows. `red_score` records the confidence of whatever match was found.

List albums that are missing tracks (using the `missing` plugin) and have a matching torrent, which may be candidates for snatching to fill gaps in the library:

```shell
beet missing -a -c -f '$albumartist - $album: Missing $missing; Red: https://redacted.sh/torrents.php?id=$red_groupid&torrentid=$red_torrentid' ^red_torrentid::^$
```

## Roadmap

1. Improved album - torrent matching logic.
1. Configure preferred torrent encoding, e.g. 'Lossless' > '24bit Lossless' > 'V0 (VBR)' > '320'.
1. Command for snatching and exporting `.torrent` files.

Planned matching improvements:

- ~~Prioritize user's snatches~~
- ~~Match across scripts and alternate artist names~~
- If possible, re-use Beets' distance matching algorithm.
- Add fields other than artist name, album name, and year to the matching process. It should also include the album's media, distributor, multiple artists, remastering, format, encoding, if it's personal freeleech, and if it's bookmarked.

## Contributing

Comments and suggestions are welcome. Discussion may happen in [the Redacted forum thread](https://redacted.sh/forums.php?action=viewthread&threadid=72289). Pull requests will be eagerly considered.

### Development environment

The plugin uses [Poetry](https://python-poetry.org/) and [Poe the Poet](https://poethepoet.natn.io/) to manage the package, its dependencies, and to format, lint, and test the code. Please ensure you have both installed and are familiar with their usage.

While Beets supports Python 3.8+, this plugin requires 3.9+.

Set up the development environment:

```shell
# Install dependencies
poetry install

# Install Git pre-commit hooks
poetry run pre-commit install
```

Run Beets using the development plugin:

```shell
poetry run beet redacted
```

Sort imports, format, lint, run tests, and check types:

```shell
poetry run poe sort
poetry run poe format
poetry run poe lint
poetry run poe test
poetry run poe type
```

Sort, format, and run tests - useful for AI agents' self-iteration:

```shell
poetry run poe check
```

Sort imports, format, run tests, lint, and check types - enforced on merging pull requests:

```shell
poetry run poe checkall
```

### Development guidelines

To support AI-assisted development, strong type checking is required and it's imperative to maintain very high code coverage with effective unit tests.
