"""Real-time speed: parsing ffmpeg's progress, the demo verdict, and the live
watch that tells the operator a running template cannot keep up (Phase 3 of the
optimisation advisor)."""

from __future__ import annotations

import asyncio

import pytest

from app.services import ffmpeg_speed as sp
from app.services import ffmpeg_validate
from app.services import stream_manager as sm
from app.services.stream_manager import StreamHandle, StreamManager, _stderr_segments


def line(i: int, media: float, speed: float = 1.0) -> str:
    h, rem = divmod(media, 3600)
    m, s = divmod(rem, 60)
    return (f"frame={i:5d} fps= 25 q=26.0 size=  1024kB time={int(h):02d}:{int(m):02d}:{s:05.2f} "
            f"bitrate= 838.9kbits/s speed={speed:.2f}x    \r")


# --------------------------------------------------------------------------- #
#  parsing
# --------------------------------------------------------------------------- #
def test_parse_progress_reads_time_and_cumulative_speed():
    p = sp.parse_progress(line(250, 10.0, 1.01))
    assert p == {"time": 10.0, "speed": 1.01}
    assert sp.parse_progress(line(1, 3725.5, 0.5))["time"] == 3725.5
    assert sp.parse_progress("Stream mapping:") is None


def test_parse_progress_ignores_the_na_line_before_the_first_frame():
    assert sp.parse_progress("frame=    0 fps=0.0 q=0.0 size=N/A time=N/A bitrate=N/A speed=N/A") is None
    p = sp.parse_progress("frame=    5 fps=0.0 q=0.0 size=N/A time=00:00:00.20 bitrate=N/A speed=N/A")
    assert p == {"time": 0.2, "speed": None}


def test_last_progress_takes_the_final_whole_run_figure():
    err = "Input #0...\n" + line(1, 0.5, 0.3) + line(50, 2.0, 2.4) + "\nvideo:1kB audio:1kB\n"
    assert sp.last_progress(err)["speed"] == 2.4
    assert sp.last_progress("no stats at all") is None


# --------------------------------------------------------------------------- #
#  the watch
# --------------------------------------------------------------------------- #
def drive(speed: float, seconds: float, *, step: float = 0.5, start_media: float = 0.0):
    """Feed a SpeedWatch one stats line per `step` wall seconds at `speed`x."""
    w, events, t, media, i = sp.SpeedWatch(), [], 0.0, start_media, 0
    while t < seconds:
        t += step
        media += step * speed
        i += 1
        ev = w.feed(line(i, media, speed), now=t)
        if ev:
            events.append((t, ev))
    return w, events


def test_a_persistently_slow_encode_is_reported_once_after_the_warmup():
    w, events = drive(0.8, 120)
    assert len(events) == 1
    t, ev = events[0]
    assert t >= sp.WARMUP_S + sp.EVAL_EVERY_S * (sp.SLOW_STREAK - 1)
    assert ev["speed"] == pytest.approx(0.8, abs=0.02)
    assert w.last_speed == pytest.approx(0.8, abs=0.02)


def test_a_healthy_or_fast_encode_is_never_reported():
    assert drive(1.0, 120)[1] == []
    assert drive(3.0, 120)[1] == []


def test_a_short_dip_is_not_a_slow_template():
    """The panel reconnects for a few seconds: not the template's fault."""
    w, t, media, i, events = sp.SpeedWatch(), 0.0, 0.0, 0, []
    while t < 120:
        t += 0.5
        i += 1
        if not 40 <= t < 48:                       # 8 s stall
            media += 0.5
        ev = w.feed(line(i, media), now=t)
        if ev:
            events.append(ev)
    assert events == []


def test_nothing_is_judged_during_warmup_and_a_timestamp_restart_gives_no_verdict():
    assert drive(0.3, sp.WARMUP_S - 1)[1] == []
    w, events, at = sp.SpeedWatch(), [], {}
    for k in range(60):                            # time= jumps back to ~0 at k=30 (reconnect)
        media = 50.0 + k * 0.5 if k < 30 else (k - 30) * 0.5
        ev = w.feed(line(k, media), now=k * 0.5)
        events.extend([ev] if ev else [])
        at[k] = w.window_speed(k * 0.5)
    assert at[25] == pytest.approx(1.0)            # before the jump: a normal verdict
    assert at[34] is None                          # window straddles the jump: no verdict
    assert at[59] == pytest.approx(1.0)            # a whole window later: normal again
    assert events == []


def test_several_stats_lines_in_one_chunk_use_the_latest():
    w = sp.SpeedWatch()
    w.feed(line(1, 1.0) + line(2, 1.5) + line(3, 2.0), now=1.0)
    assert w.samples[-1] == (1.0, 2.0) and len(w.samples) == 1


def test_per_template_stats_expire_and_flag_slowness():
    sp.record("T", 0.6, now=1000.0)
    hit = sp.latest("T", now=1100.0)
    assert hit["slow"] is True and hit["speed"] == 0.6 and hit["age_s"] == 100
    sp.record("T", 1.0, now=2000.0)
    assert sp.latest("T", now=2001.0)["slow"] is False
    assert sp.latest("T", now=2000.0 + sp.STATS_TTL_S + 1) is None
    assert sp.latest("never-seen") is None


# --------------------------------------------------------------------------- #
#  demo verdict
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("speed,mode,level", [
    (0.3, "lavfi", "critical"), (0.7, "lavfi", "warn"), (1.2, "url", "tip"),
    (2.5, "lavfi", "ok"), (0.4, "playlist", "critical"), (0.8, "playlist", "warn"),
    (1.0, "playlist", "ok"),
])
def test_demo_verdict_levels(speed, mode, level):
    v = sp.demo_verdict(speed, mode)
    assert v["level"] == level and f"{speed:.2f}" in v["text"]


def test_playlist_verdict_never_claims_headroom_a_live_source_cannot_show():
    assert "headroom" not in sp.demo_verdict(1.0, "playlist")["text"].replace("spare headroom", "")
    assert "cannot show spare headroom" in sp.demo_verdict(1.0, "playlist")["text"]
    assert sp.demo_verdict(None, "lavfi") is None


def test_demo_result_carries_the_measured_speed_only_for_a_transcode():
    err = line(50, 2.0, 2.4) + "\n"
    tx = ffmpeg_validate._result(ok=True, mode="lavfi", detail="d", args=["ffmpeg", "-c:v", "libx264"], err=err)
    assert tx["speed"] == 2.4 and tx["speed_verdict"]["level"] == "ok"
    cp = ffmpeg_validate._result(ok=True, mode="lavfi", detail="d", args=["ffmpeg", "-c:v", "copy"], err=err)
    assert "speed" not in cp
    bad = ffmpeg_validate._result(ok=False, mode="lavfi", detail="d", args=["ffmpeg", "-c:v", "libx264"], err=err)
    assert "speed" not in bad


# --------------------------------------------------------------------------- #
#  stderr reading
# --------------------------------------------------------------------------- #
class _Err:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n=-1):
        return self._chunks.pop(0) if self._chunks else b""


class _Proc:
    def __init__(self, chunks, rc=0):
        self.stderr = _Err(chunks)
        self.returncode = None
        self.pid = 4321
        self._rc = rc

    async def wait(self):
        self.returncode = self._rc
        return self._rc


async def collect(stream):
    return [item async for item in _stderr_segments(stream)]


async def test_stderr_segments_split_on_cr_and_lf_and_flag_progress():
    data = b"Input #0, mpegts\n" + line(1, 0.5).encode() + line(2, 1.0).encode() + b"boom\r\n" + b"tail"
    segs = await collect(_Err([data[:30], data[30:70], data[70:]]))
    assert [s for s, p in segs if p] == [line(1, 0.5).encode(), line(2, 1.0).encode()]
    plain = [s for s, p in segs if not p]
    assert plain == [b"Input #0, mpegts\n", b"boom\r", b"tail"]      # the lone \n of \r\n is dropped


async def test_the_final_stats_line_stays_in_the_tail_as_evidence():
    segs = await collect(_Err([(line(9, 4.0, 2.0).rstrip("\r") + "\n").encode()]))
    assert segs[0][1] is False


async def test_a_quarter_megabyte_of_progress_without_a_newline_does_not_end_the_reader():
    """asyncio's readline() raised after 64 KiB of `\\r`-only output, the drain loop
    ended, and ffmpeg blocked on a full stderr pipe ~10 minutes into a stream."""
    blob = b"".join(line(i, i * 0.04).encode() for i in range(2700))
    assert len(blob) > 250_000
    chunks = [blob[i:i + 4096] for i in range(0, len(blob), 4096)] + [b"real error\n"]
    segs = await collect(_Err(chunks))
    assert segs[-1] == (b"real error\n", False)
    assert sum(1 for _s, p in segs if p) == 2700


async def test_a_readline_only_stream_survives_an_overlong_line():
    class R:
        def __init__(self):
            self.items = [b"one\n", ValueError("too long"), b"two\n", b""]

        async def readline(self):
            x = self.items.pop(0)
            if isinstance(x, Exception):
                raise x
            return x
    assert [s for s, _p in await collect(R())] == [b"one\n", b"two\n"]


async def test_a_real_subprocess_writing_progress_for_ages_is_fully_drained():
    code = ("import sys\n"
            "for i in range(4000):\n"
            "    sys.stderr.write('frame=%d fps=25 q=26 size=1kB time=00:00:01.00 bitrate=1kbits/s speed=1.0x    \\r' % i)\n"
            "sys.stderr.write('\\nthe real error\\n')\n")
    proc = await asyncio.create_subprocess_exec("python3", "-c", code, stderr=asyncio.subprocess.PIPE)
    await asyncio.wait_for(StreamManager()._drain_stderr(proc), 20)
    assert proc.spm_stderr_tail == [b"the real error\n"]


# --------------------------------------------------------------------------- #
#  the live watch inside StreamManager
# --------------------------------------------------------------------------- #
class _TickWatch(sp.SpeedWatch):
    """SpeedWatch on a fake clock: every stats line is 0.5 s after the last."""
    def feed(self, text, now=None):
        self._t = getattr(self, "_t", 0.0) + 0.5
        return super().feed(text, now=self._t)


def _handle(manager, proc, *, template="Software", command="ffmpeg -i <url> -c:v libx264 -b:v 1200k pipe:1"):
    h = StreamHandle(id="a" * 32, kind="live", item_name="Channel", user_name="u",
                     template_name=template, command=command)
    h.proc = proc
    manager.streams[h.id] = h
    return h


def _stats_chunks(speed, seconds=90.0):
    out, media, i = [], 0.0, 0
    for _ in range(int(seconds / 0.5)):
        media += 0.5 * speed
        i += 1
        out.append(line(i, media, speed).encode())
    return out


@pytest.fixture
def logs(monkeypatch):
    logged = []

    async def fake_log(level, component, message):
        logged.append((level, component, message))

    monkeypatch.setattr(sm, "db_log", fake_log)
    monkeypatch.setattr(sm, "SpeedWatch", _TickWatch)
    return logged


async def test_a_slow_transcode_is_logged_once_with_the_template_and_a_hint(logs):
    manager = StreamManager()
    proc = _Proc(_stats_chunks(0.7))
    h = _handle(manager, proc)
    await manager._drain_stderr(proc)
    slow = [m for lvl, comp, m in logs if lvl == "WARNING" and comp == "ffmpeg"]
    assert len(slow) == 1
    assert "template 'Software'" in slow[0] and "0.70x real time" in slow[0]
    assert "-preset" in slow[0]                                   # libx264 without a preset
    assert "slower than real time" in slow[0]                     # honest about the other cause
    assert sp.latest("Software")["slow"] is True
    assert h.public()["encode_speed"] == pytest.approx(0.7, abs=0.05)


async def test_a_healthy_transcode_is_quiet_but_its_speed_is_remembered(logs):
    manager = StreamManager()
    proc = _Proc(_stats_chunks(1.0))
    _handle(manager, proc)
    await manager._drain_stderr(proc)
    assert not [m for _l, _c, m in logs if "real time" in m]
    assert sp.latest("Software")["slow"] is False


async def test_a_remux_is_never_judged_on_encode_speed(logs):
    manager = StreamManager()
    proc = _Proc(_stats_chunks(0.5))
    _handle(manager, proc, template="Copy", command="ffmpeg -i <url> -c:v copy -c:a copy pipe:1")
    await manager._drain_stderr(proc)
    assert not [m for _l, _c, m in logs if "real time" in m]
    assert sp.latest("Copy") is None


async def test_progress_lines_do_not_bury_the_error_lines_in_the_tail(logs):
    manager = StreamManager()
    proc = _Proc([b"Error while decoding stream #0:0: Invalid data\n", *_stats_chunks(1.0, 60), b"\n"], rc=1)
    _handle(manager, proc)
    await manager._drain_stderr(proc)
    assert any(b"Invalid data" in ln for ln in proc.spm_stderr_tail)
    assert any("rc=1" in m and "Invalid data" in m for _l, _c, m in logs)


def test_the_dashboard_payload_has_a_speed_key_even_without_a_watch():
    h = StreamHandle(id="b" * 32, kind="live", item_name="x", user_name=None,
                     template_name="T", command="c")
    assert h.public()["encode_speed"] is None


async def test_dashboard_lists_the_live_speed_and_the_page_renders_it(logs):
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    manager = StreamManager()
    proc = _Proc(_stats_chunks(0.7))
    _handle(manager, proc)
    await manager._drain_stderr(proc)
    assert manager.list()[0]["encode_speed"] == pytest.approx(0.7, abs=0.05)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        page = await c.get("/")
        if page.status_code in (301, 302, 303):
            page = await c.get(page.headers["location"])
    assert page.status_code == 200 and "real time" in page.text and "encode_speed" in page.text
