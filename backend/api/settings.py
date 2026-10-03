"""Settings endpoints."""
from fastapi import APIRouter
from .. import config

router = APIRouter(prefix="/api/settings", tags=["settings"])

QUALITY_PRESETS = {
    "best": {"name": "Best Quality", "format": "flac", "bitrate": "lossless", "description": "Lossless audio, maximum quality"},
    "balanced": {"name": "Balanced", "format": "mp3", "bitrate": "320k", "description": "High quality, reasonable file size"},
    "ipod_saver": {"name": "iPod Saver", "format": "m4a", "bitrate": "128k", "description": "Small files, optimized for iPod storage"},
}


@router.get("")
async def get_settings():
    return config.get_all()


@router.put("")
async def update_settings(data: dict):
    return config.update(data)


@router.get("/quality-presets")
async def get_quality_presets():
    return QUALITY_PRESETS
