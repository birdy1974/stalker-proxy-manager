"""Scan-persisted local metadata removes synchronous first-play probes."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models import LocalFile, LocalPlaylist, LocalSource
from app.services import local_files as local_file_service
from app.services import probe as probe_service
from app.services.probe import media_codecs, subtitle_streams
from app.services.stream_manager import StreamManager


async def _row_for(path, media: dict) -> int:
    stat = path.stat()
    async with SessionLocal() as session:
        source = LocalSource(directory=str(path.parent), enabled=True)
        session.add(source)
        await session.flush()
        local = LocalFile(
            local_source_id=source.id,
            relative_path=path.name,
            filename=path.name,
            size_bytes=stat.st_size,
            mtime=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            duration_s=media.get("duration_s"),
            media_probe=json.dumps(media),
        )
        session.add(local)
        await session.flush()
        item = LocalPlaylist(local_file_id=local.id, custom_name="Cached movie")
        session.add(item)
        await session.commit()
        return item.id


async def test_scan_probe_result_is_persisted_with_duration(tmp_path, monkeypatch):
    path = tmp_path / "new.mp4"
    path.write_bytes(b"media")
    item_id = await _row_for(path, {})
    async with SessionLocal() as session:
        item = await session.get(LocalPlaylist, item_id)
        file_id = item.local_file_id
        local = await session.get(LocalFile, file_id)
        local.media_probe = None
        await session.commit()

    result = {
        "container": "mov,mp4,m4a,3gp,3g2,mj2", "duration_s": 91.5,
        "video": {"codec": "h264"}, "audio": [{"codec": "aac"}],
        "subtitles": [],
    }

    async def fake_probe(target, *, is_url):
        assert target == str(path)
        assert is_url is False
        return result

    monkeypatch.setattr(local_file_service, "probe_media", fake_probe)
    await local_file_service._fill_one(file_id)
    async with SessionLocal() as session:
        local = await session.get(LocalFile, file_id)
        assert json.loads(local.media_probe) == result
        assert local.duration_s == 91.5


async def test_persisted_probe_primes_both_startup_gates(tmp_path, monkeypatch):
    path = tmp_path / "cached.mkv"
    path.write_bytes(b"media")
    item_id = await _row_for(path, {
        "container": "matroska,webm",
        "duration_s": 42.0,
        "video": {"codec": "hevc"},
        "audio": [{"codec": "flac"}],
        "subtitles": [{"index": 2, "codec": "subrip"}],
    })

    async def must_not_probe(*args, **kwargs):
        raise AssertionError("persisted metadata should bypass ffmpeg probing")

    monkeypatch.setattr(probe_service.asyncio, "create_subprocess_exec", must_not_probe)
    assert await StreamManager().local_disk_path(item_id) == (str(path), "Cached movie")
    assert await media_codecs(str(path), is_url=False) == {"video": "hevc", "audio": "flac"}
    assert await subtitle_streams(str(path), is_url=False) == [
        {"index": 2, "codec": "subrip"}
    ]


async def test_changed_file_invalidates_persisted_startup_metadata(tmp_path, monkeypatch):
    path = tmp_path / "changed.mkv"
    path.write_bytes(b"old")
    item_id = await _row_for(path, {
        "video": {"codec": "h264"}, "audio": [{"codec": "aac"}],
        "subtitles": [],
    })
    manager = StreamManager()
    await manager.local_disk_path(item_id)  # first hydrate the process cache
    path.write_bytes(b"replacement with a different size")

    calls = 0

    async def failed_probe(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise OSError("test probe unavailable")

    monkeypatch.setattr(probe_service.asyncio, "create_subprocess_exec", failed_probe)
    await manager.local_disk_path(item_id)
    assert await media_codecs(str(path), is_url=False) is None
    assert calls == 1
