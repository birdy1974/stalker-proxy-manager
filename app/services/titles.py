"""Pick a real display title from the messy names panels send.

Some VOD rows arrive with `name` = just the year (\"2026\") and the actual
title in `o_name` (\"**Man of War - 2026\"). Using `name` first then made the
playlist and the M3U show only \"2026\".
"""

from __future__ import annotations

import re

_YEAR_ONLY = re.compile(r"^(?:19|20)\d{2}$")


def best_title(*names: str | None, limit: int = 400) -> str:
    """Return the most complete title among the candidates.

    Year-only strings lose to anything with letters. Among remaining names
    the longest wins (portal `o_name` is often the full labelled title).
    """
    cands = []
    for n in names:
        s = str(n or "").strip()
        if s and s != "?":
            cands.append(s)
    if not cands:
        return "?"
    lettered = [s for s in cands if re.search(r"[A-Za-z]", s) and not _YEAR_ONLY.match(s)]
    pool = lettered or [s for s in cands if not _YEAR_ONLY.match(s)] or cands
    return max(pool, key=len)[:limit]


def portal_item_title(item: dict, limit: int = 400) -> str:
    """Title for a Stalker VOD/series list row."""
    return best_title(
        item.get("o_name"),
        item.get("orig_name"),
        item.get("name"),
        item.get("title"),
        item.get("fname"),
        limit=limit,
    )


def m3u_attr(value: str | None) -> str:
    """Safe double-quoted M3U attribute (quotes/newlines would truncate titles)."""
    return (value or "").replace("\\", "\\\\").replace('"', "'").replace("\n", " ").replace("\r", "")


# VLC 3.0 `parseEXTINF` (modules/demux/playlist/m3u.c) treats the display
# name after the EXTINF comma as `Artist - Title` whenever it sees this
# exact three-character ASCII sequence. A VOD named "Man of War - 2026"
# then shows up in the playlist as just "2026". An en dash is not that
# splitter, and looks the same in the playlist UI.
_VLC_ARTIST_SEP = " - "
_VLC_SAFE_DASH = " \u2013 "  # EN DASH (U+2013)


def m3u_display_title(title: str | None) -> str:
    """Title for `#EXTINF:…,title` and `tvg-name`, safe for VLC.

    Single physical line (a newline would orphan the URL). ASCII
    space-hyphen-space is rewritten so VLC 3.0 does not split the name
    into artist / title.
    """
    s = (title or "").replace("\r", " ").replace("\n", " ").replace("\0", "")
    return s.replace(_VLC_ARTIST_SEP, _VLC_SAFE_DASH)
