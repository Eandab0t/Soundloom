"""Tests for backend.services.playlists — parsers, writers, matching, enqueue."""
import json
from pathlib import Path

import pytest

from backend.services import playlists as svc


# --------------------------------------------------------------------------
# iTunes XML parsing
# --------------------------------------------------------------------------

def _itunes_xml(tracks, playlist_name="Road Trip"):
    items = []
    for i, (title, artist, album, ms, loc) in enumerate(tracks, 1):
        items.append(f"""
      <key>{i}</key><dict>
        <key>Track ID</key><integer>{i}</integer>
        <key>Name</key><string>{title}</string>
        <key>Artist</key><string>{artist}</string>
        <key>Album</key><string>{album}</string>
        <key>Total Time</key><integer>{ms}</integer>
        <key>Location</key><string>{loc}</string>
      </dict>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Major Version</key><integer>1</integer>
  <key>Tracks</key><dict>{''.join(items)}
  </dict>
  <key>Playlists</key><array>
    <dict>
      <key>Name</key><string>{playlist_name}</string>
      <key>Playlist Items</key><array>
        <dict><key>Track ID</key><integer>1</integer></dict>
      </array>
    </dict>
  </array>
</dict>
</plist>"""


class TestItunesXml:
    def test_parses_tracks_and_playlist_name(self):
        xml = _itunes_xml([
            ("Lovefool", "The Cardigans", "Best Of", 191000, "file://localhost/C:/Music/Lovefool.mp3"),
            ("Boston", "Stella Lefty", "Singles", 244000, "file://localhost/C:/Music/Boston.mp3"),
        ])
        out = svc.parse_itunes_xml(xml)
        assert out["name"] == "Road Trip"
        assert len(out["tracks"]) == 2
        t = out["tracks"][0]
        assert t["title"] == "Lovefool"
        assert t["artist"] == "The Cardigans"
        assert t["album"] == "Best Of"
        assert t["duration"] == pytest.approx(191.0)
        assert t["path"] == "C:/Music/Lovefool.mp3"

    def test_windows_file_url_drive_letter(self):
        xml = _itunes_xml([("X", "A", "", 1000, "file://localhost/C:/Users/me/Music/x.mp3")])
        assert svc.parse_itunes_xml(xml)["tracks"][0]["path"].startswith("C:/Users/me/Music")

    def test_plain_file_url(self):
        xml = _itunes_xml([("X", "A", "", 1000, "file:///home/me/Music/x.flac")])
        assert svc.parse_itunes_xml(xml)["tracks"][0]["path"] == "/home/me/Music/x.flac"

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            svc.parse_itunes_xml("this is not xml <")

    def test_missing_tracks_dict_raises(self):
        with pytest.raises(ValueError):
            svc.parse_itunes_xml('<?xml version="1.0"?><plist version="1.0"><dict></dict></plist>')


# --------------------------------------------------------------------------
# M3U parsing
# --------------------------------------------------------------------------

class TestM3U:
    def test_extended_m3u(self):
        data = "#EXTM3U\n#EXTINF:191,The Cardigans - Lovefool\nC:/Music/Lovefool.mp3\n#EXTINF:244,Alpha - Boston\nC:/Music/Boston.mp3\n"
        out = svc.parse_m3u(data, name_hint="Mix")
        assert out["name"] == "Mix"
        assert out["tracks"][0]["title"] == "Lovefool"
        assert out["tracks"][0]["artist"] == "The Cardigans"
        assert out["tracks"][0]["duration"] == pytest.approx(191.0)
        assert out["tracks"][1]["path"] == "C:/Music/Boston.mp3"

    def test_plain_m3u_uses_filename(self):
        data = "C:/Music/01 - Song.mp3\nC:/Music/02 - Other.mp3\n"
        out = svc.parse_m3u(data)
        assert out["name"] == "M3U Import"
        assert out["tracks"][0]["title"] == "01 - Song"

    def test_playlist_directive_names_it(self):
        data = "#EXTM3U\n#PLAYLIST:My Favorite Songs\n#EXTINF:100,A - T\nC:/a.mp3\n"
        out = svc.parse_m3u(data)
        assert out["name"] == "My Favorite Songs"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            svc.parse_m3u("#EXTM3U\n")

    def test_bom_is_tolerated(self):
        data = "\ufeff#EXTM3U\n#EXTINF:5,A - T\nC:/a.mp3\n"
        assert len(svc.parse_m3u(data)["tracks"]) == 1


# --------------------------------------------------------------------------
# CSV parsing
# --------------------------------------------------------------------------

class TestCSV:
    def test_basic_headers(self):
        data = "title,artist,album,duration,path\nLovefool,The Cardigans,Best Of,191,C:/m/love.mp3\n"
        out = svc.parse_csv(data, name_hint="From Sheet")
        assert out["name"] == "From Sheet"
        t = out["tracks"][0]
        assert t["title"] == "Lovefool" and t["artist"] == "The Cardigans"
        assert t["duration"] == pytest.approx(191.0)

    def test_mmss_duration(self):
        data = "title,artist,duration\nSong,X,3:05\n"
        assert svc.parse_csv(data)["tracks"][0]["duration"] == pytest.approx(185.0)

    def test_flexible_column_names(self):
        data = "Name,Artists\nSong,X\n"
        assert svc.parse_csv(data)["tracks"][0]["title"] == "Song"

    def test_missing_title_column_raises(self):
        with pytest.raises(ValueError):
            svc.parse_csv("artist,album\nX,Y\n")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            svc.parse_csv("title,artist\n")


# --------------------------------------------------------------------------
# Writers round-trip
# --------------------------------------------------------------------------

ROWS = [
    {"title": "Lovefool", "artist": "The Cardigans", "album": "Best Of",
     "duration": 191.0, "path": "C:/Music/Lovefool.mp3"},
    {"title": "Boston", "artist": "Stella Lefty", "album": "Singles",
     "duration": 244.0, "path": ""},
]


class TestWriters:
    def test_m3u_round_trip(self, tmp_path):
        out = svc.write_m3u("My Mix", ROWS, tmp_path)
        assert out.exists() and out.suffix == ".m3u8"
        parsed = svc.parse_m3u(out.read_text(encoding="utf-8"))
        assert [t["title"] for t in parsed["tracks"]] == ["Lovefool", "Boston"]
        assert parsed["tracks"][0]["path"] == "C:/Music/Lovefool.mp3"

    def test_m3u_collision_gets_suffix(self, tmp_path):
        svc.write_m3u("Same", ROWS, tmp_path)
        out2 = svc.write_m3u("Same", ROWS, tmp_path)
        assert out2.name == "Same (2).m3u8"

    def test_csv_round_trip(self, tmp_path):
        out = svc.write_csv("My Mix", ROWS, tmp_path)
        parsed = svc.parse_csv(out.read_text(encoding="utf-8-sig"))
        assert parsed["tracks"][0]["title"] == "Lovefool"
        assert parsed["tracks"][0]["duration"] == pytest.approx(191.0)

    def test_itunes_xml_round_trip(self, tmp_path):
        out = svc.write_itunes_xml("My Mix", ROWS, tmp_path)
        # Re-parse with our own iTunes reader (it is a plist dialect reader).
        parsed = svc.parse_itunes_xml(out.read_text(encoding="utf-8"))
        assert parsed["name"] == "My Mix"
        assert [t["title"] for t in parsed["tracks"]] == ["Lovefool", "Boston"]
        assert parsed["tracks"][0]["path"] == "C:/Music/Lovefool.mp3"

    def test_itunes_xml_escapes_entities(self, tmp_path):
        rows = [{"title": "A & B <c>", "artist": "X", "album": "", "duration": 1.0, "path": ""}]
        out = svc.write_itunes_xml("Esc", rows, tmp_path)
        text = out.read_text(encoding="utf-8")
        assert "A &amp; B &lt;c&gt;" in text

    def test_json_round_trip(self, tmp_path):
        out = svc.write_json("My Mix", ROWS, tmp_path)
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data["name"] == "My Mix"
        assert len(data["tracks"]) == 2

    def test_names_are_sanitized(self, tmp_path):
        out = svc.write_m3u('My: "Bad" <Name>/x?', ROWS, tmp_path)
        assert "/" not in out.name and ":" not in out.name
