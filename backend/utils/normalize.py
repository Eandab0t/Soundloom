"""Artist/title normalization utilities."""
import re


def normalize_artist(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r"^(The|A|An)\s+", "", name.strip(), flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", name).strip().lower()


def normalize_title(title: str) -> str:
    if not title:
        return ""
    t = title.strip().lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def parse_featured(artist_str: str) -> tuple[str, list[str]]:
    if not artist_str:
        return ("", [])
    patterns = [
        r"^(.+?)\s*\((?:feat\.|ft\.|featuring)\s+(.+?)\)\s*$",
        r"^(.+?)\s+(?:feat\.|ft\.|featuring)\s+(.+?)$",
    ]
    for pat in patterns:
        m = re.match(pat, artist_str, re.IGNORECASE)
        if m:
            primary = m.group(1).strip()
            featured = [a.strip() for a in re.split(r",\s*|&\s*|\s+and\s+", m.group(2))]
            return (primary, featured)
    parts = [p.strip() for p in artist_str.split(",")]
    if len(parts) > 1:
        return (parts[0], parts[1:])
    return (artist_str.strip(), [])


def format_display_artist(primary: str, featured: list[str]) -> str:
    if not featured:
        return primary
    return f"{primary} (feat. {', '.join(featured)})"


def fingerprint(artist: str, title: str) -> str:
    a = normalize_artist(artist)
    t = normalize_title(title)
    return f"{a}|||{t}"
