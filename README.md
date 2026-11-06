# Soundloom

<img src="https://repository-images.githubusercontent.com/1403714786/1959a13d-8fa5-4af4-860c-dddcc2f3a832" alt="Soundloom" width="100%">

A local music library that fills itself. Watch artists, sync playlists, download new music, and keep your library tidy — all on your own machine, nothing in the cloud.

I built Soundloom because getting new music onto my iPod was annoyingly manual. I wanted to follow artists and have their new releases just… show up in my library, without hunting them down one by one. Soundloom does that: it watches artists, mirrors playlists, downloads what you don't have yet, and keeps your metadata clean. Everything stays on your computer — no accounts, no cloud, no subscriptions.

## What it does

**Watch artists** — Follow an artist and Soundloom checks for new albums, EPs, and singles, then downloads them automatically.

**Sync playlists** — Point it at a Spotify or Deezer playlist and Soundloom downloads anything you don't already own, keeping it up to date on a schedule.

**Add music** — Paste a YouTube, SoundCloud, or Deezer URL, or point it at a local file. It downloads, converts, tags, and files it away.

**Identify & fix tags** — Look up your tracks against MusicBrainz, preview the changes, and apply them when you're happy.

**Convert audio** — Runs FFmpeg in your browser. Files never leave your machine.

**Play** — There's a persistent player that survives tab switches, so your music keeps going while you browse.

## How it works

Soundloom is a desktop app. You run it locally, it opens in your browser at `http://127.0.0.1:5555`, and everything happens on your machine. Your music library, settings, and database all live in a `data/` folder next to the app — nothing leaves your computer.

### Requirements

- Python 3.12+
- `ffmpeg` on your PATH
- `deemix` (npm, global) for Deezer downloads

### Setup

```bat
python -m venv .venv
.venv\Scripts\pip install -e .
copy data\settings.example.json data\settings.json
run.bat
```

The UI opens at `http://127.0.0.1:5555`. Close the browser and the server shuts itself down after a short timeout.

## What it does *not* do

- It does not stream music. It downloads files to your library.
- It does not host anything online. Your library stays on your disk.
- It does not require an account. There is no login.
- It does not touch your music unless you tell it to. Scanning is read-only; downloads go where you point them.

## License

[MIT](LICENSE) — use it, modify it, share it.

## Updates

Soundloom does not auto-update. New releases go to [GitHub Releases](https://github.com/Eandab0t/Soundloom/releases). Download the new `Soundloom.exe`, replace the old one, and run it — your `data/` folder stays put.

## Support

This is a personal project I maintain in my spare time. There's no support team or guaranteed response time. If something breaks, open a GitHub Issue and I'll look at it when I can.
