"""
What we asked each panel, and what it answered - the exchange log.

The diagnostics card answers "why is zapping slow" from aggregates: percentiles,
refusal counts, probe cache hits. This is the other half, the individual
requests in the order they happened, with the timestamps a support conversation
actually needs:

    12:04:31.482  portal-a  00:1A:79:AA:01  type=itv action=create_link cmd=…
    12:04:31.913  -> HTTP 200 in 431 ms, 1.4 kB, js.cmd = http://…/392166.ts

Everything is in-memory and bounded, for the same reason api_stats is: this runs
on the hot path of every channel page, link request and health probe, and a
database write per panel call would be the first thing a busy proxy notices.
The ring keeps the last `TRAFFIC_HISTORY` exchanges of *this process*; a restart
starts empty, and the container log keeps the same events under `[spm.portal]`.

Secrets never enter a row. A bearer or a `play_token` in a screenshot is a
working credential, so every value we store passes `_mask` first.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from typing import Any
from urllib.parse import urlsplit

#: How many exchanges to keep. 400 covers a full catalogue page walk on one
#: portal plus a few hundred plays, which is what a debugging session needs, and
#: costs ~200 KB. `SPM_PORTAL_TRAFFIC=0` turns the log off entirely.
TRAFFIC_HISTORY = int(os.environ.get("SPM_PORTAL_TRAFFIC", "400"))

#: Bounds on what one row stores: enough to diagnose, small enough that a
#: 10 000-channel portal cannot turn the ring into a second channel list.
PARAM_CHARS = 220
ANSWER_CHARS = 220
ERROR_CHARS = 140

#: Parameter names whose value is a credential, and regex for the same thing
#: inside a URL (`cmd=…&play_token=…`). Both are masked, not dropped: seeing
#: *that* the panel was sent a token is part of the diagnosis.
SECRET_PARAMS = frozenset({"token", "play_token", "access_token", "prehash",
                           "auth_second_step", "signature", "password"})
_SECRET_IN_URL = re.compile(
    r"(?i)\b(token|play_token|access_token|signature|prehash)=([^&\s\"']+)")

#: What a row's `outcome` means, for the GUI's filter and its colour:
#:   ok       the panel answered and we accepted the answer
#:   refused  the panel answered and said no, with a code (limit, http_429, …)
#:   failed   no answer at all (timeout, DNS, connection reset)
#:   skipped  we did not send it - the portal host was paused after a 429
OUTCOMES = ("ok", "refused", "failed", "skipped")

_ring: deque[dict] = deque(maxlen=max(1, TRAFFIC_HISTORY))
_next_id = 0
#: Writers are concurrent (every portal call runs in its own task) and a deque
#: append is atomic in CPython, but the id counter and the stats read are not.
_lock = threading.Lock()


def host_of(portal_url: str) -> str:
    """The portal's host, as the key the GUI filters on.

    Duplicated from `portal.client.portal_host` on purpose: importing it here
    would be a cycle (client -> portal_traffic -> client), and this is three
    lines that cannot drift into being wrong in a harmful way.
    """
    try:
        return (urlsplit(portal_url or "").netloc or portal_url or "").lower()
    except Exception:  # noqa: BLE001 - a malformed URL is not worth a crash
        return str(portal_url or "").lower()


def _mask(text: Any) -> str:
    """Text with every credential-shaped value replaced by `***`."""
    return _SECRET_IN_URL.sub(lambda m: f"{m.group(1)}=***", str(text or ""))


def _clip(text: str, limit: int) -> str:
    text = _mask(text).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def request_line(params: dict | None) -> str:
    """The request as the panel sees it: `type=itv&action=create_link&cmd=…`."""
    if not params:
        return ""
    bits = []
    for k, v in params.items():
        if k == "JsHttpRequest":          # our transport quirk, never diagnostic
            continue
        bits.append(f"{k}=***" if str(k).lower() in SECRET_PARAMS else f"{k}={v}")
    return _clip("&".join(bits), PARAM_CHARS)


def summarize_answer(data: Any) -> str:
    """The part of a panel answer that tells you what happened.

    A channel page is "data: 32 item(s)", a link request is the cmd it answered
    with, anything else is its keys. Storing whole bodies would be a second copy
    of the catalogue and would push the interesting rows out of the ring.

    The list scan is generic on purpose: panels put their items under `data`,
    `ch_list`, `epg`, `genres`, `my_accounts` - whichever it is, "how many came
    back" is the number worth a column.
    """
    js = data.get("js") if isinstance(data, dict) else data
    if isinstance(js, list):
        return f"js: {len(js)} item(s)"
    if isinstance(js, dict):
        bits = []
        if js.get("cmd"):
            bits.append(_clip(f"cmd={js['cmd']}", 120))
        lists = [(k, v) for k, v in js.items() if isinstance(v, list)][:3]
        bits += [f"{k}: {len(v)} item(s)" for k, v in lists]
        for k in ("id", "total_items", "selected_item", "max_page_items", "token",
                  "blocked", "status"):
            if js.get(k) not in (None, ""):
                bits.append(f"{k}={js[k]}")
        if not bits:
            keys = ", ".join(str(k) for k in list(js)[:8])
            bits.append(f"js keys: {keys}")
        return _clip(", ".join(bits), ANSWER_CHARS)
    if js is None:
        return f"{type(data).__name__} payload (no js)"
    return _clip(str(js), ANSWER_CHARS)


def outcome_of(status: int, code: str, skipped: bool) -> str:
    if skipped:
        return "skipped"
    if status == 0:
        return "failed"
    return "refused" if code else "ok"


def record(*, portal_url: str, mac: str = "", params: dict | None = None,
           status: int = 0, code: str = "", error: str = "", size: int = 0,
           answer: str = "", ms: float = 0.0, started: float | None = None,
           retried: bool = False, skipped: bool = False, stage: int = 0) -> dict:
    """Append one finished exchange. Returns the row (the GUI's detail view)."""
    global _next_id
    if TRAFFIC_HISTORY <= 0:
        return {}
    started = time.time() if started is None else started
    row = {
        "t": round(started, 3),                       # when we asked (epoch s)
        "ms": round(max(0.0, ms), 1),                 # round trip
        "host": host_of(portal_url),
        "mac": _clip(mac, 32),
        "action": str((params or {}).get("action") or ""),
        "type": str((params or {}).get("type") or ""),
        "params": request_line(params),
        "status": int(status),
        "code": str(code or ""),
        "outcome": outcome_of(status, code, skipped),
        "error": _clip(error, ERROR_CHARS),
        "bytes": int(size or 0),
        "answer": _clip(answer, ANSWER_CHARS),
        "retried": bool(retried),
        "stage": int(stage),                          # handshake shape (1-3)
    }
    with _lock:
        _next_id += 1
        row["id"] = _next_id
        _ring.append(row)
    return row


class Exchange:
    """One portal request: starts the clock, ends with the reason it ended.

    `_get` has a dozen exits - transport error, 429, a 401 that re-handshakes,
    a 200 carrying `{"js":{"error":"limit"}}` - and every one of them is a row
    somebody will want. So the row is created before the request goes out and
    closed by `done()`, which is idempotent: each exit describes its own outcome
    where it happens (which keeps the ring in the order the panel saw), and the
    caller's `finally` is only the net for a path that forgot.
    """

    __slots__ = ("portal_url", "mac", "params", "started", "_mono", "retried",
                 "stage", "_closed")

    def __init__(self, portal_url: str, mac: str = "", params: dict | None = None,
                 *, retried: bool = False, stage: int = 0) -> None:
        self.portal_url = portal_url
        self.mac = mac
        self.params = params
        self.started = time.time()
        self._mono = time.monotonic()
        self.retried = retried
        self.stage = stage
        self._closed = False

    @property
    def ms(self) -> float:
        return (time.monotonic() - self._mono) * 1000.0

    def done(self, *, status: int = 0, code: str = "", error: str = "",
             size: int = 0, answer: str | None = None,
             data: Any = None, skipped: bool = False) -> dict:
        """Close the row exactly once. `data` summarizes itself when given."""
        if self._closed:
            return {}
        self._closed = True
        return record(portal_url=self.portal_url, mac=self.mac, params=self.params,
                      status=status, code=code, error=error, size=size,
                      answer=answer if answer is not None else summarize_answer(data),
                      ms=self.ms, started=self.started, retried=self.retried,
                      skipped=skipped, stage=self.stage)


def exchange(portal_url: str, mac: str = "", params: dict | None = None, *,
             retried: bool = False, stage: int = 0) -> Exchange:
    return Exchange(portal_url, mac, params, retried=retried, stage=stage)


def _matches(row: dict, host: str, outcome: str, q: str) -> bool:
    if host and row["host"] != host:
        return False
    if outcome == "errors":
        if row["outcome"] == "ok":
            return False
    elif outcome and row["outcome"] != outcome:
        return False
    if q:
        hay = " ".join((row["host"], row["mac"], row["action"], row["type"],
                        row["params"], row["code"], row["error"], row["answer"],
                        str(row["status"]))).lower()
        if q not in hay:
            return False
    return True


def stats() -> dict:
    """The one-line summary the Zapping card shows, plus the filter facets."""
    rows = list(_ring)
    counts = {k: 0 for k in OUTCOMES}
    hosts: dict[str, int] = {}
    codes: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
        hosts[r["host"]] = hosts.get(r["host"], 0) + 1
        if r["code"]:
            codes[r["code"]] = codes.get(r["code"], 0) + 1
    last_minute = sum(1 for r in rows if time.time() - r["t"] <= 60)
    return {"enabled": TRAFFIC_HISTORY > 0, "kept": len(rows), "history": TRAFFIC_HISTORY,
            "counts": counts, "last_minute": last_minute,
            "oldest": rows[0]["t"] if rows else 0.0,
            "newest": rows[-1]["t"] if rows else 0.0,
            "hosts": [{"host": h, "count": n}
                      for h, n in sorted(hosts.items(), key=lambda kv: -kv[1])],
            "codes": [{"code": c, "count": n}
                      for c, n in sorted(codes.items(), key=lambda kv: -kv[1])][:12]}


def query(*, page: int = 1, per_page: int = 25, host: str = "", outcome: str = "",
          q: str = "") -> dict:
    """Newest first, filtered, paged - the shape the GUI's table renders."""
    per_page = max(1, min(int(per_page or 25), 200))
    page = max(1, int(page or 1))
    host = (host or "").strip().lower()
    outcome = (outcome or "").strip().lower()
    q = (q or "").strip().lower()
    rows = [r for r in reversed(_ring) if _matches(r, host, outcome, q)]
    total = len(rows)
    start = (page - 1) * per_page
    st = stats()
    return {"total": total, "page": page, "per_page": per_page,
            "items": rows[start:start + per_page],
            "kept": st["kept"], "history": st["history"], "counts": st["counts"],
            "last_minute": st["last_minute"], "hosts": st["hosts"], "codes": st["codes"]}


def clear() -> None:
    """Forget every row (the GUI's Clear, and the tests)."""
    global _next_id
    with _lock:
        _ring.clear()
        _next_id = 0
