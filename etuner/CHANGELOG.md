# Changelog

## v0.2.1 — Polish, safety, and application wrapper

### Bug fixes

- **Backup captured pre-mutation state** (was the inverse): `cmd_fix` and
  `cmd_fetch` snapshotted tags *after* the cleanup/fetch rules had already
  mutated them in memory, so the backup file contained the cleaned values —
  useless for rollback. Both commands now clone the originals immediately
  after scanning, before any rules run.
- **`etuner menu` command wired into the CLI**: the subcommand existed in the
  argument parser but `main()` had no handler for it, so running `etuner menu`
  fell through to printing help and exiting 1. Now launches the interactive
  menu correctly.
- **`_unify_feat` greedy dot replacement**: the old regex
  `re.sub(r"\s*\.\s*", ". ", …)` ran on the *entire* string whenever "feat"
  was present, adding spaces around every period (e.g. "A.B.C. feat. SZA" →
  "A. B. C. feat. SZA"). Replaced with a feat-anchored pattern that only
  normalizes spacing around the `feat.` token itself.
- **`_progress` division-by-zero**: calling `_progress(0, 0)` (empty folder)
  would crash with a ZeroDivisionError. Now guards `total <= 0`.
- **Redundant confirmation condition**: `(n > threshold or n > 0)` simplified
  to always confirm on changes when `--yes` is not set.

### New features

- **`handle_the` config option implemented**: was declared in `Config` but
  never used. When enabled, moves leading articles ("The Beatles" →
  `album_artist_sort = "Beatles, The"`) for correct sort-order in players.
- **`etuner restore <backup_file>`** command: reads a JSON or CSV backup
  created by `fix`/`fetch` and writes the original tag values back to disk.
  Accessible from the interactive menu's Restore option.
- **Interactive menu defaults to dry-run**: first-time users now see a
  preview by default; they must explicitly disable dry-run to write changes.

### Application launcher

- **`run_etuner.py`** — single Python entry point that checks the Python
  version (3.10+), auto-installs `mutagen` and `requests` if missing,
  installs E-Tuner itself in editable mode if needed, then launches the
  interactive menu (or forwards CLI flags to `etuner`).
- **`launch_etuner.bat`** — double-clickable Windows wrapper that finds a
  suitable Python and delegates to `run_etuner.py`.

### Consistency

- Version bumped from 0.1.0 to **0.2.0** everywhere (`__init__.py`,
  `pyproject.toml`).
- MusicBrainz, Discogs, and Spotify fetcher User-Agent strings now use
  `__version__` instead of hardcoded "0.2".
- `local_only` fetcher docstring updated (still said "Phase 1 stub").
- `config.example.json` template added with all options documented.

### Tests

- 3 new tests for `handle_the` (sort-field generation, article boundary,
  disabled-by-default).
- 2 new whitespace tests covering `feat.` with dots-in-titles edge cases.
- All 51 tests pass.

### v0.2.0 — Phase 2 actually implemented

The previous delivery's summary claimed Phase 2 (web metadata lookup) was
"Complete" with four working sources. It wasn't — `etuner/fetch/` only
contained a stub (`LocalOnlyFetcher`) that unconditionally returned "no
match," and there was no `fetch` subcommand wired into the CLI at all. The
Windows `.exe`/installer files referenced in that summary also weren't
actually present, only build scripts that assume PyInstaller/NSIS are run
locally.

This release replaces the stub with real implementations:

- **`etuner/fetch/musicbrainz.py`** — searches the MusicBrainz recording
  endpoint (no API key required), scores matches by MB's own relevance
  score.
- **`etuner/fetch/acoustid.py`** — computes an audio fingerprint via the
  local `fpcalc` (Chromaprint) binary and looks it up against AcoustID;
  the only method that can identify a file from its audio when the
  existing tags are wrong or missing entirely.
- **`etuner/fetch/discogs.py`** — searches Discogs releases via a personal
  access token.
- **`etuner/fetch/spotify.py`** — authenticates via the Client Credentials
  flow and searches tracks, caching the access token across a run.

Also new:

- `etuner fetch <folder>` CLI command with `--method`, `--confidence`, and
  `--overwrite` flags, following the same scan → preview → confirm →
  backup → write flow as `etuner fix`.
- `Config.fetch_method` / `fetch_confidence` / `fetch_overwrite` fields.
- `tests/test_fetch.py` — 15 tests covering all four fetchers with the
  HTTP layer mocked (no network or live API keys needed to run them).
- `requests` added as a real runtime dependency; the old `web` extra
  (`musicbrainzngs`, `pyacoustid`, `discogs-client`, `spotipy` — none of
  which were ever imported by any code) was removed.

Phase 1 (local tag cleanup) is unchanged — it was solid already.

**Still not built:** album art fetching, a GUI, and packaged installers
(`.exe`/`.app`). This ships as a Python CLI (`pip install -e .`).
