"""
Output generation: per-user M3U playlist + the data views behind the Xtream
Codes API subset.

User model (Phase-1 decision): admins create users with a name/password pair.
Each user has m3u_enabled / xtream_enabled, an optional expiry, a
max_connections limit and a per-type group whitelist:
    {"live": ["News"], "vod": ["Action"], "series": [], "local": []}
An EMPTY list for a type means "all groups of that type allowed".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import quote

from sqlalchemy import select

from ..database import SessionLocal
from ..models import (
    LivePlaylist, LocalFile, LocalPlaylist, LocalSource, SerieEpisode,
    SeriePlaylist, SeriePlaylistSeason, SerieSeason, SerieSource, User,
    VodPlaylist, Setting,
)


class UserAuth:
    """Validates ?u=&p= credentials and applies expiry/enabled rules."""

    @staticmethod
    async def verify(username: str | None, password: str | None,
                     need: str = "m3u") -> User | None:
        if not username or password is None:
            return None
        async with SessionLocal() as s:
            user = (await s.execute(select(User).where(User.name == username))).scalar_one_or_none()
            if not user or not user.enabled or user.password != password:
                return None
            if need == "m3u" and not user.m3u_enabled:
                return None
            if need == "xtream" and not user.xtream_enabled:
                return None
            if user.expire_date:
                try:
                    exp = datetime.fromisoformat(user.expire_date.replace("Z", "+00:00"))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > exp:
                        return None
                except ValueError:
                    pass
            # touch last_active cheaply (best effort)
            user.last_active = datetime.now(timezone.utc)
            await s.commit()
            return user


def _groups(user: User) -> dict:
    try:
        g = json.loads(user.groups_json or "{}")
        if isinstance(g, dict):
            return {"live": list(g.get("live", [])), "vod": list(g.get("vod", [])),
                    "series": list(g.get("series", [])), "local": list(g.get("local", []))}
    except (json.JSONDecodeError, TypeError):
        pass
    return {"live": [], "vod": [], "series": [], "local": []}


def _allowed(group_name: str | None, whitelist: list[str]) -> bool:
    if not whitelist:
        return True
    return (group_name or "").lower() in [w.lower() for w in whitelist]   # case-insensitive


async def get_setting(key: str, default=None):
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row is None or row.value is None:
            return default
        try:
            return json.loads(row.value)
        except json.JSONDecodeError:
            return default


async def build_m3u(base_url: str, user: User) -> str:
    """Render the final per-user playlist (groups filtered per user)."""
    groups = _groups(user)
    u, p = quote(user.name), quote(user.password)
    lines = [f'#EXTM3U url-tvg="{base_url}/epg.xml?u={u}&p={p}" x-tvg-url="{base_url}/epg.xml?u={u}&p={p}"']

    async with SessionLocal() as s:
        # ---- live ------------------------------------------------------
        items = (await s.execute(select(LivePlaylist).where(LivePlaylist.enabled.is_(True))
                                 .order_by(LivePlaylist.order, LivePlaylist.id))).scalars().all()
        for it in items:
            if not _allowed(it.group_name, groups["live"]):
                continue
            attrs = {
                "tvg-chno": it.number if it.number is not None else it.order,
                "tvg-id": it.epg_id or "", "tvg-name": it.custom_name,
                "tvg-logo": it.logo or "", "group-title": it.group_name or "Live",
            }
            attr = " ".join(f'{k}="{v}"' for k, v in attrs.items())
            lines.append(f"#EXTINF:-1 {attr},{it.custom_name}")
            lines.append(f"{base_url}/play/live/{it.id}.ts?u={u}&p={p}")

        # ---- vod -------------------------------------------------------
        vods = (await s.execute(select(VodPlaylist).where(VodPlaylist.enabled.is_(True))
                                .order_by(VodPlaylist.order, VodPlaylist.id))).scalars().all()
        for it in vods:
            if not _allowed(it.group_name, groups["vod"]):
                continue
            attr = (f'tvg-logo="{it.poster or it.logo or ""}" '
                    f'group-title="{it.group_name or "VOD"}"')
            lines.append(f"#EXTINF:-1 {attr},{it.custom_name}")
            lines.append(f"{base_url}/play/vod/{it.id}.ts?u={u}&p={p}")

        # ---- series: one m3u entry per episode of enabled seasons ------
        series = (await s.execute(select(SeriePlaylist).where(SeriePlaylist.enabled.is_(True))
                                  .order_by(SeriePlaylist.order, SeriePlaylist.id))).scalars().all()
        for sp in series:
            if not _allowed(sp.group_name, groups["series"]):
                continue
            season_rows = (await s.execute(
                select(SeriePlaylistSeason, SerieSeason)
                .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
                .where(SeriePlaylistSeason.serie_playlist_id == sp.id,
                       SeriePlaylistSeason.enabled.is_(True)))).all()
            for _, season in season_rows:
                eps = (await s.execute(select(SerieEpisode).where(
                    SerieEpisode.serie_season_id == season.id)
                    .order_by(SerieEpisode.episode_number))).scalars().all()
                for ep in eps:
                    name = f"{sp.custom_name} S{season.season_number:02d}E{ep.episode_number:02d}"
                    attr = (f'tvg-logo="{sp.poster or sp.logo or ""}" '
                            f'group-title="Series: {sp.group_name or sp.custom_name}"')
                    lines.append(f"#EXTINF:-1 {attr},{name}")
                    lines.append(f"{base_url}/play/episode/{ep.id}.ts?u={u}&p={p}")

        # ---- local files ------------------------------------------------
        locals_ = (await s.execute(select(LocalPlaylist).where(LocalPlaylist.enabled.is_(True))
                                   .order_by(LocalPlaylist.order, LocalPlaylist.id))).scalars().all()
        for it in locals_:
            if not _allowed(it.group_name, groups["local"]):
                continue
            lf = await s.get(LocalFile, it.local_file_id)
            if not lf:
                continue
            name = it.custom_name or lf.filename
            lines.append(f'#EXTINF:-1 group-title="{it.group_name or "vod-local"}",{name}')
            lines.append(f"{base_url}/play/local/{it.id}.ts?u={u}&p={p}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Xtream Codes API data views (player_api.php)
# ---------------------------------------------------------------------------
async def xtream_base(user: User, base_url: str) -> dict:
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "user_info": {
            "username": user.name, "password": user.password, "message": "",
            "auth": 1, "status": "Active",
            "exp_date": str(int(datetime.fromisoformat(user.expire_date).timestamp()))
            if user.expire_date else None,
            "is_trial": "0", "active_cons": "0", "created_at": str(now),
            "max_connections": str(user.max_connections),
            "allowed_output_formats": ["ts", "m3u8"],
        },
        "server_info": {
            "url": base_url.split("://", 1)[-1].split(":")[0],
            "port": base_url.split(":")[-1].split("/")[0],
            "https": False, "server_protocol": base_url.split("://", 1)[0],
            "rtmp_port": "0", "timestamp_now": now,
            "time_now": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Europe/Amsterdam",
        },
    }


async def xtream_categories(user: User, kind: str) -> list[dict]:
    """kind: live|vod|series - distinct group names of enabled playlist items."""
    groups = _groups(user)
    key = {"live": "live", "vod": "vod", "series": "series"}[kind]
    model = {"live": LivePlaylist, "vod": VodPlaylist, "series": SeriePlaylist}[kind]
    async with SessionLocal() as s:
        rows = (await s.execute(select(model.group_name).where(model.enabled.is_(True))
                                .distinct())).scalars().all()
    out, seen = [], set()
    for i, name in enumerate(sorted(n or f"{kind.title()}" for n in rows), 1):
        if not _allowed(name, groups[key]):
            continue
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"category_id": str(i), "category_name": name, "parent_id": 0})
    return out


async def xtream_live(user: User, base_url: str) -> list[dict]:
    groups = _groups(user)
    cats = {c["category_name"]: c["category_id"] for c in await xtream_categories(user, "live")}
    async with SessionLocal() as s:
        items = (await s.execute(select(LivePlaylist).where(LivePlaylist.enabled.is_(True))
                                 .order_by(LivePlaylist.order))).scalars().all()
    out = []
    for it in items:
        if not _allowed(it.group_name, groups["live"]):
            continue
        out.append({
            "num": it.number or it.order, "name": it.custom_name, "stream_type": "live",
            "stream_id": it.id, "stream_icon": it.logo or "", "epg_channel_id": it.epg_id or "",
            "added": "0", "category_id": cats.get(it.group_name or "Live", "1"),
            "custom_sid": "", "tv_archive": 0, "direct_source": "",
            "tv_archive_duration": 0,
        })
    return out


async def xtream_vod(user: User) -> list[dict]:
    groups = _groups(user)
    cats = {c["category_name"]: c["category_id"] for c in await xtream_categories(user, "vod")}
    async with SessionLocal() as s:
        items = (await s.execute(select(VodPlaylist).where(VodPlaylist.enabled.is_(True))
                                 .order_by(VodPlaylist.order))).scalars().all()
    return [{
        "num": it.order, "name": it.custom_name, "stream_type": "movie",
        "stream_id": it.id, "stream_icon": it.poster or it.logo or "",
        "rating": it.rating or "", "rating_5based": 0, "added": "0",
        "category_id": cats.get(it.group_name or "VOD", "1"),
        "container_extension": "ts", "custom_sid": "", "direct_source": "",
    } for it in items if _allowed(it.group_name, groups["vod"])]


async def xtream_series(user: User) -> list[dict]:
    groups = _groups(user)
    cats = {c["category_name"]: c["category_id"] for c in await xtream_categories(user, "series")}
    async with SessionLocal() as s:
        items = (await s.execute(select(SeriePlaylist).where(SeriePlaylist.enabled.is_(True))
                                 .order_by(SeriePlaylist.order))).scalars().all()
    return [{
        "num": it.order, "name": it.custom_name, "stream_type": "series",
        "series_id": it.id, "cover": it.poster or it.logo or "",
        "plot": it.overview or "", "cast": "", "director": "",
        "genre": it.group_name or "", "release_date": it.year or "",
        "rating": it.rating or "", "rating_5based": 0,
        "category_id": cats.get(it.group_name or "Series", "1"),
        "backdrop_path": [], "youtube_trailer": "", "episode_run_time": "42",
        "last_modified": "0",
    } for it in items if _allowed(it.group_name, groups["series"])]


async def xtream_series_info(user: User, series_id: int) -> dict | None:
    groups = _groups(user)
    async with SessionLocal() as s:
        sp = await s.get(SeriePlaylist, series_id)
        if not sp or not _allowed(sp.group_name, groups["series"]):
            return None
        src = await s.get(SerieSource, sp.serie_source_id)
        season_rows = (await s.execute(
            select(SeriePlaylistSeason, SerieSeason)
            .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
            .where(SeriePlaylistSeason.serie_playlist_id == sp.id,
                   SeriePlaylistSeason.enabled.is_(True)))).all()
        episodes: dict[str, list] = {}
        season_meta = []
        for pls, season in season_rows:
            eps = (await s.execute(select(SerieEpisode).where(
                SerieEpisode.serie_season_id == season.id)
                .order_by(SerieEpisode.episode_number))).scalars().all()
            season_meta.append({"id": season.id, "season_number": season.season_number,
                                "name": season.name or f"Season {season.season_number}",
                                "episode_count": len(eps), "overview": "", "air_date": "",
                                "cover": sp.poster or "", "cover_big": sp.poster or ""})
            lst = []
            for ep in eps:
                lst.append({
                    "id": str(ep.id), "episode_num": ep.episode_number,
                    "title": ep.name or f"Episode {ep.episode_number}",
                    "container_extension": "ts",
                    "info": {"duration": (ep.duration or "42:00"),
                             "video": {}, "audio": {}, "bitrate": 0},
                    "custom_sid": "", "added": "0", "season": season.season_number,
                    "direct_source": "",
                })
            episodes[str(season.season_number)] = lst
        return {
            "seasons": season_meta,
            "info": {"name": sp.custom_name, "cover": sp.poster or "",
                     "plot": sp.overview or "", "cast": "", "director": "",
                     "genre": sp.group_name or "", "release_date": sp.year or "",
                     "rating": sp.rating or "", "rating_5based": 0,
                     "category_id": "1", "backdrop_path": [],
                     "youtube_trailer": "", "episode_run_time": "42"},
            "episodes": episodes,
        }
