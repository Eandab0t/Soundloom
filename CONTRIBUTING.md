# Contributing to Soundloom

Thank you for your interest in contributing to Soundloom! Soundloom is a
local, desktop-first music-library manager that watches artists for new
releases, mirrors Spotify/Deezer playlists, downloads music via per-source
adapters, and keeps metadata and covers clean. Everything is local — your
music stays on your disk, the database is one SQLite file, and the server
binds to 127.0.0.1 only.

## How to Contribute

### 1. Fork and Clone

Fork the repository on GitHub, then clone your fork:

```bash
git clone https://github.com/<your-username>/Soundloom.git
cd Soundloom
```

### 2. Set Up the Development Environment

Soundloom requires Python 3.12+. It is recommended to use a virtual
environment:

```bash
python -m venv .venv
.venv\Scripts\pip install -e .
```

The project also requires `ffmpeg` on your system PATH and `deemix`
(globally installed via npm) for Deezer downloads:

```bash
winget install ffmpeg
npm install -g deemix
```

### 3. Run the Test Suite

Before making changes, verify that the test suite passes:

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

All tests must pass. If a test fails, investigate and fix it before
proceeding.

### 4. Make Your Changes

Create a new branch for your work:

```bash
git checkout -b feature/your-feature-name
```

Make your changes, then run the full test suite again:

```bash
.venv\Scripts\python.exe -m pytest tests -q
```

### 5. Submit a Pull Request

Push your branch to your fork and open a pull request against the
`main` branch of `Eandab0t/Soundloom`. Describe what your change does
and why it is needed.

## Areas Where Help Is Welcome

Soundloom is a solo-maintained project. Contributions are especially
welcome in these areas:

- **Source adapters**: Adding or improving download source adapters
  (YouTube, Deezer, SoundCloud, local filesystem)
- **Database migrations**: Schema changes must be idempotent, tested,
  and include automatic pre-migration backups
- **Frontend**: Vanilla JavaScript UI improvements, accessibility fixes
- **Packaging**: PyInstaller build fixes, Windows installer improvements
- **Documentation**: README improvements, setup guides, troubleshooting
- **Tests**: Regression tests for bug fixes, coverage for new features

## Code Style

Soundloom follows a minimal, boring-code philosophy:

- Prefer standard library over new dependencies
- Prefer simple, obvious code over clever abstractions
- No "future-proofing" — write for today's requirements
- Functions under 15 lines where possible
- No single-letter variables except loop counters
- Dead code is deleted, not commented out

## Reporting Bugs

Use the GitHub issue tracker. Include:

- Soundloom version (shown in the Settings tab or `/api/status`)
- Operating system and version
- Steps to reproduce
- Expected vs. actual behavior
- Relevant log excerpts from `data/logs/app.log`

Do not include your music library, credentials, or API keys in issue
reports.

## Security

See [SECURITY.md](SECURITY.md) for how to report security
vulnerabilities. Do not disclose security issues in public issues or
pull requests.

## License

Soundloom is licensed under the MIT License. By contributing, you agree
that your contributions will be licensed under the MIT License.
