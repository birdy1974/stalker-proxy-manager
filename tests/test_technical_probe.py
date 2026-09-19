"""Admin technical probe and browser-preview routing regressions."""

import asyncio
import json
import os
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import Response
from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    LocalFile,
    LocalPlaylist,
    LocalSource,
    Portal,
    SerieEpisode,
    SeriePlaylist,
    SeriePlaylistSeason,
    SerieSeason,
    SerieSource,
)
from app.services import technical_probe as tp
from app.services.stream_manager import MANAGER

MEDIA = {
    "format": {
        "filename": "http://secret/token",
        "format_name": "mpegts",
        "bit_rate": "2300000",
    },
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30000/1001",
            "pix_fmt": "yuv420p",
        },
        {
            "index": 1,
            "codec_type": "audio",
            "codec_name": "aac",
            "channels": 2,
            "sample_rate": "48000",
            "bit_rate": "128000",
            "tags": {"language": "eng"},
        },
        {"index": 2, "codec_type": "audio", "codec_name": "ac3", "channels": 6},
        {"index": 3, "codec_type": "subtitle", "codec_name": "dvb_subtitle"},
    ],
    "programs": [{"program_id": 1}],
    "chapters": [{"id": 0}],
}


def test_full_summary_tracks_and_missing_fields():
    result = tp.summarize(MEDIA)
    assert result["video"]["fps"] == 29.97
    assert result["video"]["width"] == 1920
    assert result["video"]["kbps"] is None  # never borrow the audio bitrate
    assert result["overall_kbps"] == 2300
    assert result["audio"][0]["kbps"] == 128
    assert result["audio"][1]["channels"] == 6
    assert result["subtitles"][0]["codec"] == "dvb_subtitle"
    assert result["technical"]["programs"] == MEDIA["programs"]
    assert result["technical"]["chapters"] == MEDIA["chapters"]
    assert "filename" not in result["technical"]["format"]
    assert "filename" in MEDIA["format"]  # no mutation of caller data


async def test_probe_uses_identity_and_fresh_results(monkeypatch):
    run = AsyncMock(return_value=(json.dumps(MEDIA).encode(), b"", 0))
    monkeypatch.setattr(tp, "_run", run)
    for _ in range(2):
        result = await tp.probe_technical("http://cdn.test/x.m3u8", is_url=True)
        assert result["method"] == "ffprobe"
    assert run.await_count == 2
    args = run.call_args.args[0]
    assert args[0] == tp.FFPROBE_BIN
    assert "-show_streams" in args and "-show_programs" in args
    assert "-user_agent" in args and "-rw_timeout" in args
    assert "-nostdin" not in args and "-reconnect" in args


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"{}"])
async def test_failed_probe_is_safe_and_retryable(monkeypatch, payload):
    run = AsyncMock(return_value=(payload, b"http://secret/provider-password", 1))
    monkeypatch.setattr(tp, "_run", run)
    result = await tp.probe_technical("/missing/file", is_url=False)
    assert "error" in result and "secret" not in str(result)
    run.return_value = (json.dumps(MEDIA).encode(), b"", 0)
    assert "error" not in await tp.probe_technical("/missing/file", is_url=False)


async def test_missing_binary_falls_back_to_summary(monkeypatch):
    monkeypatch.setattr(tp, "_run", AsyncMock(side_effect=FileNotFoundError))
    legacy = AsyncMock(return_value={"video": {"codec": "h264"}, "audio": []})
    monkeypatch.setattr(tp.probe, "probe_media", legacy)
    result = await tp.probe_technical("/file.mp4", is_url=False)
    assert result["method"] == "ffmpeg summary"
    assert "unavailable" in result["notice"]
    legacy.assert_awaited_once()


@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_kill_and_reap(monkeypatch, cancel):
    class Process:
        returncode = None
        killed = False
        reaped = False

        async def communicate(self):
            await asyncio.sleep(60)

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.reaped = True

    proc = Process()
    monkeypatch.setattr(
        tp.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
    )
    monkeypatch.setattr(tp.probe, "PROBE_TIMEOUT", 0.02)
    if cancel:
        task = asyncio.create_task(tp.probe_technical("/test", is_url=False))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert "timed out" in (await tp.probe_technical("/test", is_url=False))["error"]
    assert proc.killed and proc.reaped


async def test_probe_endpoint_local_id_and_admin_access(monkeypatch):
    async with SessionLocal() as db:
        directory = LocalSource(directory="/media/test")
        db.add(directory)
        await db.flush()
        file = LocalFile(
            local_source_id=directory.id, filename="test.mp4", relative_path="test.mp4"
        )
        db.add(file)
        await db.flush()
        pl = LocalPlaylist(local_file_id=file.id, custom_name="Test")
        db.add(pl)
        await db.commit()
        pid = pl.id
    run = AsyncMock(return_value=tp.summarize(MEDIA))
    monkeypatch.setattr(tp, "probe_technical", run)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/playlist/probe?scope=playlist&kind=local&id={pid}"
        )
        assert response.status_code == 200, response.text
        assert response.json()["stage"] == "source (before FFmpeg)"
        run.assert_awaited_once_with("/media/test/test.mp4", is_url=False)
        assert (
            await client.get("/api/playlist/probe?url=http://arbitrary.test")
        ).status_code == 422
        assert (
            await client.get("/api/playlist/probe?scope=source&kind=bogus&id=1")
        ).status_code == 409
        from app import security

        monkeypatch.setattr(security, "SKIP_LOGIN", False)
        assert (
            await client.get(f"/api/playlist/probe?scope=playlist&kind=local&id={pid}")
        ).status_code in (401, 403)


@pytest.mark.parametrize("failure", [False, True])
async def test_probe_reserves_and_releases_its_mac(monkeypatch, failure):
    from app.services import item_info

    mid = 888888
    MANAGER.release_mac(mid)
    monkeypatch.setattr(
        item_info,
        "resolve_preview_probe",
        AsyncMock(
            return_value={
                "url": "http://provider/stream",
                "is_url": True,
                "mac_id": mid,
                "name": "Preview",
            }
        ),
    )

    async def run(*args, **kwargs):
        assert MANAGER.is_mac_busy(mid)
        assert MANAGER.lease_holder(mid).startswith("technical-probe:")
        if failure:
            raise RuntimeError("test failure")
        return tp.summarize(MEDIA)

    monkeypatch.setattr(tp, "probe_technical", run)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.get("/api/playlist/probe?scope=source&kind=live&id=1")
        assert response.status_code == (500 if failure else 200)
    assert not MANAGER.is_mac_busy(mid)


@pytest.mark.parametrize("kind", ["live", "vod", "episode", "local"])
async def test_admin_playlist_preview_always_proxies(monkeypatch, kind):
    from app.routers import output

    stream = AsyncMock(return_value=Response(b"ts"))
    monkeypatch.setattr(output, "_stream_response", stream)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/preview-play/{kind}/1.ts?mode=redirect")
    assert response.status_code == 200
    assert stream.call_args.args[-1] == "proxy"
    assert stream.call_args.args[0] == kind


async def test_series_preview_and_probe_use_same_episode(monkeypatch):
    from app.routers import output
    from app.services.item_info import resolve_preview_probe

    async with SessionLocal() as db:
        portal = Portal(name="Test", base_url="http://test", enabled=False)
        db.add(portal)
        await db.flush()
        serie = SerieSource(
            portal_id=portal.id, portal_item_id="s", original_name="Series"
        )
        db.add(serie)
        await db.flush()
        seasons = [
            SerieSeason(serie_source_id=serie.id, season_number=n) for n in (2, 1)
        ]
        pl = SeriePlaylist(serie_source_id=serie.id, custom_name="Series", enabled=True)
        db.add_all([*seasons, pl])
        await db.flush()
        episodes = [
            SerieEpisode(
                serie_season_id=season.id,
                episode_number=1,
                cmd=f"http://test/{season.season_number}.ts",
            )
            for season in seasons
        ]
        db.add_all(
            episodes
            + [
                SeriePlaylistSeason(serie_playlist_id=pl.id, serie_season_id=season.id)
                for season in seasons
            ]
        )
        await db.commit()
        resolved = await resolve_preview_probe(db, "playlist", "series", pl.id)
        assert resolved["url"] == "http://test/1.ts"
        pid, eid = pl.id, episodes[1].id
    stream = AsyncMock(return_value=Response(b"ts"))
    monkeypatch.setattr(output, "_stream_response", stream)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(f"/preview-play/series/{pid}.ts")).status_code == 200
    assert stream.call_args.args[:2] == ("episode", eid)


def test_custom_ffmpeg_binary_is_not_an_input_token(monkeypatch):
    from app import config
    from app.services.ffmpeg_templates import argv_validation_errors

    monkeypatch.setattr(config, "FFMPEG_BIN", "/custom/ffmpeg-version-7")
    assert not argv_validation_errors(
        [
            "/custom/ffmpeg-version-7",
            "-i",
            "file.mp4",
            "-c",
            "copy",
            "-f",
            "mpegts",
            "pipe:1",
        ]
    )


@pytest.mark.skipif(
    not os.path.isfile(tp.FFPROBE_BIN), reason="real ffprobe not installed"
)
async def test_real_ffprobe_reads_generated_media(tmp_path):
    from app.config import FFMPEG_BIN

    if not os.path.isfile(FFMPEG_BIN):
        pytest.skip("real ffmpeg not installed")
    path = str(tmp_path / "sample.mp4")
    process = await asyncio.create_subprocess_exec(
        FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=blue:size=640x360:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=sample_rate=48000",
        "-t",
        "1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        path,
    )
    assert await process.wait() == 0
    result = await tp.probe_technical(path, is_url=False)
    assert result["video"]["codec"] == "h264"
    assert result["video"]["width"] == 640 and result["video"]["height"] == 360
    assert result["audio"][0]["codec"] == "aac"
    assert result["audio"][0]["rate_hz"] == 48000
    assert "filename" not in result["technical"]["format"]


async def test_disconnected_probe_is_cancelled_and_releases_mac(monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.routers.api_playlist import preview_probe
    from app.services import item_info

    mid = 888889
    MANAGER.release_mac(mid)
    monkeypatch.setattr(
        item_info,
        "resolve_preview_probe",
        AsyncMock(
            return_value={
                "url": "http://provider/stream",
                "is_url": True,
                "mac_id": mid,
            }
        ),
    )
    stopped = asyncio.Event()

    async def run(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            stopped.set()

    monkeypatch.setattr(tp, "probe_technical", run)
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
    with pytest.raises(HTTPException) as error:
        await preview_probe(request, "source", "live", 1, None)
    assert error.value.status_code == 499
    assert stopped.is_set()
    assert not MANAGER.is_mac_busy(mid)
