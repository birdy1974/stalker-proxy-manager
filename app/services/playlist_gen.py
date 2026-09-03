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

from sqlalchemy import func, select
import time

from ..database import SessionLocal
from ..models import (
    LivePlaylist, LocalFile, LocalPlaylist, LocalSource, SerieEpisode,
    SeriePlaylist, SeriePlaylistSeason, SerieSeason, SerieSource, User,
    VodPlaylist, VodSource,
)
from .db_logging import db_log
from .local_files import extinf_duration, play_extension
from .runtime_settings import get_setting  # noqa: F401  (re-export; EPG scheduler)
from .titles import best_title, m3u_attr


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


def _norm_group(value) -> str:
    """Whitelist entries and group names compare normalised. Stray whitespace
    around either side (a pasted comma list, a cosmetic rename on the portal)
    must not silently blackhole a whole content type from a user's output."""
    return str(value or "").strip().lower()


def _allowed(group_name: str | None, whitelist: list[str]) -> bool:
    if not whitelist:
        return True
    # case-insensitive; whitespace-insensitive since the match decides whether
    # an entire category shows up at all
    return _norm_group(group_name) in {_norm_group(w) for w in whitelist}


def _chunked(seq: list, size: int = 800):
    """`.in_()` batches: keeps one round trip per batch instead of per row, and
    stays under SQLite's bound-parameter limit."""
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# (user.id, kind) -> last-warned signature. A group whitelist that matches
# NOTHING for a type that HAS items looks like a deliberate choice, so the
# empty output section it produces used to be completely silent - this is the
# classic cause of "VOD entries do not appear in my playlist": the portal
# renamed its categories, the stored whitelist still holds the old names, and
# the whitelist editor only renders groups that exist, making the dead
# entries invisible there.
_BLACKHOLE_WARNED: dict[tuple[int, str], tuple] = {}


async def _warn_if_blackholed(user: User, kind: str, items: list,
                              whitelist: list[str]) -> None:
    """Log once per state when a non-empty group whitelist filters an entire
    content type out of a user's output, naming the dead entries and the
    groups that DO exist so the log pane can explain the empty section."""
    key = (user.id, kind)
    if not whitelist or not items:
        _BLACKHOLE_WARNED.pop(key, None)
        return
    have = {_norm_group(it.group_name) for it in items} - {""}
    if any(_norm_group(w) in have for w in whitelist):
        _BLACKHOLE_WARNED.pop(key, None)
        return
    signature = (len(items), tuple(sorted(_norm_group(w) for w in whitelist)),
                 frozenset(have))
    if _BLACKHOLE_WARNED.get(key) == signature:
        return
    _BLACKHOLE_WARNED[key] = signature
    pretty = sorted({(it.group_name or "").strip() for it in items} - {""})
    await db_log(
        "WARNING", "playlist",
        f"user '{user.name}': the {kind} group whitelist "
        f"{sorted(str(w) for w in whitelist)} matches none of the library's "
        f"{kind} groups - all {len(items)} {kind} item(s) are missing from "
        f"this user's M3U/Xtream/bouquet output. The {kind} groups right now "
        f"are: {pretty[:8]}{' ...' if len(pretty) > 8 else ''}. Fix it under "
        f"Users > edit user > group whitelist (dead entries show as 'stale').")


_M3U_CACHE: dict[tuple, tuple[float, str]] = {}
_M3U_CACHE_TTL = 120
# Tables whose row count / max(id) form the cheap cache-bust signature.
_REVISION_TABLES = (LivePlaylist, VodPlaylist, SeriePlaylist, LocalPlaylist,
                    SeriePlaylistSeason, LocalFile, SerieEpisode)


def clear_m3u_cache() -> None:
    """Drop every cached playlist. Tests call this between schema resets so a
    zeroed DB cannot reuse a previous library's M3U under the same revision."""
    _M3U_CACHE.clear()
    _BLACKHOLE_WARNED.clear()


async def _playlist_revision(s) -> tuple:
    """Cheap per-database revision signature; changes invalidate cached M3Us.

    One round trip for every table (scalar subqueries), not N×2 sequential
    awaits. A genexp of `await`s is an *async* generator — `tuple(...)` cannot
    iterate it, which used to make every /playlist.m3u request answer 500
    (VLC then reports it cannot load the playlist).
    """
    cols = []
    for t in _REVISION_TABLES:
        cols.append(select(func.count()).select_from(t).scalar_subquery())
        cols.append(select(func.max(t.id)).select_from(t).scalar_subquery())
    row = (await s.execute(select(*cols))).one()
    # Pack as ((count, max_id), ...) so the cache key stays stable & readable.
    return tuple((row[i], row[i + 1]) for i in range(0, len(row), 2))


def _extinf_title(title: str | None) -> str:
    """Display name after the EXTINF comma — must be a single physical line."""
    return (title or "").replace("\r", " ").replace("\n", " ").replace("\0", "")


async def build_m3u(base_url: str, user: User) -> str:
    async with SessionLocal() as s:
        revision = await _playlist_revision(s)
    key = (user.id, user.groups_json or "", base_url, revision)
    cached = _M3U_CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _M3U_CACHE_TTL:
        return cached[1]
    text = await _build_m3u(base_url, user)
    _M3U_CACHE[key] = (time.monotonic(), text)
    # Bound memory when users/URLs change.
    if len(_M3U_CACHE) > 100:
        oldest = min(_M3U_CACHE, key=lambda k: _M3U_CACHE[k][0])
        _M3U_CACHE.pop(oldest, None)
    return text

async def _build_m3u(base_url: str, user: User) -> str:
    """
    Render the final per-user playlist (groups filtered per user).

    Everything is read with a fixed number of queries. This used to run one
    query per series (seasons), one per season (episodes) and one per local
    file, so a library with a few hundred series turned a playlist request into
    a thousand Postgres round trips - which is exactly the "VLC takes forever
    to load the playlist" complaint, because VLC blocks on this response before
    it shows a single channel.
    """
    groups = _groups(user)
    u, p = quote(user.name), quote(user.password)
    # No url-tvg / x-tvg-url: VLC (and several other players) block playlist
    # parse until that EPG URL finishes or times out (~30s). /epg.xml is still
    # served; add it in the player as a separate XMLTV source if you want guide.
    lines = ["#EXTM3U"]

    async with SessionLocal() as s:
        # ---- live ------------------------------------------------------
        items = (await s.execute(select(LivePlaylist).where(LivePlaylist.enabled.is_(True))
                                 .order_by(LivePlaylist.order, LivePlaylist.id))).scalars().all()
        await _warn_if_blackholed(user, "live", items, groups["live"])
        for it in items:
            if not _allowed(it.group_name, groups["live"]):
                continue
            title = _extinf_title(best_title(it.custom_name))
            attrs = {
                "tvg-chno": it.number if it.number is not None else it.order,
                "tvg-id": m3u_attr(it.epg_id or ""), "tvg-name": m3u_attr(title),
                "tvg-logo": m3u_attr(it.logo or ""), "group-title": m3u_attr(it.group_name or "Live"),
            }
            attr = " ".join(f'{k}="{v}"' for k, v in attrs.items())
            lines.append(f"#EXTINF:-1 {attr},{title}")
            lines.append(f"{base_url}/play/live/{it.id}.ts?u={u}&p={p}")

        # ---- vod -------------------------------------------------------
        vods = (await s.execute(select(VodPlaylist).where(VodPlaylist.enabled.is_(True))
                                .order_by(VodPlaylist.order, VodPlaylist.id))).scalars().all()
        await _warn_if_blackholed(user, "vod", vods, groups["vod"])
        src_names: dict[int, str] = {}
        wanted_src = [it.vod_source_id for it in vods if it.vod_source_id]
        for batch in _chunked(wanted_src):
            for src in (await s.execute(select(VodSource).where(VodSource.id.in_(batch)))).scalars().all():
                src_names[src.id] = src.original_name
        for it in vods:
            if not _allowed(it.group_name, groups["vod"]):
                continue
            # Prefer the longest non-year title: some portals store the year in
            # `name` and the full title in `o_name` (now original_name).
            title = _extinf_title(best_title(it.custom_name, src_names.get(it.vod_source_id)))
            attr = (f'tvg-name="{m3u_attr(title)}" '
                    f'tvg-logo="{m3u_attr(it.poster or it.logo or "")}" '
                    f'group-title="{m3u_attr(it.group_name or "VOD")}"')
            lines.append(f"#EXTINF:-1 {attr},{title}")
            lines.append(f"{base_url}/play/vod/{it.id}.ts?u={u}&p={p}")

        # ---- series: one m3u entry per episode of enabled seasons ------
        # Filter by the user's group whitelist FIRST, then fetch seasons and
        # episodes for the visible series in two queries total.
        series = (await s.execute(select(SeriePlaylist).where(SeriePlaylist.enabled.is_(True))
                                  .order_by(SeriePlaylist.order, SeriePlaylist.id))).scalars().all()
        await _warn_if_blackholed(user, "series", series, groups["series"])
        visible = [sp for sp in series if _allowed(sp.group_name, groups["series"])]

        season_rows: list = []
        for batch in _chunked([sp.id for sp in visible]):
            season_rows += (await s.execute(
                select(SeriePlaylistSeason.serie_playlist_id, SerieSeason.id,
                       SerieSeason.season_number)
                .join(SerieSeason, SerieSeason.id == SeriePlaylistSeason.serie_season_id)
                .where(SeriePlaylistSeason.serie_playlist_id.in_(batch),
                       SeriePlaylistSeason.enabled.is_(True))
                .order_by(SerieSeason.season_number))).all()

        eps_by_season: dict[int, list] = {}
        for batch in _chunked([r[1] for r in season_rows]):
            for season_id, ep_id, ep_num in (await s.execute(
                    select(SerieEpisode.serie_season_id, SerieEpisode.id,
                           SerieEpisode.episode_number)
                    .where(SerieEpisode.serie_season_id.in_(batch))
                    .order_by(SerieEpisode.episode_number))).all():
                eps_by_season.setdefault(season_id, []).append((ep_id, ep_num))

        seasons_by_playlist: dict[int, list] = {}
        for row in season_rows:
            seasons_by_playlist.setdefault(row[0], []).append(row)
        for sp in visible:
            for sp_id, season_id, season_number in seasons_by_playlist.get(sp.id, []):
                for ep_id, ep_num in eps_by_season.get(season_id, []):
                    name = _extinf_title(
                        f"{best_title(sp.custom_name)} S{season_number:02d}E{ep_num:02d}")
                    attr = (f'tvg-name="{m3u_attr(name)}" '
                            f'tvg-logo="{m3u_attr(sp.poster or sp.logo or "")}" '
                            f'group-title="{m3u_attr("Series: " + (sp.group_name or sp.custom_name))}"')
                    lines.append(f"#EXTINF:-1 {attr},{name}")
                    lines.append(f"{base_url}/play/episode/{ep_id}.ts?u={u}&p={p}")

        # ---- local files ------------------------------------------------
        locals_ = (await s.execute(select(LocalPlaylist).where(LocalPlaylist.enabled.is_(True))
                                   .order_by(LocalPlaylist.order, LocalPlaylist.id))).scalars().all()
        await _warn_if_blackholed(user, "local", locals_, groups["local"])
        files: dict[int, LocalFile] = {}
        wanted = [it.local_file_id for it in locals_ if it.local_file_id]
        for batch in _chunked(wanted):
            for f in (await s.execute(select(LocalFile).where(
                    LocalFile.id.in_(batch)))).scalars().all():
                files[f.id] = f
        for it in locals_:
            if not _allowed(it.group_name, groups["local"]):
                continue
            lf = files.get(it.local_file_id)
            if not lf:
                continue
            name = _extinf_title(best_title(it.custom_name, lf.filename))
            dur = extinf_duration(lf.duration_s)
            ext = play_extension(lf.filename)
            lines.append(f'#EXTINF:{dur} tvg-name="{m3u_attr(name)}" '
                         f'group-title="{m3u_attr(it.group_name or "vod-local")}",{name}')
            lines.append(f"{base_url}/play/local/{it.id}{ext}?u={u}&p={p}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Xtream Codes API data views (player_api.php)
# ---------------------------------------------------------------------------
async def xtream_base(user: User, base_url: str) -> dict:
    from .stream_manager import MANAGER as _mgr
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "user_info": {
            "username": user.name, "password": user.password, "message": "",
            "auth": 1, "status": "Active",
            "exp_date": str(int(datetime.fromisoformat(user.expire_date).timestamp()))
            if user.expire_date else None,
            "is_trial": "0",
            "active_cons": str(_mgr.user_stream_count(user.name)),
            "created_at": str(now),
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
            "tv_archive_duration": 0, "timeshift": "", "is_adult": 0,
        })
    return out


async def xtream_vod(user: User) -> list[dict]:
    groups = _groups(user)
    cats = {c["category_name"]: c["category_id"] for c in await xtream_categories(user, "vod")}
    async with SessionLocal() as s:
        items = (await s.execute(select(VodPlaylist).where(VodPlaylist.enabled.is_(True))
                                 .order_by(VodPlaylist.order))).scalars().all()
    src_names: dict[int, str] = {}
    async with SessionLocal() as s2:
        wanted = [it.vod_source_id for it in items if it.vod_source_id]
        for batch in _chunked(wanted):
            for src in (await s2.execute(select(VodSource).where(VodSource.id.in_(batch)))).scalars().all():
                src_names[src.id] = src.original_name
    return [{
        "num": it.order,
        "name": best_title(it.custom_name, src_names.get(it.vod_source_id)),
        "stream_type": "movie",
        "stream_id": it.id, "stream_icon": it.poster or it.logo or "",
        "rating": it.rating or "", "rating_5based": 0, "added": "0",
        "category_id": cats.get(it.group_name or "VOD", "1"),
        "container_extension": "ts", "custom_sid": "", "direct_source": "",
    } for it in items if _allowed(it.group_name, groups["vod"])]


async def xtream_vod_info(user: User, vod_id: int) -> dict | None:
    """Real get_vod_info answer (Phase 3): metadata from the playlist row with
    the portal source as fallback (poster/plot/year/rating may live on either)."""
    groups = _groups(user)
    async with SessionLocal() as s:
        it = await s.get(VodPlaylist, vod_id)
        if not it or not it.enabled or not _allowed(it.group_name, groups["vod"]):
            return None
        src = await s.get(VodSource, it.vod_source_id)
    poster = it.poster or (src.poster if src else "") or it.logo or ""
    plot = it.overview or (src.description if src else "") or ""
    rating = it.rating or (src.rating if src else "") or ""
    year = it.year or (src.year if src else "") or ""
    title = best_title(it.custom_name, src.original_name if src else None)
    cats = {c["category_name"]: c["category_id"] for c in await xtream_categories(user, "vod")}
    info = {
        "kinopoisk_url": "", "tmdb_id": str(it.tmdb_id or ""),
        "name": title, "o_name": src.original_name if src else title,
        "cover_big": poster, "movie_image": poster, "releasedate": year,
        "episode_run_time": 0, "youtube_trailer": "", "director": "",
        "actors": "", "cast": "", "description": plot, "plot": plot,
        "age": "", "mpaa_rating": "", "rating_count_kinopoisk": 0,
        "country": "", "genre": it.group_name or "", "backdrop_path": [poster] if poster else [],
        "duration_secs": 0, "duration": src.duration if src and src.duration else "",
        "video": [], "audio": [], "bitrate": 0, "rating": rating,
    }
    return {"info": info,
            "movie_data": {"stream_id": it.id, "name": title, "added": "0",
                           "category_id": cats.get(it.group_name or "VOD", "1"),
                           "container_extension": "ts", "custom_sid": "", "direct_source": ""}}


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
                             "movie_image": sp.poster or "",
                             "tmdb_id": str(sp.tmdb_id or "") if sp.tmdb_id else "",
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
