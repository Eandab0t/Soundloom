"""Pipeline interfaces for Soundloom.

Defines the contract between pipeline stages.
yt-dlp, Spotify, MusicBrainz etc. become implementations,
not the application's core.

Pipeline flow:
    Input → Resolver → Metadata → Matcher → Download Plan →
    Downloader → Converter → Tagger → Organizer → Library
"""
