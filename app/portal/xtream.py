"""
The MAC → Xtream bridge: a portal that hands out Xtream links, read as such.

Why this exists
---------------
A Stalker portal authenticated by MAC is, for many panels, the *same account* as
an Xtream-codes login: the panel builds a stream URL of the form
``/live/<user>/<pass>/<id>.ts`` (or ``/movie/``, ``/series/``) and hands it to the
box. EStalker noticed this and uses it (`playlists.py:1030-1075`): harvest the
credentials out of one `create_link` answer, then query ``player_api.php`` like
any other Xtream client - which gives account status (`Active`, `exp_date`,
`active_cons`, `max_connections`) that the portal API reports late or not at all,
and a playlist format that is cheaper and far more robust to stream than a MAC
session.

This module is the **pure** half of that: parsing a link, building URLs, reading
`player_api.php` answers, and matching a panel's channel list to an Xtream one.
The impure half - who asks, what gets stored, and the explicit user action that
turns an observation into playback - is `app/services/xtream_bridge.py`.

What is deliberately *not* here
-------------------------------
* **No automatic adoption.** A panel that gives out Xtream links is not
  necessarily a panel that wants its MAC-derived stream traffic moved onto a
  username/password it may be rate-limiting separately, and credentials that
  work today may be rotated by the panel's billing software. So detection only
  *reports*; one explicit user action switches playback over, and one switches
  it back (§R7 in docs/ESTALKER-COMPARISON.md).
* **No credential in a URL we log or display unmasked** (`mask_password`,
  `XtreamCreds.masked`). These strings are passwords, and this project writes
  its log lines to a GUI pane.
* **No session token mistaken for a password.** Some panels put a 32-hex blob in
  that path position; it is a link token, and building an Xtream login out of it
  produces an account that expires in minutes and a locked panel if you retry it
  a hundred times. EStalker has the same guard (`len(password) != 32`); here it is
  a named function with the reasoning attached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

__all__ = ["XtreamCreds", "XtreamAccount", "harvest", "mask_password",
           "looks_like_session_token", "parse_player_api", "parse_streams",
           "norm_title", "plan_adoption", "xtream_base", "media_base"]

#: the path prefixes an Xtream-codes server serves streams under. `live`,
#: `movie` and `series` are the standard three; `streaming` is what some
#: panels wrap around them (`/streaming/live/u/p/x.ts`), and a bridge that
#: only knows the canonical three silently fails on those.
PATH_KINDS = {
    "live": "live",       # → our "live"
    "movie": "movie",     # → our "vod"
    "series": "series",   # → our "series"
    "streaming": "streaming",
}

#: `/live/<user>/<pass>/…` - the user and password are single path segments, and
#: `[^/]+` (not `.+`) is what keeps a malformed URL from swallowing the rest of
#: the path into the username. `streaming/` is matched as a *wrapper* rather than
#: as a kind, so both `/live/u/p/` and `/streaming/live/u/p/` work.
#:
#: That distinction is not pedantry, it is the difference between harvesting the
#: account and harvesting nonsense: while `streaming` was also a kind, the pattern
#: could match `http://h/streaming/live/u/p/1.ts` with `h` as the wrapper and
#: `streaming` as the kind, which shifts every group by one segment and yields
#: user=`live`, password=`u` - a wrong credential sent to a paying panel, which is
#: exactly the request pattern that gets an IP locked. Any short `[a-z_]+` host name
#: (`h`, `server`, `localhost`) hit this.
#: a credential segment is raw characters or `%XX` escapes, and nothing else: the
#: escape alternative exists because a password containing `%` is ordinary, and a
#: parser that stopped at the first `%` would harvest a *truncated* password, get
#: refused by the media host and log a wrong secret. If the escapes do not
#: parse as a credential at all the match simply fails, which is the safe answer:
#: refuse rather than guess half a password against somebody's subscription.
_CRED_SEG = r"(?:[^/?%\s]|%[0-9a-fA-F]{2})"
_CRED_PATH_RE = re.compile(
    r"/(?:(?P<wrap>streaming)/)?(?P<kind>live|movie|series|vod)/"
    rf"(?P<user>{_CRED_SEG}{{1,128}})/(?P<pass>{_CRED_SEG}{{1,256}})/",
    re.I)

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def looks_like_session_token(password: str) -> bool:
    """True when that "password" is really a per-link token, not an account.

    A 32-char hex string in the credential slot is a panel's link signature:
    short-lived, and usually bound to one stream. Adopting it as an Xtream login
    looks like a win until the token expires and every channel in the playlist is
    dead - and a client that then retries `player_api.php` in a loop is how a
    panel decides your IP is a brute-forcer.
    """
    return bool(_TOKEN_RE.match(str(password or "").strip()))


_PATH_CRED_RE = re.compile(r"(?i)(/(?:live|movie|series|streaming/[^/]+|streaming)/([^/]+)/)([^/]+)/")
_QUERY_CRED_RE = re.compile(r"(?i)(password=|pass=|pwd=)([^&\s]*)")


def mask_password(text: str | None) -> str:
    """Mask the *password* in a URL, keeping the username.

    Both forms matter: the path credential (`/live/john/SECRET/1.ts`) and the
    query one `get.php`/`player_api.php` use. The username stays readable on
    purpose - it is the string a user needs in order to tell which account a log
    line is about, and it is not the secret.
    """
    out = _PATH_CRED_RE.sub(lambda m: f"{m.group(1)}***/", str(text or ""))
    return _QUERY_CRED_RE.sub(
        lambda m: m.group(1) + (m.group(2)[:2] + "***" if len(m.group(2)) > 4 else "***"), out)


@dataclass(frozen=True)
class XtreamCreds:
    """A harvested Xtream identity: where to ask, and as whom."""

    base: str                     # scheme://host[:port] - no trailing slash
    username: str
    password: str
    #: which Xtream path the harvest came from (`live`/`movie`/`series`), because
    #: a panel that exposes only `/movie/` may still refuse `/live/`
    kind: str = ""
    #: True when the base came from `server_info.http_live_url` rather than being
    #: derived from the link we harvested - a panel that hands out a stream host
    #: *and* a portal host has two of them, and only one carries the media
    from_server_info: bool = False
    #: Xtream servers that need no password (DNS-locked accounts) answer `auth: 0`
    auth: int = 1

    # -- URL builders -------------------------------------------------------- #
    def api_url(self, action: str = "", **extra) -> str:
        """`player_api.php` with the credentials in the query (the standard form)."""
        q = [f"username={quote(self.username, safe='')}",
             f"password={quote(self.password, safe='')}"]
        if action:
            q.append(f"action={quote(action, safe='')}")
        for k, v in extra.items():
            if v is not None and v != "":
                q.append(f"{k}={quote(str(v), safe='')}")
        return f"{self.base}/player_api.php?" + "&".join(q)

    def stream_url(self, stream_id: str, kind: str = "live", extension: str = "ts") -> str:
        """The direct URL for one stream id - what a player or ffmpeg is given."""
        seg = {"live": "live", "vod": "movie", "movie": "movie",
               "series": "series", "episode": "series"}.get(kind, kind or "live")
        ext = str(extension or ("ts" if seg == "live" else "mp4")).lstrip(".")
        return (f"{self.base}/{seg}/{quote(self.username, safe='')}/"
                f"{quote(self.password, safe='')}/{stream_id}.{ext}")

    def playlist_url(self, style: str = "m3u_plus") -> str:
        """An m3u a user can also paste into a player that speaks Xtream directly."""
        return (f"{self.base}/get.php?username={quote(self.username, safe='')}"
                f"&password={quote(self.password, safe='')}&type={quote(style, safe='')}&output=1")

    def epg_url(self) -> str:
        return (f"{self.base}/xmltv.php?username={quote(self.username, safe='')}"
                f"&password={quote(self.password, safe='')}")

    # -- safe forms ---------------------------------------------------------- #
    def public(self) -> dict:
        """Everything a GUI or an export may show: the password is masked."""
        return {"base": self.base, "username": self.username,
                "password": "****" if self.password else "",
                "has_password": bool(self.password),
                "kind": self.kind, "auth": self.auth,
                "from_server_info": self.from_server_info,
                "api_url": mask_password(self.api_url()),
                "playlist_url": mask_password(self.playlist_url()),
                "epg_url": mask_password(self.epg_url())}

    def masked(self) -> str:
        """One line for a log: origin, username, masked password, placeholder id.

        Built through `stream_url` rather than by hand on purpose - `mask_password`
        recognises a credential by the *path kind* in front of it, so a string like
        `http://h/…/john/secret/…` looked unrecognisable to it and this method would
        have logged the secret it exists to hide.
        """
        return mask_password(self.stream_url("…", self.kind or "live"))


def xtream_base(url: str) -> str:
    """`scheme://host[:port]` of any URL - the origin, without path or query."""
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def harvest(link: str) -> XtreamCreds | None:
    """Pull the Xtream identity out of a stream URL, or None if there is none.

    Takes the *portal's* `create_link` answer (`/movie/<u>/<p>/12345.mp4`) as well
    as a full URL, and also accepts an already-derived Xtream URL, because the
    caller sometimes has the latter after `merge_link`.
    """
    text = str(link or "").strip()
    if not text:
        return None
    # a cmd can carry the URL among other tokens (`ffmpeg http://…`)
    if " " in text:
        for tok in text.split():
            got = harvest(tok)
            if got:
                return got
        return None
    m = _CRED_PATH_RE.search(text)
    if not m:
        return None
    origin = xtream_base(text)
    if not origin:
        return None
    # Whatever path the panel put *in front of* the credential segment belongs to the
    # base: a server that streams from `http://h/xtream/live/u/p/1.ts` also answers
    # `player_api.php` at `/xtream/`, and stripping to the origin would take a
    # borrowed password to a different vhost. A root-hosted panel (the common case,
    # and what EStalker assumes) has no prefix and stays at the origin.
    prefix = urlsplit(text[:m.start("kind")]).path.rstrip("/")
    # …minus `streaming`, which is Xtream's word for "this is a stream", not a path
    # prefix: a panel answering `/streaming/live/<u>/<p>/x.ts` still serves
    # `player_api.php` at the root, and keeping the segment would send the password
    # to a URL that does not exist. Only a trailing one, so `/mock/streaming/live/…`
    # keeps its real prefix.
    low = prefix.lower()
    if low == "/streaming":
        prefix = ""
    elif low.endswith("/streaming"):
        prefix = prefix[: -len("/streaming")]
    base = origin + prefix
    # decoded here, re-quoted by every builder below: a path segment's *value* is
    # the credential, and `XtreamCreds.stream_url` quotes again on the way out. A
    # panel that wrote `pa%2Fss` in the link means a password containing a slash -
    # keeping the escapes would send `pa%252Fss` and the media host says 403,
    # which is a bug that only appears for the few users whose password has punctuation
    user, pw = unquote(m.group("user")), unquote(m.group("pass"))
    # `[^/]` above means an unquoted slash inside a credential cannot match, and a
    # panel that emits one is a panel we do not adopt (the URL is unparseable)
    if looks_like_session_token(pw):
        return None
    if user.lower() in ("", "null", "undefined") or pw.lower() in ("", "null", "undefined"):
        return None
    kind = (m.group("kind") or "").lower()
    return XtreamCreds(base=base, username=user, password=pw,
                       kind="live" if kind == "live" else
                       ("movie" if kind in ("movie", "vod") else "series"))


# ---------------------------------------------------------------------------
# player_api.php
# ---------------------------------------------------------------------------
_STATUS_MAP = {
    # Xtream's own words → our MacStatus vocabulary, so one badge renderer covers
    # both the panel's account state (R3) and a harvested Xtream one
    "active": "online",
    "online": "online",
    "expired": "expired",
    "banned": "banned",
    "disabled": "banned",
    "blocked": "banned",
    "suspended": "banned",
}


@dataclass(frozen=True)
class XtreamAccount:
    """What `player_api.php` says about the harvested account."""

    status: str = "unknown"          # our vocabulary: online/expired/banned/error/unknown
    status_raw: str = ""             # the server's word, verbatim
    exp_date: str | None = None      # ISO-ish string, from `exp_date` (unix or date)
    created_at: str | None = None
    active_cons: int | None = None
    max_cons: int | None = None
    is_trial: bool = False
    auth: int | None = None
    base: str = ""                   # the media host, when server_info names one
    error: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def known(self) -> bool:
        return not self.error and (self.status != "unknown" or self.exp_date is not None)

    def public(self) -> dict:
        return {"status": self.status, "status_raw": self.status_raw,
                "exp_date": self.exp_date or "", "created_at": self.created_at or "",
                "active_cons": self.active_cons, "max_connections": self.max_cons,
                "is_trial": self.is_trial, "auth": self.auth,
                "base": self.base, "error": self.error}


def _ts(value) -> str | None:
    """Xtream sends unix seconds (`exp_date`: 1767225600 or null). Also accept a
    date string, because panels are panels."""
    s = str(value if value is not None else "").strip()
    if not s or s.lower() in ("null", "none", "0"):
        return None
    if s.isdigit() and len(s) >= 9:
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(int(s), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        except (OverflowError, OSError, ValueError):
            return None
    return s[:40]


def _num(value) -> int | None:
    try:
        s = str(value if value is not None else "").strip()
        return int(s) if s else None
    except (TypeError, ValueError):
        return None


_LIVE_TAIL_RE = re.compile(r"/(?:live|movie|series|vod|streaming(?:/[^/]+)?)/?$", re.I)


def media_base(url: str) -> str:
    """Origin + path prefix of an `http_live_url`, which is a *media root*.

    Panels spell it three ways - `http://h:8000/live`, `http://h:8000/live/`, and
    `http://h/xtream/live` for a panel behind a path prefix - so the rule is "drop
    the trailing kind segment, keep what is left" rather than "take the origin".
    Getting this wrong is not cosmetic: the base decides where `player_api.php` is
    asked and where every adopted channel URL points, so a stripped prefix sends the
    harvested password to a vhost that never issued it.
    """
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    head = _LIVE_TAIL_RE.sub("", raw)
    origin = xtream_base(head)
    if not origin:
        return ""
    return origin + urlsplit(head).path.rstrip("/")


def parse_player_api(payload) -> XtreamAccount:
    """Read `{"server_info":…, "user_info":…}` in whatever spelling it arrived.

    Never raises: an HTML refusal, a `{"user_info": []}` (which is how Xtream
    answers a *wrong* password) and a timeout all land in `.error`/`status=error`
    so the caller can report "no Xtream account here" instead of failing the
    portal check that was only trying to be helpful.
    """
    data = payload
    if isinstance(data, str):
        # a panel answering player_api.php with markup is refusing us, not
        # describing an account; saying so beats parsing "<html" into a status
        low = data[:400].lower()
        if "<html" in low or "<!doctype" in low or "forbidden" in low or "unauthorized" in low:
            return XtreamAccount(status="error", error="not a JSON answer (refused)")
        return XtreamAccount(status="error", error="not a JSON answer")
    if not isinstance(data, dict):
        return XtreamAccount(status="error", error="no answer from player_api.php")
    if isinstance(data.get("errors"), (str, list, dict)):
        err = data["errors"]
        return XtreamAccount(status="error",
                             error=str(err if isinstance(err, str) else "refused")[:120])
    user = data.get("user_info")
    if user is None:
        user = data.get("userInfo") or {}
    server = data.get("server_info")
    if server is None:
        server = data.get("serverInfo") or {}
    # `{"user_info": []}` / `{}`: the classic "these credentials are wrong"
    if not isinstance(user, dict):
        return XtreamAccount(status="error", error="credentials refused by the panel",
                             raw=data if isinstance(data, dict) else {})
    if not user and not server:
        return XtreamAccount(status="error", error="empty answer from player_api.php")

    si = server if isinstance(server, dict) else {}
    raw_status = str(user.get("status") or "").strip()
    auth = _num(user.get("auth"))
    status = _STATUS_MAP.get(raw_status.lower(), "")
    if not status:
        # no status word at all is not "Active": panels omit the field when the
        # account is DNS-locked, and guessing Active would hide an expired line
        status = "unknown" if raw_status else "error"
    live = str(si.get("http_live_url") or si.get("https_live_url") or "").strip().rstrip("/")
    return XtreamAccount(
        status=status, status_raw=raw_status,
        exp_date=_ts(user.get("exp_date") or user.get("end_date")),
        created_at=_ts(user.get("created_at")),
        active_cons=_num(user.get("active_cons")),
        max_cons=_num(user.get("max_connections") or user.get("max_connections_allowed")),
        is_trial=str(user.get("is_trial") or "").lower() in ("1", "true", "yes"),
        auth=auth,
        base=media_base(live),
        error="" if status != "error" else (raw_status or "no status from the panel"),
        raw={"user_info": user, "server_info": si} if len(str(data)) < 2000 else {})


# ---------------------------------------------------------------------------
# stream lists (the catalogue side of the bridge)
# ---------------------------------------------------------------------------
def parse_streams(payload) -> list[dict]:
    """Normalise `action=get_live_streams` / `get_vod_streams` into plain dicts.

    A live stream row is `{num, name, stream_id, category_id, container_extension,
    epg_channel_id, …}` and a VOD row uses `movie_id` instead of `stream_id`; both
    are flattened to `stream_id` here so one matcher serves the two, and the ids
    stay **strings** (some panels use non-numeric ids, and an int cast would turn
    a valid id into a traceback at 3 a.m.).
    """
    rows = payload
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("streams") or []
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        sid = row.get("stream_id")
        if sid in (None, ""):
            sid = row.get("movie_id")
        if sid in (None, ""):
            sid = row.get("series_id")
        if sid in (None, ""):
            continue
        out.append({
            "stream_id": str(sid),
            "name": str(row.get("name") or row.get("title") or "").strip(),
            "num": str(row.get("num") or row.get("channel_no") or "").strip(),
            "category_id": str(row.get("category_id") or ""),
            # `container_extension` is the Xtream spelling, `extension` the one
            # some panels use in get_vod_streams; the second key used to be a copy
            # of the first, which silently dropped the extension for those
            "extension": str(row.get("container_extension") or row.get("extension")
                             or "").lstrip("."),
            "epg_channel_id": str(row.get("epg_channel_id") or row.get("tvg-id") or ""),
        })
    return out


_TITLE_TAGS = re.compile(
    r"\b(?:fhd|uhd|hd|sd|4k|8k|hevc|h ?265|dolby|ddp? ?5[01]|51|multi|backup|alt|"
    r"russ|eng|dut|ger|tur|sub|subtitle|soft ?sub|3d|live|multi-audio)\b", re.I)


def norm_title(name: str) -> str:
    """A light normalizer for matching a panel's name to an Xtream one.

    Deliberately *not* `services.epg.norm_name`: that one is tuned to bridge two
    unrelated naming cultures (logo files vs. guide ids) and is allowed to be
    sloppy, because a wrong EPG match shows the wrong programme. A wrong match
    here hands the player someone else's stream, so this keeps digits and word
    order and only drops the quality/language tags panels bolt onto a name.
    """
    x = str(name or "").lower().strip()
    x = re.sub(r"[\[({][^\])}]*[\])}]", " ", x)          # "(HD)", "[Backup]"
    x = x.replace("|", " ").replace(":", " ").replace("-", " ")
    x = re.sub(r"\bf(?:eb|or)?\b", " ", x)                 # "F | Name" / "FOX Sports" guard
    x = _TITLE_TAGS.sub(" ", x)
    x = re.sub(r"[^a-z0-9]+", " ", x)
    return re.sub(r"\s{2,}", " ", x).strip()


@dataclass(frozen=True)
class AdoptionPlan:
    """What adopting would do, before anyone does it."""

    urls: dict[int, str] = field(default_factory=dict)      # source row id -> stream url
    matched: int = 0
    by_number: int = 0
    by_name: int = 0
    ambiguous: list[str] = field(default_factory=list)      # names matching >1 stream
    unmatched: list[str] = field(default_factory=list)      # our channels with no match

    def public(self, limit: int = 12) -> dict:
        return {"matched": self.matched, "by_number": self.by_number,
                "by_name": self.by_name, "ambiguous": self.ambiguous[:limit],
                "unmatched": self.unmatched[:limit],
                "ambiguous_total": len(self.ambiguous),
                "unmatched_total": len(self.unmatched)}


def plan_adoption(sources: list[dict], streams: list[dict], creds: XtreamCreds,
                  *, kind: str = "live") -> AdoptionPlan:
    """Match our rows to an Xtream stream list, conservatively.

    `sources` items are `{id, number, name}` - the caller reads them off the DB
    rows, so this stays a pure function that a test and `dev/check-links.py` can
    drive without a database. Two rules do the work:

    * **channel number first, then the name**: a panel numbers its own IPTV
      channels and copies those numbers into the Xtream side, and a number
      survives a rename;
    * **never guess**: a name matching more than one stream (the `Sky Sports` /
      `Sky Sports 1` / `Sky Sports HD` family every provider has) is reported as
      ambiguous and left alone. A partially adopted playlist still plays; a
      confidently wrong one shows up as "the proxy switched my channels", which is
      a much worse bug than a missing channel.

    A name collision on the *number* side falls through to the name instead of
    giving up, because two streams sharing `num=101` is a panel data-entry
    accident and our row is often still uniquely identified by its title.
    """
    by_num: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    for st in streams:
        if st.get("num"):
            by_num.setdefault(str(st["num"]).lstrip("0") or "0", []).append(st)
        if st.get("name"):
            by_name.setdefault(norm_title(st["name"]), []).append(st)
    urls: dict[int, str] = {}
    ambiguous: list[str] = []
    unmatched: list[str] = []
    n_num = n_name = 0
    for src in sources:
        label = str(src.get("name") or src.get("id") or "?")
        num = str(src.get("number") or "").strip()
        hit = how = None
        collision = ""
        if num.isdigit():
            cands = by_num.get(num.lstrip("0") or "0", [])
            if len(cands) == 1:
                hit, how = cands[0], "number"
            elif len(cands) > 1:
                # not fatal: two rows sharing a number is a panel data-entry
                # accident, so fall through to the title and keep the reason
                collision = f"{len(cands)} streams share channel number {num}"
        if hit is None:
            cands = by_name.get(norm_title(label), [])
            if len(cands) == 1:
                hit, how = cands[0], "name"
            elif len(cands) > 1:
                collision = collision or f"{len(cands)} streams share that name"
        if hit is None:
            # `ambiguous` is the part of what we did not adopt that we *could* have
            # matched, and it is reported as the reason rather than as a second list:
            # a GUI line reading "12 matched, 1 unmatched, 1 ambiguous" invites the
            # user to add numbers that do not add up
            if collision:
                ambiguous.append(f"{label} ({collision})")
            else:
                unmatched.append(label)
            continue
        if how == "number":
            n_num += 1
        else:
            n_name += 1
        urls[src["id"]] = creds.stream_url(hit["stream_id"], kind, hit.get("extension") or "")
    return AdoptionPlan(urls=urls, matched=len(urls), by_number=n_num, by_name=n_name,
                        ambiguous=ambiguous, unmatched=unmatched)
