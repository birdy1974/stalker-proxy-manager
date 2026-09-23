"""Portal MKV startup must not spend a play token on a subtitle preflight."""

import asyncio
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from app.services import probe
from app.services.ffmpeg_templates import FFmpegOptions, build_command
from app.services.stream_manager import StreamManager


def _command(video_codec="copy", hw_accel="none"):
    return build_command(FFmpegOptions(
        hw_accel=hw_accel, video_codec=video_codec, audio_codec="copy",
        resolution="source", output_format="matroska", subs="keep"))


@pytest.mark.parametrize("video,hardware", [("copy", "none"), ("h264_vaapi", "vaapi")])
async def test_portal_mkv_spawn_opens_the_url_only_for_playback(monkeypatch, video, hardware):
    url = "https://portal.example/movie/42?play_token=single-use"
    monkeypatch.setattr(probe, "_SUBS_CACHE", {})
    calls = []

    async def execute(*args, **kwargs):
        calls.append(args)
        # The pre-fix subtitle probe would spend the token here and cause
        # the second (playback) open to fail on a single-use provider.
        assert len(calls) == 1, "play token already consumed by subtitle probe"
        assert args[args.index("-f") + 1] == "matroska", "unexpected preflight"
        return SimpleNamespace(returncode=None)

    async def drain(self, proc):
        pass

    monkeypatch.setattr(asyncio, "create_subprocess_exec", execute)
    monkeypatch.setattr(StreamManager, "_drain_stderr", drain)
    proc = await StreamManager()._spawn(_command(video, hardware), url, "Movie", pace=True)
    await proc.spm_stderr_task
    assert len(calls) == 1
    assert "0:s?" in calls[0]
    assert calls[0][calls[0].index("-c:s") + 1] == "copy"
    assert "-sn" not in calls[0]


@pytest.mark.parametrize("age,tracks", [
    (0, [{"index": 2, "codec": "subrip", "lang": "dut"}]),
    (0, []), (0, None),
    (probe._SUBS_CACHE_TTL + 1, [{"index": 2, "codec": "subrip"}]),
])
async def test_cache_only_never_probes_even_if_missing_or_expired(monkeypatch, age, tracks):
    base = "https://portal.example/movie/42"
    monkeypatch.setattr(probe, "_SUBS_CACHE", {
        f"{base}|True": (time.time() - age, tracks),
    })

    async def unexpected(*args, **kwargs):
        pytest.fail("cache-only metadata lookup must not open the portal")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", unexpected)
    result = await probe.subtitle_streams(base + "?play_token=new", is_url=True, cached_only=True)
    assert result == (tracks if age == 0 else None)
    assert await probe.subtitle_streams(base + "/missing", is_url=True, cached_only=True) is None


async def test_cached_portal_metadata_still_skips_incompatible_subtitle_codecs(monkeypatch):
    url = "https://portal.example/movie/42?play_token=new"
    monkeypatch.setattr(probe, "_SUBS_CACHE", {
        "https://portal.example/movie/42|True": (time.time(), [
            {"index": 2, "codec": "dvb_teletext", "lang": "dut"},
            {"index": 3, "codec": "subrip", "lang": "eng"},
            {"index": 4, "codec": "ass", "lang": "fre"},
        ]),
    })
    args = StreamManager._ffmpeg_argv(_command(), url, pace=True)
    result = await StreamManager()._subs_gate(args, url, True, "Movie")
    maps = [result[i + 1] for i, token in enumerate(result) if token == "-map"]
    assert maps == ["0:v:0", "0:a:0?", "0:s:1", "0:s:2"]
    assert "-sn" not in result  # English AND French kept; no language filtering


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
async def test_single_use_portal_plays_video_audio_and_dutch_english_subtitles(tmp_path, monkeypatch):
    """Real FFmpeg + HTTP origin refusing a second GET of the same token."""
    subtitle = tmp_path / "sub.srt"
    subtitle.write_text("1\n00:00:00,000 --> 00:00:01,500\nTest subtitle\n")
    media = tmp_path / "source.mkv"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=160x90:r=10",
        "-f", "lavfi", "-i", "sine=frequency=440", "-i", str(subtitle),
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "2:s",
        "-map", "2:s", "-metadata:s:s:0", "language=dut",
        "-metadata:s:s:1", "language=eng", "-metadata:s:s:2", "language=fre",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "copy",
        "-t", "2", "-live", "1", str(media),
    ], check=True, capture_output=True, timeout=20)
    body = media.read_bytes()
    requests = []

    class Origin(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if len(requests) > 1:
                self.send_error(403, "Play token already used")
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/x-matroska")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(probe, "_SUBS_CACHE", {})
    url = f"http://127.0.0.1:{server.server_port}/movie.mkv?play_token=once"
    proc = None
    try:
        # This finite fixture tests startup, not the templates' live-style
        # reconnect-at-EOF policy (which would reopen any short movie at EOF).
        command = _command().replace("-reconnect_at_eof 1", "-reconnect_at_eof 0")
        proc = await StreamManager()._spawn(command, url, "Movie", pace=True)
        output = await asyncio.wait_for(proc.stdout.read(), timeout=20)
        await asyncio.wait_for(proc.spm_stderr_task, timeout=5)
        assert proc.returncode == 0, b"".join(proc.spm_stderr_tail).decode(errors="replace")
        assert output.startswith(b"\x1a\x45\xdf\xa3")  # Matroska EBML header
        assert requests == ["/movie.mkv?play_token=once"]
        saved = tmp_path / "output.mkv"
        saved.write_bytes(output)
        # Decode both A/V tracks, not merely check that the muxer emitted a
        # header; inspect the input banner for all three selectable languages.
        result = subprocess.run([
            "ffmpeg", "-hide_banner", "-i", str(saved), "-map", "0:v:0",
            "-map", "0:a:0", "-f", "null", "-",
        ], check=True, capture_output=True, timeout=10)
        banner = result.stderr.decode(errors="replace").split("\nOutput #", 1)[0]
        assert [m.group(2) for m in probe._RE_STREAM_SUBTITLE.finditer(banner)] == [
            "dut", "eng", "fre"]
    finally:
        await StreamManager._kill_quiet(proc)
        if proc is not None:
            await proc.spm_stderr_task
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
