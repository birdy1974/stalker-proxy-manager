"""Optimisation advisor for FFmpeg templates: quality / speed / load trade-offs.

`advise()` looks at a template's structured options (plus, when available, the
host's real capabilities and the last measured live speed) and returns
*findings*: what could be better, why, and - where there is an unambiguous
change - a concrete fix. It is **advice only**: nothing here rewrites a value
on its own, and nothing blocks saving. The editor shows the findings, lets the
operator tick the ones they want, previews the exact field changes
(`apply_selected`) and only then writes them into the form.

A finding::

    {"id": "gop-long", "severity": "critical|warn|tip", "axis": "start|speed|
     load|quality|compat|host", "fields": ["gop"], "message": "...",
     "why": "...", "fix": {"set": {...}, "extra": [...], "remove": [...],
     "summary": "GOP -> 50"} | None, "ignored": False}

Design rules (enforced by tests):

* every shipped preset produces no advice other than what is documented there;
* a rule that needs a fact it does not have (source fps, GPU capabilities, a
  measurement) says nothing instead of guessing;
* numbers quoted as rules of thumb are labelled as such, never as measurements;
* a finding only carries a `fix` when exactly one sensible change exists.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict

from .ffmpeg_applicability import disabled_parameters
from .ffmpeg_editor import set_extra_option
from .ffmpeg_templates import (FFmpegOptions, VAAPI_ENCODERS, _tokens,
                               target_size, vf_preset_by_id)

SEVERITIES = ("critical", "warn", "tip")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

# --------------------------------------------------------------------------- #
#  goals
# --------------------------------------------------------------------------- #
X264_PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium",
                "slow", "slower", "veryslow", "placebo")

#: Each goal moves thresholds and switches a few rules on or off; it never
#: changes a value by itself.
GOALS: dict[str, dict] = {
    "balanced": {
        "label": "Balanced",
        "hint": "A sensible middle: fast zapping, moderate CPU, no obvious waste.",
        "gop_s": 4.0, "preset": "veryfast", "preset_max": "fast", "preset_sev": "tip",
        "bpp_low_tip": None, "bpp_high": 0.20, "zerolatency": False, "cap_cqp": False,
        "fps_tip": True, "lp_sev": "tip",
    },
    "fast_start": {
        "label": "Fast start",
        "hint": "Shortest time to a picture when zapping: short keyframe gaps, "
                "low-latency encoder settings.",
        "gop_s": 2.5, "preset": "veryfast", "preset_max": "faster", "preset_sev": "warn",
        "bpp_low_tip": None, "bpp_high": 0.20, "zerolatency": True, "cap_cqp": False,
        "fps_tip": True, "lp_sev": "tip",
    },
    "quality": {
        "label": "Best quality",
        "hint": "Picture first: allows slower encoder presets and longer keyframe "
                "gaps, and warns earlier about starved bitrates.",
        "gop_s": 6.0, "preset": "fast", "preset_max": "medium", "preset_sev": "tip",
        "bpp_low_tip": 0.05, "bpp_high": 0.30, "zerolatency": False, "cap_cqp": False,
        "fps_tip": False, "lp_sev": "tip",
    },
    "low_cpu": {
        "label": "Low CPU",
        "hint": "Fewest CPU/GPU cycles per stream, so more streams run side by "
                "side on a small NAS.",
        "gop_s": 4.0, "preset": "superfast", "preset_max": "veryfast", "preset_sev": "warn",
        "bpp_low_tip": None, "bpp_high": 0.12, "zerolatency": False, "cap_cqp": False,
        "fps_tip": True, "lp_sev": "warn",
    },
    "internet": {
        "label": "Internet / weak link",
        "hint": "Streams leave your network: a capped bitrate that cannot spike "
                "past the viewer's connection.",
        "gop_s": 4.0, "preset": "veryfast", "preset_max": "fast", "preset_sev": "tip",
        "bpp_low_tip": 0.04, "bpp_high": 0.12, "zerolatency": False, "cap_cqp": True,
        "fps_tip": True, "lp_sev": "tip",
    },
}
DEFAULT_GOAL = "balanced"


def clean_goal(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in GOALS else DEFAULT_GOAL


def clean_ignored(value) -> str:
    """Normalise the stored dismissed-ids text: unique, known-shaped, capped."""
    if isinstance(value, (list, tuple, set)):
        value = ",".join(str(v) for v in value)
    out: list[str] = []
    for part in re.split(r"[,\s]+", str(value or "")):
        part = part.strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", part) and part not in out:
            out.append(part)
    return ",".join(out[:64])


def goals_public() -> list[dict]:
    return [{"id": k, "label": g["label"], "hint": g["hint"]} for k, g in GOALS.items()]


# --------------------------------------------------------------------------- #
#  small parsers
# --------------------------------------------------------------------------- #
def rate_bps(text) -> float | None:
    """'2500k' / '2.5M' / '800000' -> bits per second."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmMgG]?)\s*", str(text or ""))
    if not m:
        return None
    return float(m[1]) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m[2].lower()]


def fmt_rate(bps: float) -> str:
    """Always whole kbit/s ('2400k'): the form the editor's dropdowns and the
    shipped presets use, so a suggested value reads like the ones beside it."""
    return f"{max(1, int(round(bps / 1000)))}k"


def _num(text) -> float | None:
    try:
        v = float(str(text).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _flag_value(tokens: list[str], flag: str) -> str | None:
    """Value of `flag` in an option list; '' when present without a value."""
    for i, t in enumerate(tokens):
        if t == flag:
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            return "" if nxt.startswith("-") and not re.fullmatch(r"-\d+(?:\.\d+)?", nxt) else nxt
    return None


def _drop_flag(raw: str, flag: str) -> str:
    """`raw` without `flag` and its value, quoting preserved for the rest."""
    from .ffmpeg_templates import _join_tokens
    tokens = _tokens(raw)
    out, i = [], 0
    while i < len(tokens):
        if tokens[i] == flag:
            i += 1
            if i < len(tokens) and (not tokens[i].startswith("-")
                                    or re.fullmatch(r"-\d+(?:\.\d+)?", tokens[i])):
                i += 1
        else:
            out.append(tokens[i])
            i += 1
    return _join_tokens(out)


# H.264 level limits: MaxMBPS (macroblocks/s) and MaxFS (macroblocks/frame).
_LEVEL_LIMITS = [("1.0", 1485, 99), ("1.1", 3000, 396), ("1.2", 6000, 396),
                 ("1.3", 11880, 396), ("2.0", 11880, 396), ("2.1", 19800, 792),
                 ("2.2", 20250, 1620), ("3.0", 40500, 1620), ("3.1", 108000, 3600),
                 ("3.2", 216000, 5120), ("4.0", 245760, 8192), ("4.1", 245760, 8192),
                 ("4.2", 522240, 8704), ("5.0", 589824, 22080), ("5.1", 983040, 36864),
                 ("5.2", 2073600, 36864)]
_LEVEL_BY_NAME = {n: (mbps, fs) for n, mbps, fs in _LEVEL_LIMITS}


def level_needed(width: int, height: int, fps: float) -> str | None:
    """Lowest H.264 level that can carry width x height at fps."""
    mbs = math.ceil(width / 16) * math.ceil(height / 16)
    for name, max_mbps, max_fs in _LEVEL_LIMITS:
        if mbs <= max_fs and mbs * fps <= max_mbps:
            return name
    return None


def _level_key(level) -> str | None:
    v = _num(level)
    if v is None:
        return None
    key = f"{v:.1f}"
    return key if key in _LEVEL_BY_NAME else None


_RES_LADDER = ["360p", "480p", "576p", "720p", "1080p", "1440p", "2160p"]


# --------------------------------------------------------------------------- #
#  context + finding helpers
# --------------------------------------------------------------------------- #
class Ctx:
    """Everything a rule may look at, computed once."""

    def __init__(self, opts: FFmpegOptions, goal: str, env: dict | None, live: dict | None):
        self.o = opts
        self.gid = goal
        self.g = GOALS[goal]
        self.env = env or {}
        self.live = live
        self.codec = opts.video_codec
        self.transcode = opts.video_codec != "copy"
        self.h264 = opts.video_codec in ("libx264", "h264_vaapi", "h264_qsv")
        self.hevc = opts.video_codec in ("libx265", "hevc_vaapi", "hevc_qsv")
        self.sw = opts.video_codec in ("libx264", "libx265")
        self.hw = self.transcode and opts.hw_accel in ("vaapi", "qsv") and not self.sw
        self.inactive = disabled_parameters(opts)
        self.tin = _tokens(opts.extra_input)
        self.tout = _tokens(opts.extra_output)
        self.size = target_size(opts.resolution, opts.aspect) if self.transcode else None
        self.fps = _num(opts.fps) if (opts.fps or "").strip() else None
        self.fps_eff = self.fps or 25.0
        self.fps_assumed = self.fps is None
        self.gop = int(_num(opts.gop)) if _num(opts.gop) is not None else None
        self.rc = (opts.rc_mode or "AUTO").upper()
        vf = vf_preset_by_id(opts.vf_preset) if opts.vf_preset not in ("", "none") else None
        self.vf = vf
        self.bitrate = rate_bps(opts.video_bitrate) if "video_bitrate" not in self.inactive else None
        self.maxrate = rate_bps(opts.maxrate) if "maxrate" not in self.inactive else None
        self.bufsize = rate_bps(opts.bufsize) if "bufsize" not in self.inactive else None
        preset = _flag_value(self.tout, "-preset")
        self.preset = preset if preset else None
        self.effective_preset = self.preset or ("medium" if self.sw else None)

    def devices(self) -> list[dict] | None:
        d = self.env.get("devices")
        return d if isinstance(d, list) else None

    def vaapi_caps(self) -> dict:
        return (self.env.get("vaapi") or {}).get(self.o.device) or {}


def _fix_summary(fix: dict) -> str:
    parts = [f"{k} → {'on' if v is True else 'off' if v is False else (v or 'source/auto')}"
             for k, v in (fix.get("set") or {}).items()]
    parts += [f"add {e['flag']} {e['value']}".strip() for e in fix.get("extra") or []]
    parts += [f"remove {r['flag']}" for r in fix.get("remove") or []]
    return "; ".join(parts)


def _f(fid: str, sev: str, axis: str, fields: list[str], message: str, why: str = "",
       *, set: dict | None = None, extra: list | None = None, remove: list | None = None) -> dict:
    fix = None
    if set or extra or remove:
        fix = {"set": set or {}, "extra": extra or [], "remove": remove or []}
        fix["summary"] = _fix_summary(fix)
    return {"id": fid, "severity": sev, "axis": axis, "fields": fields,
            "message": message, "why": why, "fix": fix, "ignored": False}


def _out(flag: str, value: str) -> dict:
    return {"side": "output", "flag": flag, "value": value}


# --------------------------------------------------------------------------- #
#  rules
# --------------------------------------------------------------------------- #
RULES: list = []


def rule(fn):
    RULES.append(fn)
    return fn


# ---- start / latency -------------------------------------------------------
@rule
def r_hls(c: Ctx):
    if c.o.output_format == "hls":
        return _f("hls-output", "critical", "compat", ["output_format"],
                  "HLS file output cannot be used by the proxy: it streams through a "
                  "pipe and refuses templates that write segment files.",
                  "Even where segments are allowed, HLS adds about two segments "
                  "(~12 s at the default 6 s) before the first picture. MPEG-TS "
                  "starts as soon as the first keyframe arrives.",
                  set={"output_format": "mpegts"})


@rule
def r_gop(c: Ctx):
    if not c.transcode or c.gop is None or c.gop <= 1:
        return None
    secs = c.gop / c.fps_eff
    if secs <= c.g["gop_s"]:
        return None
    target = max(2, int(round(2 * c.fps_eff)))
    assumed = " (assuming 25 fps because FPS is 'src')" if c.fps_assumed else ""
    return _f("gop-long", "warn", "start", ["gop"],
              f"GOP {c.gop} is {secs:.1f} s between keyframes{assumed}; a player that "
              f"joins or zaps in waits for the next one.",
              f"The '{c.g['label']}' goal wants at most {c.g['gop_s']:g} s. Two "
              f"seconds ({target} frames) is the usual live-TV value; longer gaps "
              "only save a few percent bitrate.",
              set={"gop": str(target)})


@rule
def r_gop_intra(c: Ctx):
    if c.transcode and c.gop is not None and c.gop <= 1:
        target = max(2, int(round(2 * c.fps_eff)))
        return _f("gop-intra", "warn", "quality", ["gop"],
                  f"GOP {c.gop} makes every frame a keyframe: the bitrate needed for "
                  "a decent picture is several times higher.",
                  "Intra-only video is for editing, not streaming. Two seconds of "
                  "frames keeps zapping fast without the bitrate explosion.",
                  set={"gop": str(target)})


@rule
def r_gop_default(c: Ctx):
    if c.codec in ("libx264", "libx265") and not (c.o.gop or "").strip():
        secs = 250 / c.fps_eff
        if secs > c.g["gop_s"]:
            target = max(2, int(round(2 * c.fps_eff)))
            return _f("gop-default", "warn", "start", ["gop"],
                      f"No GOP set: {c.codec} then uses 250 frames, about {secs:.0f} s "
                      "between keyframes.",
                      "Zapping waits for a keyframe. Set the GOP to about two seconds "
                      "of frames.",
                      set={"gop": str(target)})


@rule
def r_async_depth(c: Ctx):
    if c.codec not in VAAPI_ENCODERS or "async_depth" in c.inactive:
        return None
    depth = _num(c.o.async_depth)
    if depth is None:
        return None
    if depth > 8:
        return _f("async-depth-high", "tip", "start", ["async_depth"],
                  f"Async depth {int(depth)} keeps {int(depth)} frames in flight: more "
                  "latency and GPU memory for no extra throughput.",
                  "A depth of 2-4 already saturates the encoder.",
                  set={"async_depth": "4"})
    if depth < 2:
        return _f("async-depth-low", "tip", "speed", ["async_depth"],
                  "Async depth 1 lets the GPU idle between frames and lowers throughput.",
                  "2-4 frames in flight is the usual sweet spot.",
                  set={"async_depth": "4"})


@rule
def r_probe(c: Ctx):
    big_ad = _num(_flag_value(c.tin, "-analyzeduration") or "")
    big_ps = _num(_flag_value(c.tin, "-probesize") or "")
    bad = (big_ad is not None and big_ad > 3_000_000) or (big_ps is not None and big_ps > 5_000_000)
    if not bad:
        return None
    extra = [{"side": "input", "flag": "-analyzeduration", "value": "1000000"},
             {"side": "input", "flag": "-probesize", "value": "1000000"}]
    return _f("probe-large", "tip", "start", ["extra_input"],
              "Stream analysis is set high (-analyzeduration / -probesize): ffmpeg reads "
              "that much before it outputs the first byte.",
              "1 s / 1 MB is what the Enigma2 presets use and is enough for broadcast "
              "MPEG-TS; larger values only help unusual sources, at the price of a "
              "slower start.",
              extra=extra)


@rule
def r_bufsize(c: Ctx):
    if not c.transcode or not c.bitrate or not c.bufsize:
        return None
    ratio = c.bufsize / c.bitrate
    if ratio > 4:
        return _f("bufsize-large", "warn", "start", ["bufsize"],
                  f"Buffer size is {ratio:.1f}× the bitrate (~{ratio:.0f} s): the encoder "
                  "may hold back that long before the stream settles.",
                  "About 2× the bitrate (2 s) absorbs a congested link without "
                  "delaying the start.",
                  set={"bufsize": fmt_rate(c.bitrate * 2)})
    if ratio < 1:
        return _f("bufsize-small", "warn", "quality", ["bufsize"],
                  f"Buffer size is only {ratio:.1f}× the bitrate: the encoder cannot "
                  "average out busy scenes and the picture pumps.",
                  "At least 1×, preferably 2×, the bitrate gives rate control room.",
                  set={"bufsize": fmt_rate(c.bitrate * 2)})


@rule
def r_maxrate(c: Ctx):
    if not c.transcode or not c.bitrate:
        return None
    if c.maxrate and c.maxrate < c.bitrate:
        return _f("maxrate-low", "warn", "quality", ["maxrate"],
                  f"Maxrate {c.o.maxrate} is below the target bitrate {c.o.video_bitrate}: "
                  "the two limits contradict each other.",
                  "Maxrate is the ceiling for spikes; it must be at least the average "
                  "bitrate (about +10 % is typical).",
                  set={"maxrate": fmt_rate(c.bitrate * 1.1)})
    if (not c.maxrate and c.gid in ("internet", "balanced", "fast_start")
            and "maxrate" not in c.inactive and not (c.o.maxrate or "").strip()):
        return _f("maxrate-missing", "tip", "quality", ["maxrate", "bufsize"],
                  "No maxrate/bufsize: bitrate spikes are not capped, so a busy "
                  "scene can exceed the viewer's connection.",
                  "A cap of about +10 % with a 2 s buffer keeps the stream smooth.",
                  set={"maxrate": fmt_rate(c.bitrate * 1.1),
                       "bufsize": fmt_rate(c.bitrate * 2)})


# ---- encoder speed / load --------------------------------------------------
@rule
def r_x264_preset(c: Ctx):
    if c.codec != "libx264":
        return None
    goal_preset = c.g["preset"]
    if not c.preset:
        return _f("x264-preset", c.g["preset_sev"], "load", ["extra_output"],
                  "libx264 has no -preset, so it runs 'medium': a lot of CPU for a live "
                  "stream.",
                  f"Rule of thumb: 'medium' → '{goal_preset}' uses roughly 2-3× less CPU "
                  "for about 10 % more bitrate at the same quality, which is the usual "
                  "trade for live TV.",
                  extra=[_out("-preset", goal_preset)])
    if c.preset in X264_PRESETS and X264_PRESETS.index(c.preset) > X264_PRESETS.index(c.g["preset_max"]):
        return _f("x264-preset-slow", "warn", "load", ["extra_output"],
                  f"-preset {c.preset} is slow for live encoding: it needs several times "
                  "the CPU of 'veryfast' and may not keep up in real time.",
                  f"'{goal_preset}' is the recommended live setting for the "
                  f"'{c.g['label']}' goal.",
                  extra=[_out("-preset", goal_preset)])
    if c.preset == "ultrafast" and c.gid != "low_cpu":
        return _f("x264-ultrafast", "tip", "quality", ["extra_output"],
                  "-preset ultrafast wastes bitrate: roughly a third more is needed for "
                  "the same picture as 'veryfast'.",
                  "Unless the CPU is truly starved, superfast/veryfast looks clearly "
                  "better at the same bitrate.",
                  extra=[_out("-preset", "veryfast")])


@rule
def r_x264_tune(c: Ctx):
    if c.codec == "libx264" and c.g["zerolatency"] and _flag_value(c.tout, "-tune") is None:
        return _f("x264-zerolatency", "tip", "start", ["extra_output"],
                  "No -tune zerolatency: x264 buffers frames ahead (look-ahead, B-frames) "
                  "before it outputs anything.",
                  "zerolatency removes that delay so the first picture appears sooner, "
                  "at a cost of roughly 10 % bitrate efficiency.",
                  extra=[_out("-tune", "zerolatency")])


@rule
def r_hevc(c: Ctx):
    if c.codec == "libx265":
        return _f("hevc-software", "warn", "load", ["video_codec"],
                  "Software HEVC (libx265) needs roughly 3-5× the CPU of libx264 and "
                  "many set-top boxes cannot decode HEVC at all.",
                  "For live transcoding H.264 is the practical choice; the GPU "
                  "encodes it almost for free.",
                  set={"video_codec": "libx264"})
    if c.codec in ("hevc_vaapi", "hevc_qsv"):
        target = "h264_vaapi" if c.codec == "hevc_vaapi" else "h264_qsv"
        return _f("hevc-hardware", "tip", "compat", ["video_codec"],
                  "HEVC hardware encoding has no low-power path on Intel Apollo Lake and "
                  "most set-top boxes cannot decode it.",
                  "H.264 is the sweet spot for live transcoding: faster to encode, "
                  "decodes everywhere.",
                  set={"video_codec": target})


@rule
def r_low_power(c: Ctx):
    if c.codec != "h264_vaapi":
        return None
    caps = c.vaapi_caps()
    if not c.o.low_power and caps.get("h264_low_power") is True:
        return _f("low-power-off", c.g["lp_sev"], "load", ["low_power"],
                  "This GPU has the fixed-function low-power H.264 encoder, but low-power "
                  "is off.",
                  "The low-power encoder is faster and draws less power, and it leaves "
                  "the GPU's shader units free for more parallel streams.",
                  set={"low_power": True})
    if c.o.low_power and caps.get("h264_low_power") is False and caps.get("h264_encode"):
        return _f("low-power-unsupported", "critical", "compat", ["low_power"],
                  "Low-power is on, but this GPU's driver lists no low-power H.264 "
                  "entrypoint: ffmpeg will fail to open the encoder.",
                  "vainfo shows VAEntrypointEncSlice but not EncSliceLP for this "
                  "device.",
                  set={"low_power": False})


@rule
def r_threads(c: Ctx):
    threads = _num(_flag_value(c.tout, "-threads") or "")
    if threads is None or not c.transcode:
        return None
    cpus = c.env.get("cpus")
    if c.hw:
        return _f("threads-hardware", "tip", "load", ["extra_output"],
                  "-threads has no effect on a GPU encoder; it only adds CPU threads for "
                  "the small CPU parts of the pipeline.",
                  "Leave it at automatic.", remove=[{"side": "output", "flag": "-threads"}])
    if cpus and threads > cpus:
        return _f("threads-too-many", "tip", "load", ["extra_output"],
                  f"-threads {int(threads)} is more than the {cpus} CPU thread(s) this "
                  "container can use.",
                  "Extra threads only add scheduling overhead; automatic (0) picks a "
                  "sensible number.",
                  remove=[{"side": "output", "flag": "-threads"}])


@rule
def r_fps(c: Ctx):
    if not c.transcode or not c.g["fps_tip"] or c.fps is None or c.fps < 50:
        return None
    if c.vf is not None and c.vf.doubles:
        return None      # a bob deinterlacer deliberately produces 50p
    return _f("fps-high", "tip", "load", ["fps"],
              f"{c.fps:g} fps doubles the encode work and the bitrate needed, compared "
              "with 25.",
              "Most TV channels are 25 fps (or 25i). Keep 50 only for sport and other "
              "fast motion, where it is visibly smoother.",
              set={"fps": "25"})


@rule
def r_same_as_source(c: Ctx):
    o = c.o
    if (c.transcode and o.resolution == "source" and not (o.fps or "").strip()
            and c.vf is None):
        return _f("transcode-as-source", "tip", "load", ["video_codec"],
                  "Re-encoding at the source's own size and frame rate changes nothing "
                  "visible but the codec and bitrate.",
                  "If the player can handle the source codec, Copy uses almost no CPU "
                  "and keeps the original quality. Keep the transcode only if you need "
                  "a bitrate cap or H.264 output.",
                  set={"video_codec": "copy"})


@rule
def r_host_busy(c: Ctx):
    if not (c.sw and c.transcode):
        return None
    cpus, active, load = c.env.get("cpus"), c.env.get("active_transcodes"), c.env.get("load1")
    if cpus and active is not None and active + 1 > cpus:
        return _f("host-busy", "warn", "host", ["video_codec"],
                  f"{active} transcode(s) already run on {cpus} CPU thread(s); another "
                  "software encode will compete with them and every stream may stutter.",
                  "Move the encode to the GPU (VAAPI/QSV), choose a faster preset or "
                  "lower resolution, or use Copy where the box can play the source.")
    if cpus and load is not None and load > cpus * 0.9:
        return _f("host-busy", "warn", "host", ["video_codec"],
                  f"The host is already busy (load {load:.1f} on {cpus} CPU thread(s)); a "
                  "software encode needs spare CPU to keep up in real time.",
                  "Move the encode to the GPU (VAAPI/QSV) or use a lower resolution.")


@rule
def r_live_slow(c: Ctx):
    live = c.live
    if not (live and live.get("slow") and c.transcode):
        return None
    speed = live["speed"]
    ago = _ago(live.get("age_s"))
    why = ("A live transcode must run at 1.0× or faster. Below that the player's buffer "
           "drains and the picture stutters. It can also mean the source itself "
           "delivers slower than real time; the stream log tells which.")
    fixes: dict = {}
    if c.codec == "libx264" and (c.effective_preset not in X264_PRESETS[:2]):
        fixes = {"extra": [_out("-preset", "superfast")]}
    else:
        step = _lower_resolution(c.o.resolution)
        if step:
            fixes = {"set": {"resolution": step}}
    return _f("live-slow", "critical", "speed", ["resolution", "video_codec"],
              f"Measured on a real stream{ago}: encoding ran at {speed:.2f}× real time. "
              "This template cannot keep up.", why, **fixes)


def _ago(age_s) -> str:
    if age_s is None:
        return ""
    if age_s < 120:
        return " just now"
    if age_s < 7200:
        return f" {age_s // 60} min ago"
    return f" {age_s // 3600} h ago"


def _lower_resolution(res: str) -> str | None:
    if res in _RES_LADDER and _RES_LADDER.index(res) > 0:
        return _RES_LADDER[_RES_LADDER.index(res) - 1]
    return None


# ---- picture quality --------------------------------------------------------
@rule
def r_bpp(c: Ctx):
    if not (c.transcode and c.bitrate and c.size):
        return None
    w, h = c.size
    bpp = c.bitrate / (w * h * c.fps_eff)
    note = " (assuming 25 fps)" if c.fps_assumed else ""
    if bpp < 0.03:
        want = _round_rate(0.05 * w * h * c.fps_eff)
        return _f("bitrate-low", "warn", "quality", ["video_bitrate"],
                  f"{c.o.video_bitrate} for {w}×{h}{note} is {bpp:.3f} bits per pixel: "
                  "expect blocking and smearing in motion.",
                  "Live H.264 usually wants 0.05-0.10 bits per pixel. Either raise the "
                  "bitrate or lower the resolution.",
                  set=_scaled_rates(c, want))
    tip = c.g["bpp_low_tip"]
    if tip and bpp < tip:
        want = _round_rate(tip * 1.2 * w * h * c.fps_eff)
        return _f("bitrate-low-goal", "tip", "quality", ["video_bitrate"],
                  f"{c.o.video_bitrate} for {w}×{h}{note} is {bpp:.3f} bits per pixel, "
                  f"on the low side for the '{c.g['label']}' goal.",
                  "Quality-focused streams look better above about "
                  f"{tip:g} bits per pixel.",
                  set=_scaled_rates(c, want))
    if bpp > c.g["bpp_high"]:
        want = _round_rate(0.10 * w * h * c.fps_eff)
        return _f("bitrate-high", "tip", "load", ["video_bitrate"],
                  f"{c.o.video_bitrate} for {w}×{h}{note} is {bpp:.2f} bits per pixel: "
                  "well past the point where the picture improves.",
                  "Extra bitrate costs bandwidth (and a slow link) but adds almost no "
                  "visible quality.",
                  set=_scaled_rates(c, want))


def _round_rate(bps: float) -> float:
    for step in (50_000, 100_000, 250_000, 500_000):
        if bps < step * 20:
            return max(step, round(bps / step) * step)
    return round(bps / 500_000) * 500_000


def _scaled_rates(c: Ctx, new_bps: float) -> dict:
    """Bitrate fix that keeps maxrate/bufsize at the same ratios (only the
    ones that are actually rendered)."""
    out = {"video_bitrate": fmt_rate(new_bps)}
    if c.maxrate:
        out["maxrate"] = fmt_rate(new_bps * (c.maxrate / c.bitrate))
    if c.bufsize:
        out["bufsize"] = fmt_rate(new_bps * (c.bufsize / c.bitrate))
    return out


@rule
def r_cqp_uncapped(c: Ctx):
    if not (c.g["cap_cqp"] and c.codec in VAAPI_ENCODERS and c.rc in ("CQP", "ICQ")):
        return None
    return _f("cqp-uncapped", "tip", "quality", ["rc_mode"],
              f"{c.rc} sets no bitrate limit: a busy scene can burst far above the "
              "viewer's connection and stall the stream.",
              "Fine on a LAN. For internet viewers VBR with a maxrate caps the spikes "
              "(the bitrate fields already hold the values).",
              set={"rc_mode": "VBR"})


@rule
def r_quality(c: Ctx):
    if not (c.codec in VAAPI_ENCODERS and c.rc in ("CQP", "ICQ", "QVBR")
            and "global_quality" not in c.inactive):
        return None
    q = _num(c.o.global_quality)
    if q is None:
        return None
    if q < 18:
        return _f("quality-too-high", "tip", "load", ["global_quality"],
                  f"Quality {int(q)} is near-lossless: huge, unbounded bitrates for a "
                  "gain nobody can see on TV material.",
                  "22-30 is the usable live-IPTV band; 26 is a solid default.",
                  set={"global_quality": "26"})
    if q > 36:
        return _f("quality-too-low", "tip", "quality", ["global_quality"],
                  f"Quality {int(q)} is very coarse: visible blocking on anything but "
                  "static pictures.",
                  "22-30 is the usable live-IPTV band; 26 is a solid default.",
                  set={"global_quality": "26"})


@rule
def r_audio(c: Ctx):
    o = c.o
    if o.audio_codec in ("copy", "none"):
        return None
    out = []
    if o.audio_codec == "mp2" and o.audio_channels == "6":
        out.append(_f("audio-mp2-surround", "critical", "compat", ["audio_channels"],
                      "MP2 audio supports at most two channels; 5.1 makes ffmpeg fail "
                      "at start.", "Use AC3 for 5.1, or downmix to stereo.",
                      set={"audio_channels": "2"}))
    abps = rate_bps(o.audio_bitrate) if "audio_bitrate" not in c.inactive else None
    if o.audio_channels == "6" and o.audio_codec in ("aac", "ac3") and abps and abps < 256_000:
        out.append(_f("audio-surround-low", "warn", "quality", ["audio_bitrate"],
                      f"5.1 audio at {o.audio_bitrate} is about {abps / 6000:.0f} kbit/s "
                      "per channel: muddy and full of artefacts.",
                      "5.1 wants 384k (AC3/AAC) or more.",
                      set={"audio_bitrate": "384k"}))
    if (o.audio_rate or "") == "44100":
        out.append(_f("audio-rate-44k", "tip", "quality", ["audio_rate"],
                      "44.1 kHz audio against video: TV and DVB run at 48 kHz, so the "
                      "audio is resampled and can drift against the picture on long "
                      "streams.",
                      "48 kHz matches broadcast sources and Enigma2 boxes.",
                      set={"audio_rate": "48000"}))
    return out or None


# ---- compatibility ------------------------------------------------------------
@rule
def r_level(c: Ctx):
    if not (c.transcode and c.h264 and c.size and (c.o.level or "").strip()):
        return None
    key = _level_key(c.o.level)
    if key is None:
        return None
    fps = c.fps or 30.0            # an unknown source fps must not cause false alarms
    need = level_needed(c.size[0], c.size[1], fps)
    max_mbps, max_fs = _LEVEL_BY_NAME[key]
    mbs = math.ceil(c.size[0] / 16) * math.ceil(c.size[1] / 16)
    if mbs <= max_fs and mbs * fps <= max_mbps:
        return None
    return _f("level-too-low", "critical", "compat", ["level"],
              f"H.264 level {c.o.level} cannot carry {c.size[0]}×{c.size[1]} at "
              f"{fps:g} fps; set-top boxes may refuse or stutter on a stream that "
              "breaks its declared level.",
              f"This size and frame rate needs level {need or 'above 5.2'} or higher.",
              set={"level": need} if need else None)


# ---- this host ------------------------------------------------------------------
@rule
def r_device(c: Ctx):
    if not (c.transcode and c.o.hw_accel in ("vaapi", "qsv") and c.codec != "copy"
            and (c.codec in VAAPI_ENCODERS or c.codec.endswith("_qsv"))):
        return None
    devices = c.devices()
    if devices is None:
        return None
    entry = next((d for d in devices if d["path"] == c.o.device), None)
    if entry is None:
        return None
    if not entry["exists"]:
        alt = next((d["path"] for d in devices if d["exists"] and d["accessible"]), None)
        return _f("device-missing", "critical", "host", ["device"],
                  f"{c.o.device} does not exist in this container, so this template "
                  "cannot start."
                  + (f" {alt} is available." if alt else " No render device is mapped."),
                  "Map /dev/dri into the container, or use a software or Copy "
                  "template on hosts without a GPU.",
                  set={"device": alt} if alt else None)
    if not entry["accessible"]:
        return _f("device-denied", "critical", "host", ["device"],
                  f"{c.o.device} exists but this container's user cannot read and write "
                  "it.",
                  "Add the container to the group that owns the render node "
                  "(see the README's VAAPI section).")
    caps = c.vaapi_caps()
    if c.codec == "h264_vaapi" and caps.get("h264_encode") is False:
        return _f("encoder-unsupported", "critical", "host", ["video_codec"],
                  "The GPU driver lists no H.264 encode entrypoint for this device.",
                  "vainfo shows no VAEntrypointEncSlice for H.264 here; use "
                  "software encoding or another render node.")
    if c.codec == "hevc_vaapi" and caps.get("hevc_encode") is False:
        return _f("encoder-unsupported", "critical", "host", ["video_codec"],
                  "The GPU driver lists no HEVC encode entrypoint for this device.",
                  "Use h264_vaapi.", set={"video_codec": "h264_vaapi"})


@rule
def r_encoder_missing(c: Ctx):
    encoders = c.env.get("encoders")
    if not (c.transcode and isinstance(encoders, list) and encoders):
        return None
    if c.codec not in encoders:
        return _f("encoder-missing", "critical", "host", ["video_codec"],
                  f"This ffmpeg build has no '{c.codec}' encoder.",
                  "Pick an encoder from the list, or install an ffmpeg build that has "
                  "it.")


# --------------------------------------------------------------------------- #
#  scores (rough, for the meter)
# --------------------------------------------------------------------------- #
_PRESET_LOAD = {"ultrafast": 82, "superfast": 74, "veryfast": 62, "faster": 52, "fast": 44,
                "medium": 34, "slow": 20, "slower": 10, "veryslow": 4, "placebo": 1}
_PRESET_QUALITY = {"ultrafast": -15, "superfast": -10, "veryfast": -5, "faster": -2,
                   "fast": 0, "medium": 2, "slow": 5, "slower": 7, "veryslow": 9, "placebo": 10}


def _clamp(v: float) -> int:
    return int(max(0, min(100, round(v))))


def _interp(x: float, points: list[tuple[float, float]]) -> float:
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def scores(c: Ctx) -> dict:
    """Three 0-100 bars, higher = better: picture quality, time to first
    picture, and how light the stream is on the host. A heuristic overview."""
    o = c.o
    if not c.transcode:
        return {"quality": 100, "start": 92, "efficiency": 100, "basis": "copy"}
    # quality
    if c.bitrate and c.size:
        bpp = c.bitrate / (c.size[0] * c.size[1] * c.fps_eff)
        q = _interp(bpp, [(0.01, 8), (0.03, 30), (0.05, 52), (0.08, 70), (0.12, 84), (0.2, 95)])
    elif c.codec in VAAPI_ENCODERS and c.rc in ("CQP", "ICQ", "QVBR") and _num(o.global_quality) is not None:
        q = _interp(_num(o.global_quality), [(14, 98), (18, 92), (24, 80), (28, 66), (34, 44), (40, 22), (51, 5)])
    else:
        q = 60.0
    if c.sw and c.effective_preset in _PRESET_QUALITY:
        q += _PRESET_QUALITY[c.effective_preset]
    elif c.hw:
        q -= 6
    # start
    if c.gop is not None and c.gop > 1:
        s = _interp(c.gop / c.fps_eff, [(0.5, 96), (2, 92), (4, 72), (8, 42), (12, 22), (20, 8)])
    elif c.gop is None and c.codec in ("libx264", "libx265"):
        s = 24.0
    else:
        s = 60.0
    if o.output_format == "hls":
        s -= 45
    depth = _num(o.async_depth)
    if c.codec in VAAPI_ENCODERS and depth is not None and depth > 8:
        s -= 10
    if c.bitrate and c.bufsize and c.bufsize / c.bitrate > 4:
        s -= 10
    if _flag_value(c.tout, "-tune") == "zerolatency":
        s += 6
    # efficiency
    if c.hw:
        e = 88.0 if (c.codec == "h264_vaapi" and o.low_power) else 78.0
    elif c.sw:
        e = float(_PRESET_LOAD.get(c.effective_preset or "medium", 34))
        if c.codec == "libx265":
            e *= 0.4
    else:
        e = 60.0
    if c.size:
        e -= _interp(c.size[1], [(360, 0), (720, 0), (1080, 12), (2160, 35)]) * (1.0 if c.hw else 1.4)
    if c.fps and c.fps >= 50:
        e -= 12
    return {"quality": _clamp(q), "start": _clamp(s), "efficiency": _clamp(e),
            "basis": "estimate"}


# --------------------------------------------------------------------------- #
#  public API
# --------------------------------------------------------------------------- #
def as_options(options) -> FFmpegOptions:
    from .ffmpeg_templates import coerce_options
    if isinstance(options, FFmpegOptions):
        return options
    return FFmpegOptions(**coerce_options(options if isinstance(options, dict) else {}))


def advise(options, *, goal: str = DEFAULT_GOAL, env: dict | None = None,
           live: dict | None = None, ignored=()) -> dict:
    """Findings, scores and counts for one template. Never raises on odd
    values: a rule that fails on unexpected input is skipped."""
    opts = as_options(options)
    goal = clean_goal(goal)
    ctx = Ctx(opts, goal, env, live)
    dismissed = set(clean_ignored(ignored).split(",")) - {""}
    findings: list[dict] = []
    for fn in RULES:
        try:
            res = fn(ctx)
        except Exception:  # noqa: BLE001 - one broken rule must not hide the rest
            continue
        for f in ([res] if isinstance(res, dict) else (res or [])):
            f["ignored"] = f["id"] in dismissed
            findings.append(f)
    findings.sort(key=lambda f: (_SEV_RANK[f["severity"]], f["id"]))
    shown = [f for f in findings if not f["ignored"]]
    return {
        "goal": goal,
        "findings": findings,
        "counts": {s: sum(1 for f in shown if f["severity"] == s) for s in SEVERITIES},
        "scores": scores(ctx),
    }


def _apply_fix(values: dict, fix: dict) -> None:
    for k, v in (fix.get("set") or {}).items():
        values[k] = v
    for e in fix.get("extra") or []:
        key = "extra_input" if e["side"] == "input" else "extra_output"
        values[key] = set_extra_option(values.get(key, ""), e["side"], e["flag"], e["value"])
    for r in fix.get("remove") or []:
        key = "extra_input" if r["side"] == "input" else "extra_output"
        values[key] = _drop_flag(values.get(key, ""), r["flag"])


def _touched(fix: dict) -> set:
    t = {("f", k) for k in (fix.get("set") or {})}
    t |= {("x", e["side"], e["flag"]) for e in fix.get("extra") or []}
    t |= {("x", r["side"], r["flag"]) for r in fix.get("remove") or []}
    return t


def apply_selected(options, ids, *, goal: str = DEFAULT_GOAL, env: dict | None = None,
                   live: dict | None = None) -> dict:
    """Compose the fixes of the chosen findings into one change set.

    Returns {"changes": [{field, from, to, ids}], "skipped": [{id, reason}],
    "options": the resulting option values}. Findings are applied in severity
    order; one that would change something an earlier one already changed
    (differently) is skipped and reported, never silently merged."""
    opts = as_options(options)
    report = advise(opts, goal=goal, env=env, live=live)
    wanted = [f for f in report["findings"] if f["id"] in set(ids or [])]
    values = asdict(opts)
    original = dict(values)
    touched: dict = {}
    changed_by: dict[str, list[str]] = {}
    skipped: list[dict] = []
    for f in wanted:
        fix = f.get("fix")
        if not fix:
            skipped.append({"id": f["id"], "reason": "no automatic fix for this finding"})
            continue
        keys = _touched(fix)
        clash = next((touched[k] for k in keys if k in touched), None)
        if clash:
            skipped.append({"id": f["id"], "reason": f"conflicts with '{clash}'"})
            continue
        trial = dict(values)
        try:
            _apply_fix(trial, fix)
        except ValueError as exc:
            skipped.append({"id": f["id"], "reason": str(exc)})
            continue
        for k in keys:
            touched[k] = f["id"]
        for fld in trial:
            if trial[fld] != values[fld]:
                changed_by.setdefault(fld, []).append(f["id"])
        values = trial
    changes = [{"field": k, "from": original[k], "to": values[k], "ids": changed_by.get(k, [])}
               for k in values if values[k] != original[k]]
    return {"changes": changes, "skipped": skipped, "options": values}


# ---- helpers for the live-speed log line --------------------------------------
def is_transcode_command(command: str | None) -> bool:
    toks = _tokens(command or "")
    if "-c:v" not in toks and "-vcodec" not in toks:
        return False
    flag = "-c:v" if "-c:v" in toks else "-vcodec"
    return _flag_value(toks, flag) not in (None, "", "copy")


def hint_for_command(command: str | None) -> str:
    """One practical sentence for 'this running template is too slow'."""
    toks = _tokens(command or "")
    codec = _flag_value(toks, "-c:v") or _flag_value(toks, "-vcodec") or ""
    if codec == "libx264":
        preset = _flag_value(toks, "-preset")
        if not preset:
            return ("libx264 has no -preset (it runs 'medium'): add -preset veryfast or "
                    "superfast, or move the encode to the GPU (VAAPI).")
        if preset in X264_PRESETS and X264_PRESETS.index(preset) > 2:
            return f"-preset {preset} is slow for live: try veryfast or superfast."
        return "Lower the resolution or move the encode to the GPU (VAAPI)."
    if codec == "libx265":
        return "libx265 is very CPU-hungry: switch to H.264 (GPU or libx264 veryfast)."
    return ("Lower the resolution or fps. If the source itself is slow (panel or "
            "network), a faster template will not help.")
