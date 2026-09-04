"""
Resolve which FFmpeg template a playlist item actually plays with.

The playlist row carries a default template. An optional *area* (a named
playback profile attached to a User) overlays:

  1. a sparse per-item exception (area_item_templates)
  2. a per-kind default on the area (live / vod / series / local)
  3. the playlist item's ffmpeg_template_id
  4. the global default template

Used by the stream manager (including redirect vs proxy), M3U/Xtream URL
extensions, and Enigma2 bouquet container/player choice. One TemplateMap per
request keeps it to a handful of queries.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from ..models import Area, AreaItemTemplate, FFmpegTemplate, User
from .ffmpeg_templates import REDIRECT_COMMAND, URL_PLACEHOLDER

AREA_KINDS = ("live", "vod", "series", "local")
KIND_DEFAULT_COL = {
    "live": "ffmpeg_template_live_id",
    "vod": "ffmpeg_template_vod_id",
    "series": "ffmpeg_template_series_id",
    "local": "ffmpeg_template_local_id",
}
_PASS_CMD = f"ffmpeg -i {URL_PLACEHOLDER} -c copy -f mpegts pipe:1"


def area_kind(kind: str) -> str:
    """Map a stream kind (episode) onto an area kind (series)."""
    return "series" if kind == "episode" else kind


def container_for(command: str | None, output_format: str | None) -> str:
    """URL alias the player should request: ts or mkv."""
    if (command or "").strip() == REDIRECT_COMMAND:
        return "ts"
    if (output_format or "") == "matroska":
        return "mkv"
    return "ts"


@dataclass
class ResolvedTemplate:
    id: int | None
    name: str
    command: str
    output_format: str

    @property
    def container(self) -> str:
        return container_for(self.command, self.output_format)

    @property
    def is_redirect(self) -> bool:
        return (self.command or "").strip() == REDIRECT_COMMAND


class TemplateMap:
    """In-memory overlay: area exceptions + kind defaults + catalog + global default."""

    def __init__(self, templates: list[FFmpegTemplate], area: Area | None,
                 overrides: dict[tuple[str, int], int]):
        self.by_id = {t.id: t for t in templates}
        enabled = [t for t in templates if t.enabled]
        self.default = next((t for t in enabled if t.is_default), None)
        self.any = enabled[0] if enabled else None
        self.area = area if (area is not None and area.enabled) else None
        self.overrides = overrides if self.area is not None else {}

    def _tpl(self, tid: int | None) -> FFmpegTemplate | None:
        if not tid:
            return None
        t = self.by_id.get(int(tid))
        if t is None or not t.enabled:
            return None
        return t

    def resolve(self, kind: str, item) -> ResolvedTemplate:
        kind = area_kind(kind)
        item_id = getattr(item, "id", None) if item is not None else None
        chosen = None
        if self.area is not None and item_id is not None and kind in KIND_DEFAULT_COL:
            chosen = self._tpl(self.overrides.get((kind, int(item_id))))
            if chosen is None:
                chosen = self._tpl(getattr(self.area, KIND_DEFAULT_COL[kind], None))
        if chosen is None and item is not None:
            chosen = self._tpl(getattr(item, "ffmpeg_template_id", None))
        if chosen is None:
            chosen = self.default or self.any
        if chosen is None:
            return ResolvedTemplate(None, "(pass)", _PASS_CMD, "mpegts")
        cmd = chosen.command or _PASS_CMD
        return ResolvedTemplate(chosen.id, chosen.name, cmd,
                                chosen.output_format or "mpegts")


async def template_map_for(session, user: User | None) -> TemplateMap:
    templates = (await session.execute(select(FFmpegTemplate))).scalars().all()
    area = None
    overrides: dict[tuple[str, int], int] = {}
    area_id = getattr(user, "area_id", None) if user is not None else None
    if area_id:
        area = await session.get(Area, area_id)
        if area is not None:
            for row in (await session.execute(select(AreaItemTemplate).where(
                    AreaItemTemplate.area_id == area.id))).scalars().all():
                overrides[(row.kind, row.playlist_id)] = row.ffmpeg_template_id
    return TemplateMap(list(templates), area, overrides)


async def template_map_for_username(session, user_name: str | None) -> TemplateMap:
    user = None
    if user_name:
        user = (await session.execute(select(User).where(
            User.name == user_name))).scalar_one_or_none()
    return await template_map_for(session, user)
