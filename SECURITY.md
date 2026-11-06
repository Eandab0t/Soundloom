# Soundloom Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.2   | :white_check_mark: |
| < 0.2.2 | :x: |

## Reporting a Security Vulnerability

Soundloom is a local, desktop-first application. It binds to `127.0.0.1`
only and stores all data under `data/` on the user's machine. It does not
collect telemetry, does not phone home, and does not require user accounts.

If you discover a security vulnerability, please report it privately by
opening a GitHub issue marked as private, or by emailing the repository
owner through GitHub. Do not disclose security issues in public issues
or pull requests.

Please include:

- A description of the vulnerability
- Steps to reproduce
- The affected component (e.g., settings, OAuth, queue, downloader)
- The Soundloom version
- Your operating system

## Security Design

### Local-First Architecture

Soundloom runs entirely on the user's machine:

- The server binds to `127.0.0.1` only (configurable, but defaults to
  localhost-only)
- All data is stored under `data/` (SQLite database, settings, logs,
  covers, exports)
- No data is uploaded to any external server
- No user accounts or authentication system

### Credentials

API credentials (Spotify client ID/secret, SoundCloud client ID/secret)
are stored in `data/settings.json` on the user's machine. They are never
transmitted to Soundloom's developers or any third party. The settings
API returns `<key>_configured` booleans instead of secret values to the
browser.

### OAuth

Soundloom uses OAuth 2.0 with PKCE for Spotify and SoundCloud
connections. The authorization code is exchanged server-to-server via
HTTPS. The code_verifier is kept in-process only and is single-use.
Refresh tokens are stored in the local settings file.

### Third-Party Dependencies

Soundloom depends on several third-party packages:

- fastapi, uvicorn — web server
- aiosqlite — SQLite database access
- mutagen — audio metadata
- yt-dlp — YouTube/SoundCloud audio downloading
- pydantic — data validation
- aiohttp — HTTP client

Users are responsible for keeping these dependencies up to date.
Soundloom does not perform automatic dependency updates.

## What We Do Not Do

- No automatic executable updates (users download new releases manually
  from GitHub Releases)
- No telemetry or analytics
- No user accounts or authentication
- No cloud storage of music or settings
- No data collection of any kind
