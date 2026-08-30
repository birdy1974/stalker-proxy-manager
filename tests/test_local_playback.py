"""
Local files in the M3U used to play as silence in VLC.

Direct/copy must serve the original file (Range, real Content-Type, original
extension, real EXTINF duration). A transcode template still goes through
ffmpeg, with the path quoted so spaces survive shlex.split. Missing files
are 404, not an empty 200 / IndexError.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import (
    FFmpegTemplate, LocalFile, LocalPlaylist, LocalSource, User,
)
from app.services.ffmpeg_templates import (
    COPY_PRESET_NAME, REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME,
    URL_PLACEHOLDER, build_command, FFmpegOptions,
)
from app.services.local_files import extinf_duration, play_extension
from app.services.playlist_gen import build_m3u
from app.services.stream_manager import MANAGER, StreamManager

BASE = "http://testserver"


async def _user() -> User:
    async with SessionLocal() as s:
        u = User(name="loc", password="pw", enabled=True, m3u_enabled=True,
                 max_connections=4)
        s.add(u)
        await s.commit()
        return u


async def _seed_file(tmp_path: Path, *, name: str = "The Movie.mp4",
                     template_id: int | None = None,
                     duration_s: float | None = 125.4,
                     contents: bytes | None = None) -> tuple[int, Path]:
    folder = tmp_path / "library"
    folder.mkdir(parents=True, exist_ok=True)
    media = folder / name
    media.write_bytes(contents if contents is not None else b"FAKEMP4" + b"\x00" * 64)
    async with SessionLocal() as s:
        ls = LocalSource(directory=str(folder), enabled=True, recursive=True)
        s.add(ls)
        await s.flush()
        lf = LocalFile(local_source_id=ls.id, relative_path=name, filename=name,
                       size_bytes=media.stat().st_size, duration_s=duration_s)
        s.add(lf)
        await s.flush()
        lp = LocalPlaylist(local_file_id=lf.id, custom_name="Home video",
                           group_name="vod-local", ffmpeg_template_id=template_id)
        s.add(lp)
        await s.commit()
        return lp.id, media


async def test_ffmpeg_argv_quotes_local_paths_with_spaces():
    cmd = (f"ffmpeg -rw_timeout 10000000 -reconnect 1 -i {URL_PLACEHOLDER} "
           "-c copy -f mpegts pipe:1")
    args = StreamManager._ffmpeg_argv(cmd, "/media/The Movie.mkv")
    assert args is not None
    i = args.index("-i")
    assert args[i + 1] == "/media/The Movie.mkv"
    assert "-reconnect" not in args
    assert "-rw_timeout" not in args
    assert StreamManager._ffmpeg_argv("@redirect", "/media/x.mp4") is None


async def test_legacy_dvbsub_templates_are_neutralised_at_spawn():
    """Templates written before the -sn change stored command text with
    `-map 0:s?` + `-c:s dvbsub` (for live DVB subs). On a VOD/local container
    with SRT/ASS/PGS subtitles that mapping aborts ffmpeg at output init with
    zero bytes - the reported "templates do not work for vod". They are dvb
    intent now: argv keeps the mapping and the gate degrades it per source
    (text-only movie -> dropped; live -> kept without a probe)."""
    cmd = (f"ffmpeg -i {URL_PLACEHOLDER} -vf scale=1280:720 -map 0:v:0 "
           "-map 0:a:0? -map 0:s? -dn -c:v libx264 -c:a aac -c:s dvbsub "
           "-f mpegts pipe:1")
    args = StreamManager._ffmpeg_argv(cmd, "/media/movie.mkv")
    assert args is not None
    assert "-c:s" in args and "dvbsub" in args          # intent survives argv
    specs = [args[i + 1] for i, t in enumerate(args) if t == "-map"]
    assert "0:s?" in specs

    async def fake_probe(target, *, is_url):
        return [{"index": 2, "codec": "subrip"}]        # a text-sub movie file

    orig = sm_subs = None
    from app.services import stream_manager as smod
    orig = smod.subtitle_streams
    smod.subtitle_streams = fake_probe
    try:
        out = await StreamManager()._subs_gate(args, "/media/movie.mkv", True, "Test")
    finally:
        smod.subtitle_streams = orig
    specs = [out[i + 1] for i, t in enumerate(out) if t == "-map"]
    assert specs == ["0:v:0", "0:a:0?"], specs
    assert "-c:s" not in out and "dvbsub" not in out
    assert "-sn" in out, "no subtitle stream may reach the mpegts pipe"


async def test_file_inputs_are_paced_with_re_and_live_is_not():
    """A movie file read without -re is drained at encode speed: the player
    hits EOF long before the end. VOD/episode/local are paced; live (paced by
    its own encoder) is never throttled. A user's own -re/-readrate wins."""
    cmd = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"
    local = StreamManager._ffmpeg_argv(cmd, "/media/movie.mkv", pace=True)
    assert local[local.index("-i") - 1] == "-re"
    net = StreamManager._ffmpeg_argv(cmd, "http://cdn/movie.mkv", pace=True)
    assert net[net.index("-i") - 1] == "-re"
    live = StreamManager._ffmpeg_argv(cmd, "http://cdn/live.ts")
    assert "-re" not in live and "-readrate" not in live
    own = StreamManager._ffmpeg_argv(
        f"ffmpeg -readrate 1.5 -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1",
        "/media/movie.mkv", pace=True)
    assert "-re" not in own and own.count("-readrate") == 1


async def test_m3u_uses_real_duration_and_original_extension(tmp_path):
    pid, _ = await _seed_file(tmp_path, duration_s=125.4)
    user = await _user()
    text = await build_m3u(BASE, user)
    assert f"#EXTINF:{extinf_duration(125.4)} " in text
    assert "#EXTINF:-1 tvg-name=\"Home video\"" not in text
    assert f"{BASE}/play/local/{pid}.mp4?u=loc&p=pw" in text
    assert f"/play/local/{pid}.ts?" not in text
    assert play_extension("The Movie.mkv") == ".mkv"


async def test_direct_and_copy_serve_the_original_file(tmp_path):
    await _seed_defaults()
    payload = b"FTPpayload-XXXX"
    async with SessionLocal() as s:
        copy_id = (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == COPY_PRESET_NAME))).scalar_one()
        redir_id = (await s.execute(select(FFmpegTemplate.id).where(
            FFmpegTemplate.name == REDIRECT_PRESET_NAME))).scalar_one()

    pid_redir, media = await _seed_file(tmp_path, name="a.mp4",
                                        template_id=redir_id, contents=payload)
    pid_copy, _ = await _seed_file(tmp_path / "b", name="b.mp4",
                                   template_id=copy_id, contents=payload)
    pid_blank, _ = await _seed_file(tmp_path / "c", name="c.mp4",
                                    template_id=None, contents=payload)
    await _user()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        for pid in (pid_redir, pid_copy, pid_blank):
            r = await c.get(f"/play/local/{pid}.mp4?u=loc&p=pw")
            assert r.status_code == 200, r.text
            assert r.content == payload
            assert "video/mp4" in r.headers.get("content-type", "")
            assert r.headers.get("accept-ranges") == "bytes"
            assert "attachment" not in r.headers.get("content-disposition", "")
        # legacy .ts URL still works
        r = await c.get(f"/play/local/{pid_redir}.ts?u=loc&p=pw")
        assert r.status_code == 200 and r.content == payload
        head = await c.head(f"/play/local/{pid_redir}.mp4?u=loc&p=pw")
        assert head.status_code == 200
        assert head.headers.get("accept-ranges") == "bytes"
        assert int(head.headers["content-length"]) == len(payload)
        rng = await c.get(f"/play/local/{pid_redir}.mp4?u=loc&p=pw",
                          headers={"Range": "bytes=0-3"})
        assert rng.status_code == 206
        assert rng.content == payload[:4]
    await MANAGER.kill_all()


async def test_missing_local_file_is_404_not_empty_200(tmp_path):
    await _seed_defaults()
    pid, media = await _seed_file(tmp_path, name="gone.mp4")
    media.unlink()
    await _user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get(f"/play/local/{pid}.mp4?u=loc&p=pw")
    assert r.status_code == 404, r.text
    assert "not found" in r.text.lower()


async def test_transcode_template_goes_through_ffmpeg_with_quoted_path(
        tmp_path, monkeypatch):
    await _seed_defaults()
    seen: list[str] = []

    async def _spawn_stub(self, cmd_template: str, url: str, title: str | None = None,
                          pace: bool = False):
        seen.append(url)
        return await asyncio.create_subprocess_exec(
            "sh", "-c", "printf MPEGTS",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)

    monkeypatch.setattr(StreamManager, "_spawn", _spawn_stub)

    async with SessionLocal() as s:
        sw = (await s.execute(select(FFmpegTemplate).where(
            FFmpegTemplate.name == REFERENCE_PRESET_NAME))).scalar_one()
        tid = sw.id
    pid, media = await _seed_file(tmp_path, name="The Movie.mkv",
                                  template_id=tid, contents=b"x" * 32)
    await _user()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        r = await c.get(f"/play/local/{pid}.mkv?u=loc&p=pw")
    assert r.status_code == 200, r.text
    assert r.content == b"MPEGTS"
    assert seen and seen[0] == str(media)
    assert "file:" not in seen[0]
    await MANAGER.kill_all()


def test_extinf_duration_helper():
    assert extinf_duration(None) == "-1"
    assert extinf_duration(0) == "-1"
    assert extinf_duration(125.4) == "125"
    assert extinf_duration(1.4) == "1"
