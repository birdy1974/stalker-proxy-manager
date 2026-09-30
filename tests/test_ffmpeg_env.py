"""Host facts behind the advisor: parsing, caching, and "unknown is not absent"."""

from __future__ import annotations

import asyncio

from app.services import ffmpeg_env

ENCODERS = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 S..... dvbsub               DVB subtitles (codec dvb_subtitle)
"""
HWACCELS = """Hardware acceleration methods:
vdpau
vaapi
qsv
"""
VAINFO = """vainfo: VA-API version: 1.17 (libva 2.12.0)
vainfo: Driver version: Intel iHD driver for Intel(R) Gen Graphics - 22.3.1
vainfo: Supported profile and entrypoints
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileH264Main               : VAEntrypointEncSlice
      VAProfileH264Main               : VAEntrypointEncSliceLP
      VAProfileH264High               : VAEntrypointEncSlice
      VAProfileHEVCMain               : VAEntrypointVLD
"""
VAINFO_NO_LP = VAINFO.replace("      VAProfileH264Main               : VAEntrypointEncSliceLP\n", "")


def test_parse_encoders_skips_the_legend():
    assert ffmpeg_env.parse_encoders(ENCODERS) == ["aac", "dvbsub", "h264_vaapi", "libx264"]
    assert ffmpeg_env.parse_encoders("") == []


def test_parse_hwaccels():
    assert ffmpeg_env.parse_hwaccels(HWACCELS) == ["vdpau", "vaapi", "qsv"]
    assert ffmpeg_env.parse_hwaccels("nothing here") == []


def test_parse_vainfo_reads_encode_and_low_power():
    caps = ffmpeg_env.parse_vainfo(VAINFO)
    assert caps == {"h264_encode": True, "h264_low_power": True, "hevc_encode": False}
    caps = ffmpeg_env.parse_vainfo(VAINFO_NO_LP)
    assert caps["h264_encode"] is True and caps["h264_low_power"] is False


def test_parse_vainfo_failure_is_unknown_not_false():
    assert ffmpeg_env.parse_vainfo("error: failed to initialize display") == {
        "h264_encode": None, "h264_low_power": None, "hevc_encode": None}


def test_effective_cpus_honours_the_cgroup_quota(monkeypatch):
    monkeypatch.setattr(ffmpeg_env.os, "sched_getaffinity", lambda _pid: set(range(8)))
    monkeypatch.setattr(ffmpeg_env, "_cgroup_cpu_limit", lambda: None)
    assert ffmpeg_env.effective_cpus() == 8
    monkeypatch.setattr(ffmpeg_env, "_cgroup_cpu_limit", lambda: 1.5)
    assert ffmpeg_env.effective_cpus() == 2
    monkeypatch.setattr(ffmpeg_env, "_cgroup_cpu_limit", lambda: 0.2)
    assert ffmpeg_env.effective_cpus() == 1


def test_render_devices_reports_exists_and_access_and_extra_paths(tmp_path, monkeypatch):
    node = tmp_path / "renderD128"
    node.write_text("")
    monkeypatch.setattr(ffmpeg_env, "VAAPI_DEVICE_CANDIDATES", [str(node)])
    monkeypatch.setattr(ffmpeg_env, "VAAPI_DEVICE", str(node))
    monkeypatch.setattr(ffmpeg_env.glob, "glob", lambda _p: [])
    devs = {d["path"]: d for d in ffmpeg_env.render_devices(extra=["/dev/dri/renderD999", "not-a-dev"])}
    assert devs[str(node)] == {"path": str(node), "exists": True, "accessible": True}
    assert devs["/dev/dri/renderD999"]["exists"] is False
    assert "not-a-dev" not in devs            # only /dev paths are probed


async def test_snapshot_probes_once_and_caches(monkeypatch):
    calls = []

    async def fake_run(args, timeout=0):
        calls.append(args[:2])
        if "-encoders" in args:
            return 0, ENCODERS
        if "-hwaccels" in args:
            return 0, HWACCELS
        return 0, VAINFO

    monkeypatch.setattr(ffmpeg_env, "_run", fake_run)
    monkeypatch.setattr(ffmpeg_env.shutil, "which", lambda _n: "/usr/bin/vainfo")
    monkeypatch.setattr(ffmpeg_env, "render_devices", lambda extra=(): [
        {"path": "/dev/dri/renderD128", "exists": True, "accessible": True},
        {"path": "/dev/dri/renderD129", "exists": True, "accessible": False}])
    first = await ffmpeg_env.snapshot()
    assert first["encoders"] == ["aac", "dvbsub", "h264_vaapi", "libx264"]
    assert first["hwaccels"] == ["vdpau", "vaapi", "qsv"]
    assert first["vaapi"]["/dev/dri/renderD128"]["h264_low_power"] is True
    assert "/dev/dri/renderD129" not in first["vaapi"]        # no access: not probed
    n = len(calls)
    await ffmpeg_env.snapshot()
    assert len(calls) == n                                      # all cached
    assert first["cpus"] >= 1


async def test_a_missing_ffmpeg_reports_unknown_and_is_retried(monkeypatch):
    calls = []

    async def gone(args, timeout=0):
        calls.append(1)
        return None

    monkeypatch.setattr(ffmpeg_env, "_run", gone)
    snap = await ffmpeg_env.snapshot()
    assert snap["encoders"] is None and snap["hwaccels"] is None and snap["ffmpeg_found"] is False
    before = len(calls)
    await ffmpeg_env.snapshot()
    assert len(calls) > before                  # not cached for an hour


async def test_a_hanging_probe_never_blocks_the_editor(monkeypatch):
    async def hang(args, timeout=0):
        await asyncio.sleep(30)

    monkeypatch.setattr(ffmpeg_env, "_run", hang)
    started = asyncio.get_running_loop().time()
    snap = await ffmpeg_env.snapshot(timeout=0.2)
    assert asyncio.get_running_loop().time() - started < 3
    assert snap["encoders"] is None and snap["cpus"] >= 1


async def test_real_run_handles_a_missing_binary_and_a_timeout():
    real = ffmpeg_env.run_subprocess
    assert await real(["/nonexistent/binary-xyz"], 5) is None
    assert await real(["sleep", "5"], 0.2) is None
    rc, out = await real(["sh", "-c", "echo hello"], 5)
    assert rc == 0 and "hello" in out
