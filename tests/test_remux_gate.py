"""
The copy -> MPEG-TS remux gate: direct local files and copy templates.

A remux command is rendered syntactically - it cannot know what is inside
the file. Before this gate, every HEVC or MPEG-2-in-MP4 local file (both
common shapes for digitised films) spawned ffmpeg with
`-bsf:v h264_mp4toannexb`, which aborts ffmpeg with rc=234 before the first
output byte: the box sat on a spinner, the logs said "produced no data".
Audio codecs without an MPEG-TS berth (Vorbis, FLAC, PCM, ... - common in
MKV) failed the same way at the mpegts muxer. And the CLI's 10 s interleave
buffer delayed the first byte by ~10 s on any file whose audio starts late.

The gate probes the source ONCE (cached) and fixes the command per file;
these tests pin the decisions (unit) and the observable behaviour against
the real bundled ffmpeg build (e2e, Skipped without a binary).
"""

from __future__ import annotations

import asyncio
import shutil

import pytest

from app.config import FFMPEG_BIN
from app.services import stream_manager as sm
from app.services.ffmpeg_templates import URL_PLACEHOLDER, mpegts_copy_command
from app.services.probe import _parse_codecs
from app.services.stream_manager import StreamManager

COPY_CMD = (f"ffmpeg -reconnect 1 -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:a:0? "
            "-dn -sn -c:v copy -c:a copy -bsf:v h264_mp4toannexb "
            "-f mpegts -mpegts_flags +resend_headers -flush_packets 1 pipe:1")


def _argv(cmd, url="/media/movie.mp4", pace=True):
    return StreamManager._ffmpeg_argv(cmd, url, pace=pace)


async def _gate(args, codecs, url="/media/movie.mp4", pace=True):
    async def fake_probe(target, *, is_url):
        return codecs
    orig = sm.media_codecs
    sm.media_codecs = fake_probe
    try:
        return await StreamManager()._remux_gate(args, url, pace, "Test")
    finally:
        sm.media_codecs = orig


def _bsf(args):
    i = next((i for i, t in enumerate(args) if t in ("-bsf:v", "-bsf:v:0")), None)
    return args[i + 1] if i is not None else None


def _opt(args, flag):
    i = next((i for i, t in enumerate(args) if t == flag), None)
    return args[i + 1] if i is not None else None


# --------------------------------------------------------------------------- #
#  unit: the probe parser
# --------------------------------------------------------------------------- #
def test_parse_codecs_reads_first_av_streams():
    text = """
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '/media/Bladel 1979.mp4':
  Duration: 01:32:00.00, start: 0.000000, bitrate: 4200 kb/s
  Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), yuv420p10le(tv), 1920x1080, 4000 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, 192 kb/s (default)
"""
    assert _parse_codecs(text) == {"video": "hevc", "audio": "aac"}


def test_parse_codecs_skips_attached_pictures():
    text = """
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 1920x1080, 3000 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
  Stream #0:1[0x2](und): Audio: ac3, 48000 Hz, stereo, fltp, 384 kb/s (default)
  Stream #0:2: Video: mjpeg (Baseline), yuvj420p(pc), 300x300 [SAR 1:1 DAR 1:1], 90k tbr, 90k tbn (attached pic)
"""
    assert _parse_codecs(text)["video"] == "h264"


def test_parse_codecs_ignores_the_output_section_and_empty_input():
    text = """
  Stream #0:0[0x1](und): Video: mpeg2video (Main) (mp4v / 0x7634706D), yuv420p, 720x576, 564 kb/s, 25 fps, 25 tbr, 12800 tbn (default)
Output #0, null, to 'pipe:':
  Stream #0:0(und): Video: wrapped_avframe, yuv420p, 720x576, q=2-31, 200 kb/s, 25 fps
"""
    assert _parse_codecs(text) == {"video": "mpeg2video", "audio": None}
    assert _parse_codecs("no streams here") is None


# --------------------------------------------------------------------------- #
#  unit: interleave flush cap
# --------------------------------------------------------------------------- #
def test_interleave_delta_is_capped_for_mpegts_and_never_duplicated():
    args = _argv(COPY_CMD)
    assert _opt(args, "-max_interleave_delta") == sm.MAX_INTERLEAVE_DELTA_US
    once = _argv(COPY_CMD.replace("pipe:1", "-max_interleave_delta 999 pipe:1"))
    assert once.count("-max_interleave_delta") == 1
    assert _opt(once, "-max_interleave_delta") == "999"      # the user wins


def test_interleave_delta_only_for_ts_and_matroska_pipes():
    mkv = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f matroska -live 1 pipe:1")
    assert "-max_interleave_delta" in mkv
    none = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f wav pipe:1")
    assert "-max_interleave_delta" not in none


# --------------------------------------------------------------------------- #
#  unit: the gate's decisions
# --------------------------------------------------------------------------- #
async def test_gate_keeps_h264_filter_untouched():
    args = _argv(COPY_CMD)
    out = await _gate(args, {"video": "h264", "audio": "aac"})
    assert _bsf(out) == "h264_mp4toannexb"
    assert _opt(out, "-c:a") == "copy"


async def test_gate_swaps_the_filter_for_hevc():
    """The reported bug: HEVC file + h264_mp4toannexb = rc=234, zero bytes."""
    args = _argv(COPY_CMD)
    out = await _gate(args, {"video": "hevc", "audio": "aac"})
    assert _bsf(out) == "hevc_mp4toannexb"


async def test_gate_strips_the_filter_for_mpeg2_and_vc1():
    """DVD-transfer MP4s (MPEG-2) carry start codes natively; the H.264
    filter on them is fatal at init."""
    args = _argv(COPY_CMD)
    out = await _gate(args, {"video": "mpeg2video", "audio": "mp3"})
    assert _bsf(out) is None
    assert _opt(out, "-c:v") == "copy"
    out = await _gate(args, {"video": "vc1", "audio": "ac3"})
    assert _bsf(out) is None


async def test_gate_adds_the_filter_when_a_hand_written_template_lacks_it():
    cmd = (f"ffmpeg -i {URL_PLACEHOLDER} -map 0:v:0 -map 0:a:0? "
           "-c:v copy -c:a copy -f mpegts pipe:1")
    args = _argv(cmd)                       # _ensure_annexb assumes h264 ...
    out = await _gate(args, {"video": "hevc", "audio": "aac"})
    assert _bsf(out) == "hevc_mp4toannexb"  # ... the gate corrects it
    assert _opt(out, "-c:v") == "copy"


async def test_gate_reencodes_ts_unsafe_audio_to_ac3_and_keeps_video_copy():
    args = _argv(COPY_CMD)
    out = await _gate(args, {"video": "h264", "audio": "vorbis"})
    assert _opt(out, "-c:v") == "copy"
    assert _opt(out, "-c:a") == "ac3"
    assert _opt(out, "-b:a") == "384k"
    # already-safe audio is not touched
    for safe in ("aac", "ac3", "eac3", "mp2", "mp3", "dts"):
        out = await _gate(args, {"video": "h264", "audio": safe})
        assert _opt(out, "-c:a") == "copy", safe
        assert "-b:a" not in out


async def test_gate_expands_a_generic_c_copy_for_unsafe_audio():
    cmd = f"ffmpeg -i {URL_PLACEHOLDER} -map 0 -sn -c copy -f mpegts pipe:1"
    args = _argv(cmd)
    out = await _gate(args, {"video": "h264", "audio": "flac"})
    assert _opt(out, "-c:v") == "copy"
    assert _opt(out, "-c:a") == "ac3"
    assert _opt(out, "-c") is None


async def test_gate_steps_aside_when_it_has_no_ground_truth():
    args = _argv(COPY_CMD)
    assert await _gate(args, None) == args                 # probe failed
    mk = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f matroska -live 1 pipe:1")
    assert await _gate(mk, {"video": "vp9", "audio": "opus"}) == mk
    live = _argv(COPY_CMD, "http://cdn/live.ts", pace=False)
    assert await _gate(live, {"video": "hevc", "audio": "aac"},
                     url="http://cdn/live.ts", pace=False) == live
    tr = _argv(f"ffmpeg -i {URL_PLACEHOLDER} -c:v libx264 -c:a aac -f mpegts pipe:1")
    assert await _gate(tr, {"video": "hevc", "audio": "aac"}) == tr


# --------------------------------------------------------------------------- #
#  e2e against the real bundled ffmpeg (the same major version the image ships)
# --------------------------------------------------------------------------- #
HAVE_FFMPEG = shutil.which("ffmpeg") is not None or "/" in FFMPEG_BIN


async def _make(tmp_path, name, *tail):
    out = tmp_path / name
    proc = await asyncio.create_subprocess_exec(
        FFMPEG_BIN, "-hide_banner", "-loglevel", "error", *tail,
        str(out), "-y",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    assert proc.returncode == 0, err.decode(errors="replace")[-400:]
    return out


async def _first_bytes_via_spawn(path):
    """The real pipeline: argv render + remux gate + spawn; first pipe bytes."""
    manager = StreamManager()
    proc = await manager._spawn(mpegts_copy_command(), str(path), "Test", pace=True)
    assert proc is not None
    try:
        data = await asyncio.wait_for(proc.stdout.read(65536), timeout=20)
        return data
    finally:
        await StreamManager._kill_quiet(proc)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg binary not available")
async def test_e2e_remux_of_a_hevc_mp4_now_flows(tmp_path):
    media = await _make(
        tmp_path, "film_hevc.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "4",
        "-c:v", "libx265", "-pix_fmt", "yuv420p", "-tag:v", "hvc1")
    # removed check: the pre-gate command died rc=234 on exactly this shape
    data = await _first_bytes_via_spawn(media)
    assert data and data[:1] == b"\x47"          # MPEG-TS sync byte


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg binary not available")
async def test_e2e_remux_of_an_mp4_with_late_audio_starts_fast(tmp_path):
    """Audio starting 16 s in used to sit ~10 s in the interleave buffer (the
    "no data within N s" killer on slow boxes); the 2 s cap starts it at ~2 s."""
    video = await _make(
        tmp_path, "v.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25", "-t", "30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p")
    media = await _make(
        tmp_path, "film_late_audio.mp4",
        "-i", str(video),
        "-f", "lavfi", "-itsoffset", "16", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "30", "-map", "0:v", "-map", "1:a", "-c", "copy", "-c:a", "aac")
    import time
    start = time.monotonic()
    data = await _first_bytes_via_spawn(media)
    assert data, "no data at all"
    assert time.monotonic() - start < 8, "first byte still takes far too long"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg binary not available")
async def test_e2e_remux_of_a_plain_h264_aac_mp4_still_flows(tmp_path):
    media = await _make(
        tmp_path, "film.mp4",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac")
    data = await _first_bytes_via_spawn(media)
    assert data and data[:1] == b"\x47"


# --------------------------------------------------------------------------- #
#  failure visibility: what ffmpeg said when a local file never starts
# --------------------------------------------------------------------------- #
def test_stderr_tail_helper_squashes_and_limits():
    class Fake:
        spm_stderr_tail = [b"Input #0, mov,mp4, from 'x.mp4':\n",
                           b"[mov] moov atom not found\rprogress junk",
                           b"Error opening input file: Invalid data\n"]
    tail = StreamManager._stderr_tail(Fake())
    assert "moov atom not found" in tail and "\r" not in tail
    assert StreamManager._stderr_tail(object()) == ""


async def test_drain_attaches_tail_and_logs_only_real_errors(monkeypatch):
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, component, message))

    monkeypatch.setattr(sm, "db_log", fake_log)

    class Fake:
        def __init__(self, lines, rc):
            self._q = asyncio.Queue()
            for ln in lines:
                self._q.put_nowait(ln)
            self._q.put_nowait(b"")
            self.stderr = self
            self._rc = rc
            self.returncode = None

        async def readline(self):
            return await self._q.get()

        async def wait(self):
            self.returncode = self._rc
            return self._rc

    manager = StreamManager()
    killed = Fake([b"some complaint\n"], -9)               # we killed it
    await manager._drain_stderr(killed)
    assert killed.spm_stderr_tail == [b"some complaint\n"]
    assert not logged                                      # kills stay quiet

    dead = Fake([b"Invalid data found when processing input\n"], 234)
    await manager._drain_stderr(dead)
    assert any("rc=234" in m and "Invalid data" in m for _, _, m in logged)


async def test_local_pump_logs_ffmpeg_tail_after_a_silent_stall(monkeypatch):
    """A process that never outputs a byte (the user's VAAPI episode) must
    leave its own last words in the stream log when it is stopped."""
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, component, message))

    async def _spawn_stub(self, cmd_template, url, title=None, pace=False):
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "echo boom-error >&2; sleep 30",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        asyncio.get_running_loop().create_task(self._drain_stderr(proc))
        return proc

    monkeypatch.setattr(sm, "db_log", fake_log)
    monkeypatch.setattr(sm, "STREAM_START_TIMEOUT", 0.6)
    monkeypatch.setattr(StreamManager, "_spawn", _spawn_stub)

    h = sm.StreamHandle(id="x" * 32, kind="local", item_name="Test.mp4",
                        user_name="u", template_name="VAAPI", command="(cmd)")
    manager = StreamManager()
    async for _ in manager._pump(h, [("local", "/nonexistent")], "local"):
        pass
    await asyncio.sleep(0.1)                     # let the drain flush its tail
    joined = "\n".join(m for _, _, m in logged)
    assert "no data within 1s from local file" in joined
    assert "still running" in joined
    assert "boom-error" in joined
    assert "stopped after" in joined
