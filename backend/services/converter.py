"""FFmpeg converter service - format conversion with proper quality profiles.

FIXED: Quality profiles are now complete objects, not mismatched dictionaries.
Source audio quality is preserved through the pipeline.
"""
import asyncio
import logging
import shutil
from pathlib import Path

from ..pipeline.models import QualityProfile, QUALITY_PRESETS

logger = logging.getLogger(__name__)


def _find_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    common = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    for p in common:
        if Path(p).exists():
            return p
    raise FileNotFoundError("ffmpeg not found. Install via winget: winget install ffmpeg")


def _find_ffprobe() -> str:
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    common = [
        r"C:\ffmpeg\bin\ffprobe.exe",
        r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
    ]
    for p in common:
        if Path(p).exists():
            return p
    return ""


async def convert(input_path: str, output_path: str,
                  profile: QualityProfile = None,
                  quality: str = "balanced",
                  progress_callback=None) -> str:
    """Convert audio file to target format/quality.

    If profile is provided, uses that directly.
    If quality string is provided, looks up the preset.
    """
    if profile is None:
        profile = QUALITY_PRESETS.get(quality, QUALITY_PRESETS["balanced"])

    ffmpeg = _find_ffmpeg()

    fmt_args = {
        "mp3": ["-codec:a", "libmp3lame", "-b:a", profile.bitrate],
        "flac": ["-codec:a", "flac"],
        "aac": ["-codec:a", "aac", "-b:a", profile.bitrate],
        "m4a": ["-codec:a", "aac", "-b:a", profile.bitrate],
        "opus": ["-codec:a", "libopus", "-b:a", profile.bitrate],
        "ogg": ["-codec:a", "libvorbis", "-b:a", profile.bitrate],
        "wav": ["-codec:a", "pcm_s16le"],
    }

    args = [
        ffmpeg, "-y", "-i", input_path,
        "-ar", profile.sample_rate,
        "-ac", str(profile.channels),
    ] + fmt_args.get(profile.output_format, fmt_args["mp3"]) + [
        "-loglevel", "warning",
        output_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            logger.error(f"ffmpeg error: {err}")
            raise RuntimeError(f"ffmpeg conversion failed: {err}")

        if progress_callback:
            progress_callback(100)

        return output_path
    except FileNotFoundError:
        raise FileNotFoundError("ffmpeg not found. Install via winget: winget install ffmpeg")


async def get_duration(file_path: str) -> float:
    """Get audio duration in seconds using ffprobe."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return 0.0

    args = [
        ffprobe, "-v", "quiet", "-print_format", "json",
        "-show_format", file_path,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        import json
        data = json.loads(stdout.decode("utf-8", errors="replace"))
        return float(data.get("format", {}).get("duration", 0))
    except Exception:
        return 0.0
