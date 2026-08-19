"""Metadata search — search local library and optionally web sources."""
import logging

logger = logging.getLogger(__name__)


async def search_metadata(query: str, artist: str = None, title: str = None) -> list[dict]:
    """Search the local library for matching tracks.

    Returns a list of result dicts with:
        query, title, artist, album, duration, score, source, file_path
    """
    from ..database import fetch_all

    search_term = (artist or title or query).strip()
    if not search_term:
        return []

    like = f"%{search_term}%"
    rows = await fetch_all(
        """SELECT title, artist, album, album_artist, duration, file_path,
                  primary_artist, display_artist, format, file_size
           FROM tracks
           WHERE (title LIKE ? OR artist LIKE ? OR album LIKE ? OR primary_artist LIKE ?)
           AND file_status != 'missing'
           ORDER BY title ASC LIMIT 20""",
        (like, like, like, like),
    )

    results = []
    for row in rows:
        score = _score_result(search_term, row)
        results.append({
            "query": query,
            "title": row.get("title", ""),
            "artist": row.get("display_artist") or row.get("artist", ""),
            "primary_artist": row.get("primary_artist") or row.get("artist", ""),
            "album": row.get("album", ""),
            "duration": row.get("duration", 0),
            "score": score,
            "source": "library",
            "file_path": row.get("file_path", ""),
            "format": row.get("format", ""),
            "file_size": row.get("file_size", 0),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def _score_result(query: str, row: dict) -> int:
    """Score how well a library track matches the search query."""
    q = query.lower().strip()
    score = 0

    title = (row.get("title") or "").lower()
    artist = (row.get("primary_artist") or row.get("artist") or "").lower()
    album = (row.get("album") or "").lower()

    if q in title:
        score += 40
    if q in artist:
        score += 30
    if q in album:
        score += 20

    words = q.split()
    if len(words) > 1:
        for w in words:
            if w in title:
                score += 10
            if w in artist:
                score += 8

    return min(score, 100)
