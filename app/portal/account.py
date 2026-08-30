"""
What the portal says about the *account*, and what we do about it.

Two portal calls carry account truth, and until now we read almost none of it:

  * ``type=stb&action=get_profile``       -> ``id``, ``play_token``, ``status``,
    ``blocked``, ``force_ch_link_check``, and the echoed ``mac``.
  * ``type=account_info&action=get_main_info`` -> ``phone`` / ``end_date``
    (the expiry, in the field names real panels happen to use).

A panel that answers those is telling us the MAC is banned, or the subscription
ended, or every link has to be re-validated. Marking such a MAC ``online``
because the handshake worked - which is what ``test_portal`` used to do - is how
an expired account stays in every fallback chain forever, burning a portal
connection slot per attempt and ending in a black channel for the user.

This module is the decision function only; the HTTP lives in client.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as _tz
import re
from typing import Any

#: statuses that mean "do not open a stream through this MAC"
#:
#: Deliberately narrow. `offline` / `error` are *our* verdicts about transport
#: and are often transient (a portal that timed out once), so keeping them out
#: of rotation would take working sources down. `banned` and `expired` are
#: different in kind: they are statements from the panel about the account, and
#: no retry will change them - only paying the portal or an admin re-check will.
UNUSABLE_MAC_STATUSES = frozenset({"banned", "expired"})

#: `dd.mm.yyyy` / `dd/mm/yyyy` - the European order a billing department types
#: into a portal admin box, which `fromisoformat` will not read.
_EU_DATE = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{4})"
                      r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?")

def _flag(value: Any) -> bool:
    """Panel truthiness: 1 / "1" / True / "yes" / "blocked" are on.

    `0`, `"0"`, None, "" and False are off - which matters more than it sounds,
    because most panels send `"blocked": "0"` as a *string* and `bool("0")` is
    True.
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    return text not in ("", "0", "0.0", "false", "none", "null", "no", "off", "ok")


def parse_expiry(raw: Any) -> datetime | None:
    """An expiry from the panel as an aware UTC datetime, None if unusable.

    Returns None (not a guess) for anything we cannot read - "Unknown", an empty
    string, a date that does not parse. `None` means "no verdict", and the
    caller must treat that as *do not disable the MAC*.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=_tz.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text or text.lower() in ("unknown", "n/a", "none", "null", "0", "-"):
        return None
    if re.fullmatch(r"\d{9,12}", text):                    # "1893456000"
        try:
            return datetime.fromtimestamp(int(text), tz=_tz.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:                                  # 2032-12-31, ...T00:00:00, +03:00, .123456
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        m = _EU_DATE.match(text)
        if not m:
            return None
        day, month, year, hh, mm, ss = m.groups()
        try:
            dt = datetime(int(year), int(month), int(day), int(hh or 0), int(mm or 0),
                          int(ss or 0))
        except ValueError:                    # 31.02.2032 and friends
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=_tz.utc)
    return dt.astimezone(_tz.utc)


@dataclass(frozen=True)
class AccountVerdict:
    """What the portal's own answers say about one MAC."""

    status: str                       # active | expired | banned | no_token | unknown
    online: bool
    reason: str                       # one line for the log and the GUI
    expire_date: str | None = None    # verbatim from the panel (as we store it)
    expires_at: datetime | None = None
    force_ch_link_check: bool = False
    play_token: str = ""
    panel_status: str = ""

    @property
    def usable(self) -> bool:
        return self.status not in UNUSABLE_MAC_STATUSES

    @property
    def days_left(self) -> int | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now(_tz.utc)).days


def account_verdict(*, profile: Any = None, info: Any = None,
                    token: str | None = None, now: datetime | None = None,
                    near_expiry_days: int = 0) -> AccountVerdict:
    """
    Turn the two account payloads into a decision.

    `profile` is the `js` of get_profile, `info` the `js` of account_info; either
    may be missing (a panel that 404s get_profile is common and must not be
    punished). Order of severity is deliberate: a banned MAC stays banned no
    matter what the expiry says, and *no token at all* outranks nothing, because
    without a bearer there is no source to blame.
    """
    prof = profile if isinstance(profile, dict) else {}
    inf = info if isinstance(info, dict) else {}
    raw_expiry = inf.get("phone") or inf.get("end_date") or prof.get("expire_date") \
        or prof.get("expired") or ""
    expires_at = parse_expiry(raw_expiry)
    force_check = _flag(prof.get("force_ch_link_check") or inf.get("force_ch_link_check"))
    panel_status = str(prof.get("status") if prof.get("status") is not None else "").strip()
    play_token = str(prof.get("play_token") or "")
    expire_str = str(raw_expiry).strip() or None

    common = dict(expire_date=expire_str, expires_at=expires_at,
                  force_ch_link_check=force_check, play_token=play_token,
                  panel_status=panel_status)

    if _flag(prof.get("blocked")) or _flag(inf.get("blocked")):
        return AccountVerdict(status="banned", online=False,
                              reason="blocked by panel (MAC disabled on the portal)", **common)
    if not str(token or "").strip():
        return AccountVerdict(status="no_token", online=False,
                              reason="no token issued (MAC unknown or IP blocked?)", **common)
    if expires_at is not None:
        ref = now or datetime.now(_tz.utc)
        if expires_at <= ref:
            return AccountVerdict(status="expired", online=False,
                                  reason=f"expired {expires_at.date().isoformat()}", **common)
        left = (expires_at - ref).days
        if near_expiry_days and left <= near_expiry_days:
            return AccountVerdict(status="active", online=True,
                                  reason=f"expires in {left} day(s) ({expire_str})", **common)
        return AccountVerdict(status="active", online=True,
                              reason=f"active until {expires_at.date().isoformat()}", **common)
    # An unreadable expiry is not an expired account: report it, keep using it.
    return AccountVerdict(status="active", online=True,
                          reason=f"active (expiry: {expire_str or 'not reported'})", **common)


#: verdict status -> MacAddress.status (the column's own vocabulary)
MAC_STATUS = {"active": "online", "expired": "expired", "banned": "banned",
              "no_token": "unauthorized", "unknown": "error"}


def mac_status(verdict: AccountVerdict) -> str:
    """A verdict as the status string the MAC table and the GUI speak."""
    return MAC_STATUS.get(verdict.status, verdict.status)


def mac_is_usable(status: Any) -> bool:
    """Chain-building predicate: may this MAC be opened at all?

    None/empty/unknown statuses are usable - a MAC that was never checked must
    still be tried, or a fresh install streams nothing.
    """
    return str(status or "").strip().lower() not in UNUSABLE_MAC_STATUSES


def expiry_warning(verdict: AccountVerdict, *, warn_days: int = 7) -> str:
    """"subscription ends in N days" text, '' when there is nothing to warn about.

    Surfacing this *before* the account dies is the difference between a user
    renewing on time and a support thread about a portal that "stopped working".
    """
    if verdict.expires_at is None:
        return ""
    left = (verdict.expires_at - datetime.now(_tz.utc)).days
    if left < 0:
        return f"expired {-left} day(s) ago"
    if left <= warn_days:
        return f"expires in {left} day(s)"
    return ""
