"""Explicit per-type user group selections; empty always means no visibility."""
from sqlalchemy import select

from ..models import LivePlaylist, LocalPlaylist, SeriePlaylist, VodPlaylist

GROUP_MODELS = {'live': LivePlaylist, 'vod': VodPlaylist,
                'series': SeriePlaylist, 'local': LocalPlaylist}
DEFAULT_GROUP_NAMES = {'live': 'Live', 'vod': 'VOD', 'series': 'Series', 'local': 'Local files'}


def group_name(kind: str, value: str | None) -> str:
    """Named option for blank/ungrouped entries, shared by editor and outputs."""
    return (value or '').strip() or DEFAULT_GROUP_NAMES[kind]


def clean_groups(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        value = {}
    out = {}
    for kind in GROUP_MODELS:
        vals = value.get(kind) or []
        if isinstance(vals, str):
            vals = [vals]
        if not isinstance(vals, list):
            vals = []
        seen = set()
        out[kind] = []
        for v in vals:
            if not isinstance(v, str):
                continue
            v = v.strip()
            if v and v.lower() not in seen:
                seen.add(v.lower())
                out[kind].append(v)
    return out


async def available_groups(db) -> dict[str, list[str]]:
    """All currently configured playlist groups, including disabled entries."""
    out = {}
    for kind, model in GROUP_MODELS.items():
        names = (await db.scalars(select(model.group_name).distinct())).all()
        unique = {}
        for name in names:
            display = group_name(kind, name)
            unique.setdefault(display.lower(), display)
        out[kind] = sorted(unique.values(), key=str.casefold)
    return out
