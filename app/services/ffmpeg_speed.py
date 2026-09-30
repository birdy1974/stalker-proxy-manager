"""Real-time speed of a running ffmpeg, read from its own progress lines.

ffmpeg prints a stats line about twice a second on stderr::

    frame=  250 fps= 25 q=26.0 size=  1024kB time=00:00:10.00 bitrate= 838.9kbits/s speed=1.01x

`speed=` is the *cumulative* average since ffmpeg started, start-up included,
so an encoder that falls behind an hour into a stream barely moves it. What
matters for a live stream is the speed over the last few seconds, so this
module derives that from consecutive ``time=`` values and the moment each line
arrived (``media seconds advanced / wall seconds passed``).

A transcode needs >= 1.0x to keep up with a live source. Below that the
encoder (or the source) delivers less than one second of picture per second,
the player's buffer drains and the picture stutters. `SpeedWatch` reports that
once per stream, after a warm-up and only when it stays slow over several
consecutive windows, so a hiccup while the panel reconnects is not reported as
"your template is too slow".

Per-template results are kept in `SPEED_STATS` (in memory) so the template
editor can show "last live run: 0.82x" next to its other advice.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field

#: live speed below this (sustained) is reported as too slow
SLOW_BELOW = 0.95
#: ignore the first seconds of a stream (hardware init, probing, a burst)
WARMUP_S = 15.0
#: the window the instantaneous speed is computed over
WINDOW_S = 10.0
#: a verdict is taken at most this often - once per window, so consecutive
#: verdicts look at *different* seconds (overlapping windows would count one
#: 8-second stall three times)...
EVAL_EVERY_S = WINDOW_S
#: ...and "slow" needs this many consecutive slow verdicts: ~30 s sustained
SLOW_STREAK = 3
#: how long a per-template measurement stays meaningful
STATS_TTL_S = 24 * 3600.0

_STATS = re.compile(r"frame=\s*\d+.*?time=\s*(?P<t>[\d:.]+|N/A).*?speed=\s*(?P<s>[\d.]+|N/A)x?")
_TIME = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")


def parse_time(text: str) -> float | None:
    m = _TIME.match(text or "")
    if not m:
        return None
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])


def parse_progress(line: str) -> dict | None:
    """{'time': media seconds, 'speed': cumulative x} from one stats line; None
    for any other stderr line (and for the `N/A` ffmpeg prints before the
    first frame)."""
    m = _STATS.search(line or "")
    if not m:
        return None
    t = parse_time(m["t"])
    if t is None:
        return None
    try:
        speed = float(m["s"])
    except ValueError:
        speed = None
    return {"time": t, "speed": speed}


def looks_like_progress(segment: bytes) -> bool:
    """A `\\r`-terminated stats line: kept out of the stderr tail, which would
    otherwise be flushed clean of the real error lines within a minute."""
    return b"frame=" in segment and b"time=" in segment


@dataclass
class SpeedWatch:
    """Feed it stderr segments; it says when the stream is persistently slow."""
    samples: deque = field(default_factory=lambda: deque(maxlen=400))
    first_at: float | None = None
    last_eval: float = 0.0
    streak: int = 0
    reported: bool = False
    last_speed: float | None = None      # latest windowed speed, for the dashboard
    cumulative: float | None = None      # ffmpeg's own speed= figure

    def feed(self, text: str, now: float | None = None) -> dict | None:
        """Process stderr text (may hold several `\\r`-separated stats lines).
        Returns an event dict the first time the stream is judged too slow."""
        now = time.monotonic() if now is None else now
        parsed = None
        for part in re.split(r"[\r\n]", text or ""):
            p = parse_progress(part)
            if p:
                parsed = p
        if parsed is None:
            return None
        if self.first_at is None:
            self.first_at = now
        self.cumulative = parsed["speed"]
        self.samples.append((now, parsed["time"]))
        return self._judge(now)

    def window_speed(self, now: float) -> float | None:
        """Media seconds per wall second over the last WINDOW_S seconds."""
        recent = [s for s in self.samples if now - s[0] <= WINDOW_S]
        if len(recent) < 2:
            return None
        wall = recent[-1][0] - recent[0][0]
        if wall < WINDOW_S * 0.6:
            return None
        media = recent[-1][1] - recent[0][1]
        if media < 0:          # timestamps restarted (reconnect): no verdict
            return None
        return media / wall

    def _judge(self, now: float) -> dict | None:
        if self.first_at is None or now - self.first_at < WARMUP_S:
            return None
        if now - self.last_eval < EVAL_EVERY_S:
            return None
        speed = self.window_speed(now)
        if speed is None:
            return None
        self.last_eval = now
        self.last_speed = speed
        if speed < SLOW_BELOW:
            self.streak += 1
        else:
            self.streak = 0
        if self.streak >= SLOW_STREAK and not self.reported:
            self.reported = True
            return {"speed": speed, "seconds": int(now - self.first_at),
                    "cumulative": self.cumulative}
        return None


# --------------------------------------------------------------------------- #
#  per-template live measurements
# --------------------------------------------------------------------------- #
SPEED_STATS: dict[str, dict] = {}


def record(template: str, speed: float, *, now: float | None = None) -> None:
    """Remember the latest windowed speed a stream of `template` reached."""
    if not template or speed is None:
        return
    SPEED_STATS[template] = {"speed": round(float(speed), 3),
                             "at": time.time() if now is None else now,
                             "slow": speed < SLOW_BELOW}


def latest(template: str, *, now: float | None = None) -> dict | None:
    hit = SPEED_STATS.get(template)
    if not hit:
        return None
    age = (time.time() if now is None else now) - hit["at"]
    if age > STATS_TTL_S:
        SPEED_STATS.pop(template, None)
        return None
    return {**hit, "age_s": int(age)}


def reset() -> None:
    SPEED_STATS.clear()


# --------------------------------------------------------------------------- #
#  demo verdict
# --------------------------------------------------------------------------- #
def last_progress(stderr: str) -> dict | None:
    """The last stats line of a finished ffmpeg run (its final, whole-run
    average - the one that matters for a 2 s demo)."""
    found = None
    for part in re.split(r"[\r\n]", stderr or ""):
        p = parse_progress(part)
        if p and p["speed"] is not None:
            found = p
    return found


def demo_verdict(speed: float | None, mode: str) -> dict | None:
    """Turn a demo's measured speed into a plain statement.

    The demo runs ~2 s and its speed includes start-up (hardware init, probing),
    so it understates steady-state speed: the cut-offs are deliberately forgiving
    and only a far-too-slow result is called critical. A playlist demo reads a
    live source that is itself paced at 1.0x, so it can only show the encoder
    is *not* the bottleneck, never how much headroom it has."""
    if speed is None:
        return None
    x = f"{speed:.2f}×"
    if mode == "playlist":
        if speed < 0.5:
            return {"level": "critical", "speed": speed,
                    "text": f"Encoded at {x} real time: far too slow for live."}
        if speed < 0.9:
            return {"level": "warn", "speed": speed,
                    "text": f"Encoded at {x} real time: probably too slow for live (the "
                            "source may also be slow; test with the synthetic demo)."}
        return {"level": "ok", "speed": speed,
                "text": f"Keeps up with the live source ({x}). A live input is read at "
                        "real time, so this cannot show spare headroom."}
    if speed < 0.5:
        return {"level": "critical", "speed": speed,
                "text": f"Encoded at {x} real time: far too slow for live. Lower the "
                        "resolution, use a faster preset or the GPU."}
    if speed < 1.0:
        return {"level": "warn", "speed": speed,
                "text": f"Encoded at {x} real time: below real time. The 2 s demo includes "
                        "start-up, but live viewers would likely see stutter."}
    if speed < 1.5:
        return {"level": "tip", "speed": speed,
                "text": f"Encoded at {x} real time: it keeps up, with little headroom for "
                        "several streams at once."}
    return {"level": "ok", "speed": speed,
            "text": f"Encoded at {x} real time: comfortable headroom."}
