"""
When a stored `cmd` is already the link, and when the panel has to be asked
(R2).

EStalker plays a channel without touching `create_link` at all unless the panel
told it the link is temporary. That is worth having: on the redirect path a
channel change costs one portal round trip, and `create_link` is itself a
failure mode (a panel that is up for everything else can still answer it with
`nothing_to_play`, a blanked `&stream=` or a 500).

But we are not a set-top box, and one difference decides the whole design: a
box fetches the catalogue and plays it minutes later, while our proxy replays a
`cmd` that may have been stored weeks ago, on a session that is long gone, for a
MAC that may not even be the one that fetched it. So the rule below is stricter
than theirs in exactly one respect - **a stored link that carries a session
token is never played as-is** - and looser in another: the fast path is only
taken where the player is sent straight to the panel (no ffmpeg), because the
transcode path *wants* `create_link` for two reasons that have nothing to do with
the URL: the fresh bearer, and the fact that a refusal there is our liveness
answer for the fallback chain.

Silence is not information, again: a channel row written before this existed has
no flags, and "no flags" must not read as "the panel said the link is
permanent". EStalker can afford a shape heuristic for those rows because a box
notices a dead stream and falls back; our *redirect* mode sends the player away
and never learns anything, so an unflagged row keeps asking until a fetch has
stored the panel's own answer. The URL-shape rules are therefore a second guard
on rows the panel *did* describe, not a substitute for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

# The per-channel flags a Stalker panel puts next to each item. They mean:
#   use_http_tmp_link  - the play URL is only valid with a token minted now
#   use_load_balancing - the CDN host is decided per request, by the panel
#   disable_ad         - the panel wants an ad inserted; it is NOT a reason to
#                        skip create_link, it is a parameter to send *to* it
#                        (the plan listed all three as gates; the source of
#                        truth, a panel's own behaviour, says otherwise)
FLAG_TMP_LINK = "use_http_tmp_link"
FLAG_LOAD_BALANCING = "use_load_balancing"
FLAG_DISABLE_AD = "disable_ad"
LINK_FLAGS = (FLAG_TMP_LINK, FLAG_LOAD_BALANCING, FLAG_DISABLE_AD)

#: flags that make a stored link unusable until the panel rebuilds it
REBUILD_FLAGS = (FLAG_TMP_LINK, FLAG_LOAD_BALANCING)

# VOLATILE_PARAMS (defined with the link helpers above) doubles as the policy's
# "this URL is session-bound" test, deliberately: `strip_volatile` removes those
# same keys before *asking*, so one list describes both sides of the trick.

#: markers that a `cmd` is a *template* the panel must finish, not a URL.
# `%mac%` is deliberately absent: we substitute that ourselves (R4), so it is
# not a reason to ask. `/ch/` is, because a path like that is built per request
# by the panel and a stale one fails without a fallback chain behind it.
UNSAFE_MARKERS = ("localhost", "127.0.0.1", "///", "/ch/")

_TRUE = ("1", "true", "yes", "on")
_FLAG_SPLIT = re.compile(r"[,\s;]+")


VOLATILE_PARAMS = frozenset({
    "play_token", "token", "tok", "auth", "auth_key", "authkey", "key",
    "signature", "sig", "sign", "session", "sess", "st", "e", "exp",
    "expires", "expire", "md5", "hash", "usertoken", "user_token",
})
URL_SCHEMES = ("http://", "https://", "rtsp://", "rtsps://", "rtmp://",
               "rtmps://", "udp://", "rtp://", "mms://")


def extract_url(raw: Any) -> str:
    """
    Pick the stream URL out of a portal cmd.

    Portals are creative here: 'ffmpeg http://…', 'ffrt http://…', the bare
    URL, the whole cmd percent-encoded, or the URL followed by extra ffmpeg
    arguments. Returns '' when no URL is recognisable.
    """
    text = str(raw or "").strip().strip("\"'")
    if not text:
        return ""
    # some panels return the cmd fully percent-encoded ('ffmpeg http%3A%2F%2F…')
    if "://" not in text and "%3a%2f%2f" in text.lower():
        text = unquote(text)
    for tok in text.split():
        candidate = tok.strip("\"'")
        if candidate.lower().startswith(URL_SCHEMES):
            return candidate
    return ""


# Panels that keep ONE link template for every box do not substitute the MAC
# themselves: they hand out 'http://cdn/ch/%mac%/1234.ts' and expect the
# set-top box - the only party that knows its own MAC - to fill it in. Handing
# that URL to ffmpeg verbatim produces a 404 that looks like a dead channel.
# The double-encoded variants appear when a placeholder travels through a
# query-string round trip (parse_qsl + urlencode in merge_link).
MAC_PLACEHOLDER = re.compile(r"%(?:25)?mac%(?:25)?", re.I)


def apply_mac_placeholder(url: str, mac: str) -> str:
    """Fill every `%mac%` (any casing, any encoding) in a resolved link."""
    if not url or "%" not in url or not mac:
        return url
    return MAC_PLACEHOLDER.sub(mac, url)


def truthy(value) -> bool:
    return str(value if value is not None else "").strip().lower() in _TRUE


def parse_link_flags(item) -> str | None:
    """The set flags of a portal catalogue row, as a comma string (None = unknown).

    Stored as a string rather than one boolean column per flag for two reasons:
    the stream path only ever asks "is any of these set?", and a row in the
    sources API or in a backup then *names* the reason instead of showing a bit
    nobody can interpret. `-1`/NULL/missing in the payload means the panel said nothing,
    which is kept distinct from an empty string ("it said: nothing applies").
    """
    if not isinstance(item, dict):
        return None
    seen = {k.lower(): v for k, v in item.items() if isinstance(k, str)}
    if not any(flag in seen for flag in LINK_FLAGS):
        return None                       # never told: unknown, not "no"
    told = [seen[f] for f in LINK_FLAGS if f in seen]
    if any(str(v).strip() == "-1" for v in told):
        # Some panels answer -1 for "this channel does not use tmp links at
        # all". It is not a "no" we may act on and it is not a "yes"; a client
        # that read it as truthy (as PHP would) rebuilds links forever, and one
        # that read it as "no" plays a link the panel may still rotate. Ask.
        return None
    set_flags = [flag for flag in LINK_FLAGS if truthy(seen.get(flag))]
    return ",".join(set_flags)


def split_flags(link_flags: str | None) -> list[str]:
    text = (link_flags or "").strip()
    if not text:
        return []
    # quotes are tolerated because this column is read by humans: a hand-edited
    # backup ("use_http_tmp_link", ...) must not silently mean "some flag nobody
    # recognises", which is the difference between asking and not asking.
    return [f for f in (x.strip().strip('\"\'') for x in _FLAG_SPLIT.split(text.lower())) if f]


def has_flag(link_flags: str | None, flag: str) -> bool:
    return flag in split_flags(link_flags)


def flags_known(link_flags: str | None) -> bool:
    return link_flags is not None


@dataclass(frozen=True)
class LinkPolicy:
    """The decision, with the sentence that explains it.

    `reason` is not decoration: it is what the stream log shows, and "we skipped
    create_link" without "because the channel says its link is permanent" is a
    mystery instead of a diagnosis.
    """

    create_link: bool
    reason: str
    flags_known: bool = True

    @property
    def direct(self) -> bool:
        return not self.create_link


def why_not_self_served(url: str) -> str:
    """Why this URL must not be handed to a player as it stands ("" = it may).

    The answer is a sentence rather than a bool because it goes into the stream
    log: "asked the panel for a link" is only useful if it says *why* the stored
    one was not good enough.
    """
    if not url:
        return "no URL in the stored cmd to play directly"
    parts = urlsplit(url)
    # the template check comes first on purpose: `/ch/101.ts` is both "not
    # absolute" and "a template", and only the second tells the reader that the
    # panel has to finish this cmd - which is the fact that decides the policy.
    marker = next((m for m in UNSAFE_MARKERS if m in url.lower()), "")
    if marker:
        return f"the stored URL is a template the panel finishes (contains {marker!r})"
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return "the stored cmd is not an absolute http(s) URL"
    # A token from the session that fetched the catalogue is worse than no
    # token: it is expired, and some panels answer an expired one with a refusal
    # instead of falling back to the unauthenticated rules. We cannot tell
    # whether it still works, and a 302 to a dead link is a black screen with no
    # fallback chain behind it - so ask, and get a token that does.
    stale = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)
             if name.lower() in VOLATILE_PARAMS]
    if stale:
        return f"the stored URL carries a session token from fetch time ({stale[0]})"
    return ""


def link_policy(*, url: str, link_flags: str | None = None,
                force_ch_link_check: bool = False, ffmpeg: bool = False,
                allow_direct: bool = True) -> LinkPolicy:
    """Ask the panel for a link, or play the stored one? See this module's header.

    `url` is what the caller would hand to the player (the `cmd` already run
    through `extract_url`), so the decision is testable without a client, a
    portal, or a database - and the *reason* is part of the answer, because it
    belongs in the stream log.
    """
    known = flags_known(link_flags)
    if ffmpeg:
        return LinkPolicy(True, "ffmpeg owns this stream: the request gives us a fresh "
                                "token *and* the liveness answer the fallback chain needs",
                          known)
    if force_ch_link_check:
        return LinkPolicy(True, "the panel set force_ch_link_check for this MAC", known)
    if not allow_direct:
        return LinkPolicy(True, "this portal is set to ask for every link (its flags are "
                                "not trusted)", known)
    if not known:
        return LinkPolicy(True, "this row has no link flags stored (it predates them) - "
                                "a fetch fills them in", False)
    if not url:
        return LinkPolicy(True, "no URL in the stored cmd to play directly", True)
    blocking = [f for f in REBUILD_FLAGS if has_flag(link_flags, f)]
    if blocking:
        return LinkPolicy(True, f"the channel says its link is not permanent "
                                f"({', '.join(blocking)})", True)
    blocker = why_not_self_served(url)
    if blocker:
        return LinkPolicy(True, f"asking the panel for a link: {blocker}", True)
    return LinkPolicy(False, "playing the stored link: the channel flags say nothing needs "
                             "rebuilding and the URL needs no token", True)


@dataclass(frozen=True)
class LinkPlan:
    """One source row + one MAC row, read the same way by every stream path.

    The attribute names live here rather than at each call site on purpose: the
    redirect path and the ffmpeg path used to read `src.cmd` in their own way,
    which is how a rule stated once in a ticket turns into two behaviours.
    """

    policy: LinkPolicy
    cmd: str
    url: str
    link_flags: str | None
    force_ch_link_check: bool
    mac: str = ""
    #: an adopted Xtream source (R7): the URL replaces the portal's answer rather
    #: than being the portal's answer, so the stream path must not occupy a MAC
    #: slot and must not open a portal session at all
    adopted: bool = False
    #: classic-Stalker episode selector: the row's cmd addresses the SEASON, and
    #: the panel picks the episode server-side via `series=<n>` at create_link
    #: time. None for everything that is not such an episode.
    series: int | None = None

    @property
    def direct_url(self) -> str:
        """The URL to play as-is (`%mac%` filled in, the only thing we may add)."""
        return apply_mac_placeholder(self.url, self.mac)

    def request_kwargs(self) -> dict:
        out = {"link_flags": self.link_flags,
               "force_ch_link_check": self.force_ch_link_check}
        if self.series is not None:
            out["series"] = self.series
        return out


def plan_for(src, mac_row, *, ffmpeg: bool = False,
             allow_direct: bool = True) -> LinkPlan:
    """The single place that reads a source row and a MAC row for a stream open."""
    cmd = str(getattr(src, "cmd", "") or "")
    url = extract_url(cmd)
    link_flags = getattr(src, "link_flags", None)
    force = bool(getattr(mac_row, "force_ch_link_check", False))
    # A classic-Stalker episode has no static answer AT ALL: its stored cmd
    # addresses the season container, and only create_link with `series=<n>`
    # yields the episode's own URL. Direct play of the stored cmd would play
    # the container (usually episode 1) for every episode of the season.
    series = None
    if bool(getattr(src, "series_param", False)):
        ep_num = getattr(src, "episode_number", None)
        if ep_num is not None:
            series = int(ep_num)
    policy = link_policy(url=url, link_flags=link_flags, force_ch_link_check=force,
                         ffmpeg=ffmpeg, allow_direct=allow_direct and series is None)
    return LinkPlan(policy=policy, cmd=cmd, url=url, link_flags=link_flags,
                    force_ch_link_check=force, mac=str(getattr(mac_row, "mac", "") or ""),
                    series=series)


# ---------------------------------------------------------------------------
# create_link parameters that a faithful box sends and we used to hardcode
# ---------------------------------------------------------------------------
#: the sentence the stream log shows for an adopted channel - it has to explain
#: the *exception* to "ffmpeg always asks", so it names who decided (the user, via
#: the portal's Xtream adoption) and what is no longer in the loop
ADOPTED_REASON = ("playing the harvested Xtream link: this portal builds its stream URLs "
                  "from an Xtream account and the user adopted it, so there is no "
                  "create_link left to call and no MAC to occupy")


def plan_adopted(url: str, *, src=None, mac_row=None) -> LinkPlan:
    """The plan for a channel whose stream lives on the Xtream side of the panel.

    The one answer that outranks every rule above it, including
    "`ffmpeg` always asks": there is no portal link to rebuild for this source -
    the harvested URL *is* the stream, and asking the MAC portal for a
    `create_link` of an Xtream path would either fail or return the very link we
    are replacing. The transcode path still gets its liveness answer, from ffmpeg
    failing to open the URL rather than from the panel refusing to build one.
    """
    return LinkPlan(policy=LinkPolicy(False, ADOPTED_REASON, True),
                    cmd=str(getattr(src, "cmd", "") or ""), url=str(url or ""),
                    link_flags=None, force_ch_link_check=False,
                    mac=str(getattr(mac_row, "mac", "") or ""), adopted=True)


def link_request_params(*, link_flags: str | None, force_ch_link_check: bool,
                        series: bool | int | str | None = False) -> dict:
    """The `create_link` query parameters the channel's own flags imply.

    Sending `disable_ad=false` for a channel whose panel wants an ad inserted is
    how a proxy ends up serving an unskippable advertisement inside a "clean"
    stream, and `force_ch_link_check=false` on a panel that set the flag
    contradicts the answer we already stored from `get_profile`.

    `series` carries the classic-Stalker episode selector: on panels where a
    season is ONE cmd with an episode-number list, `series=<n>` is how the panel
    knows which episode to link (IPTVnator sends exactly this). A bare True/False
    keeps the faithful-box default of "1"/"0".
    """
    if series is None or series is False:
        series_val = "0"
    elif series is True:
        series_val = "1"
    else:
        series_val = str(series)
    return {
        "series": series_val,
        "forced_storage": "false",
        "disable_ad": "true" if has_flag(link_flags, FLAG_DISABLE_AD) else "false",
        "download": "false",
        "force_ch_link_check": "true" if force_ch_link_check else "false",
    }
