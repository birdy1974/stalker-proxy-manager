"""
Local-file playback helpers: MIME type, playlist URL suffix, media cache.

Local items used to be advertised as live MPEG-TS (`#EXTINF:-1` + `/play/…ts`)
and then forced through ffmpeg, which is why VLC listed them but played
silence. Direct/copy now serves the original file; scan-time media metadata is
stored for playlist duration and fast first-play remux/subtitle decisions.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path

from sqlalchemy import select

from ..database import SessionLocal
from ..models import LocalFile, LocalPlaylist, LocalSource
from .item_info import local_file_path
from .probe import probe_media

log = logging.getLogger("spm.local")

# Keep in sync with app/routers/api_sources.py VIDEO_EXTS.
VIDEO_EXTS = {
    ".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv",
    ".flv", ".mov", ".webm", ".m4v", ".vob", ".3gp", ".wav", ".mp3",
}

_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".mts": "video/mp2t",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".vob": "video/mpeg", ".3gp": "video/3gpp",
}


def media_type_for(path: str) -> str:
    ext = Path(path).suffix.lower()
    return _MEDIA_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def play_extension(filename: str | None) -> str:
    """Suffix used in the M3U URL so VLC does not treat an MP4 as MPEG-TS."""
    ext = Path(filename or "").suffix.lower()
    if ext in VIDEO_EXTS:
        return ext
    if ext and ext[1:].replace("_", "").isalnum() and 2 <= len(ext) <= 8:
        return ext
    return ".bin"


def extinf_duration(seconds: float | None) -> str:
    """M3U `#EXTINF:` duration. Unknown -> `-1` (live); else rounded seconds."""
    if seconds is None or seconds <= 0:
        return "-1"
    return str(int(round(seconds)))


async def fill_local_durations(file_ids: list[int]) -> None:
    """Best-effort duration/codec/subtitle probe for scanned files. Never raises."""
    for fid in file_ids:
        try:
            await _fill_one(fid)
        except Exception:  # noqa: BLE001 - a bad file must not abort the batch
            log.exception("duration probe failed for local file %s", fid)


async def fill_duration_for_playlist_item(playlist_id: int) -> None:
    """Fill duration_s for the file behind a local playlist row, if missing."""
    try:
        async with SessionLocal() as s:
            lp = await s.get(LocalPlaylist, playlist_id)
            fid = lp.local_file_id if lp else None
        if fid:
            await _fill_one(fid)
    except Exception:  # noqa: BLE001
        log.exception("duration probe failed for local playlist %s", playlist_id)


async def _fill_one(file_id: int) -> None:
    async with SessionLocal() as s:
        lf = await s.get(LocalFile, file_id)
        if lf is None or lf.media_probe:
            return
        ls = await s.get(LocalSource, lf.local_source_id)
        if not ls:
            return
        path = local_file_path(ls.directory, lf.relative_path)
        expected_mtime, expected_size = lf.mtime, lf.size_bytes
    probe = await probe_media(path, is_url=False)
    if not probe or "error" in probe:
        return
    async with SessionLocal() as s:
        lf = await s.get(LocalFile, file_id)
        # Do not attach metadata to a file replaced while ffmpeg was probing it.
        if lf is None or lf.mtime != expected_mtime or lf.size_bytes != expected_size:
            return
        lf.media_probe = json.dumps(probe, separators=(",", ":"))
        if probe.get("duration_s"):
            lf.duration_s = float(probe["duration_s"])
        await s.commit()


async def missing_duration_ids(source_ids: list[int]) -> list[int]:
    if not source_ids:
        return []
    async with SessionLocal() as s:
        rows = (await s.execute(
            select(LocalFile.id).where(
                LocalFile.local_source_id.in_(source_ids),
                LocalFile.media_probe.is_(None)))).scalars().all()
    return list(rows)
