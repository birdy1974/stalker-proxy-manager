"""
Portal short-EPG: "what is on this channel right now", without an XMLTV source.

`type=itv&action=get_short_epg&ch_id=…&size=…` is the one guide call a Stalker
portal answers cheaply - a handful of programme rows for one channel, starting at
or before now. EStalker uses it exactly that way (`getshortepg.py:74`,
`live.py:2129`) and never tries to build a full guide from it, and neither do we:
one request per channel is the pattern that gets an IP banned, so this module
covers the *question a tooltip asks* and no more.

Two things are easy to get wrong and are pinned here:

* **Timezone.** The portal renders these times in the timezone the box declared in
  its `timezone=` cookie, and the answer carries no offset at all
  (`"time": "2026-08-30 11:00:00"`). Reading it as UTC shifts every programme by
  the difference between the panel and the server - which on a NAS set to UTC with
  a Dutch panel is two hours of "nothing is on" and a programme that appears to
  start in the past. So the interpretation is an explicit parameter, and the caller
  passes the same value the cookie says (`Portal.stb_timezone`).
* **A missing programme is not an outage.** `{"js":[]}` means the channel has no
  guide data; a 503 means the panel did not answer. The first is empty information,
  the second is a reason to stop asking for a while, and the two must not share a
  value in what we hand the GUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

__all__ = ["Programme", "parse_short_epg", "pick_now", "parse_portal_ts"]

_TS_KEYS = ("time", "start_timestamp", "start", "begin", "date_start")
_END_KEYS = ("time_to", "stop_timestamp", "stop", "end", "date_stop")
_TITLE_KEYS = ("name", "title", "programme_name", "prog_name")
_DESC_KEYS = ("descr", "description", "desc", "overview")


@dataclass(frozen=True)
class Programme:
    """One row of a short-EPG answer, with aware timestamps."""

    title: str
    start: datetime | None = None
    stop: datetime | None = None
    description: str = ""
    has_archive: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    def public(self) -> dict:
        return {"title": self.title,
                "start": self.start.isoformat(timespec="minutes") if self.start else "",
                "stop": self.stop.isoformat(timespec="minutes") if self.stop else "",
                "description": self.description, "has_archive": self.has_archive}


def _first(row: dict, keys: tuple[str, ...]) -> object:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def parse_portal_ts(value, tz: timezone | None = None) -> datetime | None:
    """A portal timestamp → an aware datetime, assuming `tz` when it is naive.

    Panels send `"2026-08-30 11:00:00"`, `"2026-08-30T11:00:00+02:00"` and unix
    seconds, sometimes in the same answer for different fields. A value we cannot
    read becomes None rather than a guess, because a wrong timestamp puts tonight's
    film on screen as "now".
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            return datetime.fromtimestamp(int(float(value)), timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("T", " ").replace("Z", "+00:00")
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[ T]?(\d{2})?:?(\d{2})?:?(\d{2})?", text)
    if not m:
        return None
    year, month, day = (int(m.group(i) or 1) for i in (1, 2, 3))
    hour, minute, second = (int(m.group(i) or 0) for i in (4, 5, 6))
    try:
        naive = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    rest = text[m.end():]
    off = re.match(r"^\s*([+-])(\d{2}):?(\d{2})$", rest)
    if off:
        delta = timedelta(hours=int(off.group(2)), minutes=int(off.group(3)))
        if off.group(1) == "-":
            delta = -delta
        return naive.replace(tzinfo=timezone(delta))
    if "am" in rest.lower() or "pm" in rest.lower():
        return None
    return naive.replace(tzinfo=tz or datetime.now(timezone.utc).astimezone().tzinfo)


def parse_short_epg(payload, tz: timezone | None = None) -> list[Programme]:
    """The `js` list of a short-EPG answer, in whatever field names it arrived with.

    Accepts the whole response or a bare `js` value, a list or `{"data": […]}`,
    and a `{"js":{"error":…}}` refusal (which yields no rows - the caller already
    has the error through the client's exception path).
    """
    data = payload
    if isinstance(data, dict) and "js" in data:
        data = data["js"]
    if isinstance(data, dict):
        data = data.get("data") or data.get("epg") or data.get("list") or []
    if not isinstance(data, list):
        return []
    out: list[Programme] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        title = str(_first(row, _TITLE_KEYS) or "").strip()
        start = parse_portal_ts(_first(row, _TS_KEYS), tz)
        stop = parse_portal_ts(_first(row, _END_KEYS), tz)
        if not title and start is None:
            continue
        archive = str(_first(row, ("has_archive", "archive", "tv_archive")) or "").lower()
        out.append(Programme(title=title or "(untitled)", start=start, stop=stop,
                             description=str(_first(row, _DESC_KEYS) or "").strip()[:1000],
                             has_archive=archive in ("1", "true", "yes", "on"),
                             raw=row if len(str(row)) < 800 else {}))
    out.sort(key=lambda p: (p.start is None, p.start or datetime.min.replace(tzinfo=timezone.utc)))
    return out


def pick_now(programmes: list[Programme], *, now: datetime | None = None,
              lookback_minutes: int = 90) -> dict:
    """Which row is "now", which is "next", and how far the current one has run.

    `lookback_minutes` exists because panels list the *upcoming* rows and a
    programme that started two hours ago is frequently already off the end of the
    list: if the last row in the answer has already ended but ended *recently*, it
    is still a better answer than "nothing is on", and the GUI shows it as
    finishing (that is what a channel-switch tooltip is for).
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    current = nxt = None
    for p in programmes:
        if p.start is None:
            continue
        end = p.stop if p.stop and p.stop > p.start else None
        if p.start <= now and (end is None or now < end):
            if current is None or p.start > current.start:
                current = p
        elif p.start > now and (nxt is None or p.start < nxt.start):
            nxt = p
    if current is None and programmes:
        last = [p for p in programmes if p.start and p.start <= now]
        # the tail of the list is the "recently started" candidate
        if last:
            tail = last[-1]
            ended = tail.stop or (tail.start + timedelta(hours=1))
            if now - ended <= timedelta(minutes=lookback_minutes):
                current = tail
    out: dict = {"found": current is not None}
    if current is not None:
        end = current.stop
        pct = None
        left = None
        if end and current.start and end > current.start:
            total = (end - current.start).total_seconds()
            pct = max(0, min(100, round((now - current.start).total_seconds() / total * 100)))
            left = max(0, round((end - now).total_seconds() / 60))
        out["now"] = dict(current.public(), progress=pct, minutes_left=left)
    else:
        out["now"] = None
    if nxt is not None:
        starts = round((nxt.start - now).total_seconds() / 60) if nxt.start else None
        out["next"] = dict(nxt.public(), starts_in=starts)
    else:
        out["next"] = None
    return out
