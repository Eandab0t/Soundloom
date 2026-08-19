"""Metadata normalization - search/matching index, never canonical data.

canonical_artist = "The Beatles"
normalized_artist = "beatles"
                    ↑ used for matching only
"""
import re
import unicodedata


def normalize_text(text: str) -> str:
    """Lowercase, strip accents, remove non-alphanumeric."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    return text


def normalize_artist(artist: str) -> str:
    """Normalize artist for matching. NEVER store this as canonical."""
    n = normalize_text(artist)
    n = re.sub(r"\s*\(.*?\)\s*", " ", n)
    n = re.sub(r"\s*\[.*?\]\s*", " ", n)
    n = re.sub(r"\bfeat\w*[.,]?\s+\S+", " ", n)
    n = re.sub(r"\bft[.]?\s+\S+", " ", n)
    n = re.sub(r"\bvs[.]?\s+\S+", " ", n)
    n = re.sub(r"\b&\s+\S+", " ", n)
    n = re.sub(r"\bthe\b", " ", n)
    n = re.sub(r"\s*-\s*\w+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_title(title: str) -> str:
    """Normalize title for matching. NEVER store this as canonical."""
    n = normalize_text(title)
    n = re.sub(r"\s*\(.*?(live|remix|acoustic|radio|edit|version).*?\)\s*", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\s*\[.*?(live|remix|acoustic|radio|edit|version).*?\]\s*", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\s*-\s*(live|remix|acoustic|radio edit|extended|instrumental|karaoke|cover).*", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\(official( video| audio)?\)", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\[official( video| audio)?\]", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\(lyric( video)?\)", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\[lyric( video)?\]", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\[.*?\]", " ", n)
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def normalize_album(album: str) -> str:
    """Normalize album for matching."""
    n = normalize_text(album)
    n = re.sub(r"\s*\(.*?(deluxe|expanded|remastered|bonus|edition|remix).*?\)\s*", " ", n, flags=re.IGNORECASE)
    n = re.sub(r"\[.*?\]", " ", n)
    n = re.sub(r"[^\w\s]", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return n


def parse_artists(raw: str) -> dict:
    """Parse artist string into primary + featured.

    Returns:
        {
            "primary": "Kanye West",
            "featured": ["Rihanna"],
            "display": "Kanye West feat. Rihanna",
            "album_artist": "Kanye West",
        }
    """
    if not raw:
        return {"primary": "", "featured": [], "display": "", "album_artist": ""}

    feat_match = re.search(
        r"\s*(?:feat\.?|ft\.?|featuring)\s+(.+?)(?:\s*$)",
        raw, re.IGNORECASE
    )
    primary = raw
    featured = []
    if feat_match:
        primary = raw[:feat_match.start()].strip()
        feat_str = feat_match.group(1).strip()
        featured = [a.strip() for a in re.split(r",\s*|&\s*|\s+and\s+", feat_str) if a.strip()]

    display = raw
    album_artist = primary

    return {
        "primary": primary,
        "featured": featured,
        "display": display,
        "album_artist": album_artist,
    }


def is_variant_match(title_a: str, title_b: str) -> bool:
    """Check if two titles are variants of each other (live, remix, etc.)."""
    variant_keywords = [
        "live", "remix", "remix", "acoustic", "radio edit",
        "extended", "instrumental", "karaoke", "cover", "demo",
        "alternate", "alternate version", "single version",
    ]
    a = normalize_title(title_a)
    b = normalize_title(title_b)
    if a == b:
        return True
    a_has_variant = any(kw in a for kw in variant_keywords)
    b_has_variant = any(kw in b for kw in variant_keywords)
    if a_has_variant != b_has_variant:
        return False
    return a == b
