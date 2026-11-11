r"""One-time repair: fill the artist-split columns for rows that predate it.

Any track that entered the library before the scanner learned to split
artist credits has primary_artist = '' and fragments the Artists view.
This writes primary/featured/display for those rows only — values already
set by identify or the match pipeline are never touched. Safe to re-run.

    .venv\Scripts\python.exe tools\backfill_artists.py            # real library
    .venv\Scripts\python.exe tools\backfill_artists.py copy.db    # a copy first
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import config                                         # noqa: E402
from backend import database as db                                 # noqa: E402
from backend.services.scanner import backfill_missing_artists      # noqa: E402


async def main() -> None:
    if len(sys.argv) > 1:
        # Dev builds always use <repo>/data, so pointing at a copy needs
        # an explicit path — trial-run against a copy of library.db first.
        config.DB_PATH = Path(sys.argv[1]).resolve()
    await db.init_db()
    try:
        result = await backfill_missing_artists()
        print(f"({config.DB_PATH})")
        print(f"examined {result['examined']} row(s), "
              f"filled {result['fixed']} primary artist(s)")
    finally:
        await db.close_db()


if __name__ == "__main__":
    asyncio.run(main())
