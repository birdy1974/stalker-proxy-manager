"""Host facts for the FFmpeg optimisation advisor.

The advisor can only say "switch on low-power encoding" or "this device node
does not exist" if it knows what this container really has. Everything here is
read-only and cheap:

* ``effective_cpus`` - CPU threads this process may use (affinity + cgroup
  quota, so a container limited to 2 CPUs on a 4-core NAS reports 2);
* ``render_devices`` - the ``/dev/dri/renderD*`` nodes, and whether each is
  readable/writable by us;
* ``ffmpeg -encoders`` / ``-hwaccels`` - which encoders this ffmpeg build has;
* ``vainfo`` - per render node: H.264/HEVC encode and the fixed-function
  low-power entrypoint (``VAEntrypointEncSliceLP``).

The two subprocess probes are cached (they cannot change while the container
runs) and bounded; a probe that fails or times out reports ``None`` = unknown,
and the advisor then simply skips every rule that needs that fact. It never
guesses.
"""

from __future__ import annotations

import asyncio
import glob
import math
import os
import re
import shutil
import time

from ..config import FFMPEG_BIN, VAAPI_DEVICE, VAAPI_DEVICE_CANDIDATES

FFMPEG_TTL_S = 3600.0
VAINFO_TTL_S = 600.0
PROBE_TIMEOUT_S = 6.0

_ffmpeg_cache: tuple[float, dict] | None = None
_vainfo_cache: dict[str, tuple[float, dict]] = {}
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def reset_cache() -> None:
    """Forget every cached probe (tests, and a container that got a new GPU)."""
    global _ffmpeg_cache, _lock
    _ffmpeg_cache = None
    _vainfo_cache.clear()
    _lock = None


# --------------------------------------------------------------------------- #
#  CPU
# --------------------------------------------------------------------------- #
def _cgroup_cpu_limit() -> float | None:
    """CPUs granted by a cgroup quota (docker --cpus), or None = unlimited."""
    try:  # cgroup v2: "max 100000" or "200000 100000"
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max" and int(period) > 0:
            return int(quota) / int(period)
        return None
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def effective_cpus() -> int:
    try:
        n = len(os.sched_getaffinity(0))
    except AttributeError:  # not Linux
        n = os.cpu_count() or 1
    limit = _cgroup_cpu_limit()
    if limit:
        n = min(n, max(1, math.ceil(limit)))
    return max(1, n)


def load_average() -> float | None:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


# --------------------------------------------------------------------------- #
#  render nodes
# --------------------------------------------------------------------------- #
def render_devices(extra: tuple | list = ()) -> list[dict]:
    """Render nodes; `extra` adds paths a template names that are not among the
    usual ones (so a typo in a custom device is reported, not skipped)."""
    paths = set(VAAPI_DEVICE_CANDIDATES) | set(glob.glob("/dev/dri/renderD*"))
    paths |= {p for p in extra if isinstance(p, str) and p.startswith("/dev/")}
    if VAAPI_DEVICE:
        paths.add(VAAPI_DEVICE)
    out = []
    for path in sorted(paths):
        exists = os.path.exists(path)
        out.append({"path": path, "exists": exists,
                    "accessible": exists and os.access(path, os.R_OK | os.W_OK)})
    return out


# --------------------------------------------------------------------------- #
#  subprocess probes
# --------------------------------------------------------------------------- #
async def _run(args: list[str], timeout: float = PROBE_TIMEOUT_S) -> tuple[int, str] | None:
    """The probes' single entry point; tests replace it instead of needing ffmpeg."""
    return await run_subprocess(args, timeout)


async def run_subprocess(args: list[str], timeout: float) -> tuple[int, str] | None:
    """(returncode, stdout+stderr) or None when it could not run / timed out."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except (FileNotFoundError, PermissionError, OSError):
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None
    return proc.returncode or 0, out.decode(errors="replace")


_ENCODER_LINE = re.compile(r"^\s*[VAS][\w.]{5}\s+(\S+)", re.M)


def parse_encoders(text: str) -> list[str]:
    """Encoder names from `ffmpeg -encoders` (the legend lines have no name
    column in this shape, so they never match)."""
    return sorted(set(_ENCODER_LINE.findall(text or "")) - {"=", "------"})


def parse_hwaccels(text: str) -> list[str]:
    names: list[str] = []
    seen_header = False
    for line in (text or "").splitlines():
        line = line.strip()
        if line.lower().startswith("hardware acceleration methods"):
            seen_header = True
        elif seen_header and line:
            names.append(line)
    return names


_VA_LINE = re.compile(r"(VAProfile\w+)\s*:\s*(VAEntrypoint\w+)")


def parse_vainfo(text: str) -> dict:
    """Encode capabilities out of `vainfo` output.

    `*_encode` = some profile of that codec has an encode entrypoint;
    `h264_low_power` = that includes the fixed-function EncSliceLP one.
    All None when the output lists no profile at all (vainfo failed)."""
    entries = _VA_LINE.findall(text or "")
    if not entries:
        return {"h264_encode": None, "h264_low_power": None, "hevc_encode": None}
    h264 = [e for p, e in entries if p.startswith("VAProfileH264")]
    hevc = [e for p, e in entries if p.startswith("VAProfileHEVC")]
    encode = ("VAEntrypointEncSlice", "VAEntrypointEncSliceLP", "VAEntrypointEncPicture")
    return {
        "h264_encode": any(e in encode for e in h264),
        "h264_low_power": "VAEntrypointEncSliceLP" in h264,
        "hevc_encode": any(e in encode for e in hevc),
    }


async def ffmpeg_capabilities() -> dict:
    """{encoders, hwaccels} of the ffmpeg binary, cached; None values = unknown."""
    global _ffmpeg_cache
    now = time.monotonic()
    if _ffmpeg_cache and now - _ffmpeg_cache[0] < FFMPEG_TTL_S:
        return _ffmpeg_cache[1]
    enc = await _run([FFMPEG_BIN, "-hide_banner", "-encoders"])
    hw = await _run([FFMPEG_BIN, "-hide_banner", "-hwaccels"])
    data = {
        "found": enc is not None,
        "encoders": parse_encoders(enc[1]) if enc and enc[0] == 0 else None,
        "hwaccels": parse_hwaccels(hw[1]) if hw and hw[0] == 0 else None,
    }
    if data["found"]:  # a missing binary is retried, not cached for an hour
        _ffmpeg_cache = (now, data)
    return data


async def vaapi_capabilities(device: str) -> dict:
    now = time.monotonic()
    hit = _vainfo_cache.get(device)
    if hit and now - hit[0] < VAINFO_TTL_S:
        return hit[1]
    unknown = {"h264_encode": None, "h264_low_power": None, "hevc_encode": None}
    if not shutil.which("vainfo"):
        return unknown
    res = await _run(["vainfo", "--display", "drm", "--device", device])
    data = parse_vainfo(res[1]) if res and res[0] == 0 else unknown
    _vainfo_cache[device] = (now, data)
    return data


async def snapshot(timeout: float = 5.0, extra_devices: tuple | list = ()) -> dict:
    """Everything the advisor may use, never raising and never blocking longer
    than `timeout` (a slow probe leaves its part unknown)."""
    devices = render_devices(extra_devices)
    caps: dict = {"found": None, "encoders": None, "hwaccels": None}
    vaapi: dict[str, dict] = {}

    async def _probe() -> None:
        nonlocal caps
        async with _get_lock():
            caps = await ffmpeg_capabilities()
            for d in devices:
                if d["accessible"]:
                    vaapi[d["path"]] = await vaapi_capabilities(d["path"])

    try:
        await asyncio.wait_for(_probe(), timeout)
    except asyncio.TimeoutError:
        pass
    except Exception:  # noqa: BLE001 - advice must never break the editor
        pass
    return {
        "cpus": effective_cpus(),
        "load1": load_average(),
        "devices": devices,
        "ffmpeg_found": caps.get("found"),
        "encoders": caps.get("encoders"),
        "hwaccels": caps.get("hwaccels"),
        "vaapi": vaapi,
    }
