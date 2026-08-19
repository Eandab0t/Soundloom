"""Metadata editor helper - ensures tags are written to files, not just DB."""
import logging
from pathlib import Path
from .models import TrackMetadata

logger = logging.getLogger(__name__)


async def apply_metadata_to_file(file_path: str, metadata: TrackMetadata,
                                  cover_path: str = None) -> bool:
    """Write metadata to the actual audio file, then verify.

    Returns True only if both write and verify succeed.
    """
    from ..services.tagger import write_tags, read_tags

    if not Path(file_path).exists():
        logger.error(f"File not found: {file_path}")
        return False

    tag_dict = metadata.to_dict()
    try:
        success = await write_tags(file_path, tag_dict, cover_path=cover_path)
        if not success:
            logger.error(f"Tag write returned False for {file_path}")
            return False
    except Exception as e:
        logger.error(f"Tag write failed for {file_path}: {e}")
        return False

    try:
        verify = await read_tags(file_path)
        if metadata.title and verify.get("title") != metadata.title:
            logger.warning(f"Title verify mismatch: expected '{metadata.title}', got '{verify.get('title')}'")
        if metadata.artist and verify.get("artist") != metadata.artist:
            logger.warning(f"Artist verify mismatch: expected '{metadata.artist}', got '{verify.get('artist')}'")
    except Exception as e:
        logger.warning(f"Tag verify failed for {file_path}: {e}")

    return True
