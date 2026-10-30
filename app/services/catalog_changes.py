"""Reviewable differences found while re-fetching enabled portal genres.

The source tables are the current catalogue and their upserts intentionally
replace portal metadata. This module captures only a bounded, display-safe
before/after summary before that happens. It does not retain stream commands,
credentials, or a complete catalogue snapshot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import func, select

from ..models import (
    CatalogChange, LiveGenre, LivePlaylist, LivePlaylistSource, LiveSource,
    SerieEpisode, SerieGenre, SeriePlaylist, SeriePlaylistSeason,
    SeriePlaylistSource, SerieSeason, SerieSource, VodGenre, VodPlaylist,
    VodPlaylistSource, VodSource, utcnow,
)
from .titles import best_title

KINDS = ("live", "vod", "series")

# Only human-useful source metadata is kept in the before/after summary. Stream
# commands are compared, but their contents can contain credentials or tokens,
# so the review record says only that the portal link changed.
_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "live": (
        ("Name", "original_name"),
        ("Channel number", "number"),
        ("Logo", "logo_original"),
        ("EPG ID", "epg_original"),
        ("Catch-up", "tv_archive"),
        ("Link flags", "link_flags"),
    ),
    "vod": (
        ("Title", "original_name"),
        ("Year", "year"),
        ("Genre", "genre"),
        ("Poster", "poster"),
        ("Rating", "rating"),
        ("Description", "description"),
        ("Director", "director"),
        ("Cast", "actors"),
        ("Duration", "duration"),
        ("Added", "added"),
        ("Link flags", "link_flags"),
    ),
    "series": (
        ("Title", "original_name"),
        ("Year", "year"),
        ("Genre", "category_name"),
        ("Poster", "poster"),
        ("Rating", "rating"),
        ("Description", "description"),
        ("Season metadata", "raw_series"),
    ),
}


def _season_summary(value: Any) -> str | None:
    """Turn provider season metadata into a short, deterministic display value."""
    if value in (None, ""):
        return None
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw[:300]
    if isinstance(raw, list):
        labels: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                label = (item.get("name") or item.get("season_name")
                         or item.get("season_number") or item.get("season_id")
                         or item.get("id"))
                labels.append(str(label) if label is not None else "season")
            else:
                labels.append(str(item))
        return ", ".join(labels)[:300]
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False, sort_keys=True)[:300]
    return str(raw)[:300]


def _display_value(kind: str, label: str, value: Any) -> Any:
    if value is None:
        return None
    if label == "Season metadata":
        return _season_summary(value)
    if label in ("Logo", "Poster"):
        # Image URLs occasionally carry signed query strings or embedded user
        # info. The review UI needs to show that the artwork reference changed,
        # not persist those access tokens in the change table.
        raw = str(value)
        try:
            parts = urlsplit(raw)
        except ValueError:
            return "Unparseable image reference"
        if parts.scheme and parts.netloc:
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = parts.port
            except ValueError:
                port = None
            if port:
                host = f"{host}:{port}"
            return f"{parts.scheme}://{host}{parts.path}"[:300]
        return raw.split("?", 1)[0].split("#", 1)[0][:300]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    # Descriptions and cast lists can be large; the review screen needs a
    # useful excerpt, not a second copy of the entire portal payload.
    return str(value)[:300]


def source_snapshot(kind: str, row) -> dict[str, Any]:
    """Safe, bounded display fields from a source row before/after an upsert."""
    if kind not in _FIELDS:
        return {}
    # Keep raw field values only in this short-lived in-memory comparison.
    # Truncation/season summarization happens only for fields that actually
    # changed, so long catalogues do not repeatedly parse every series blob.
    return {label: getattr(row, attr, None) for label, attr in _FIELDS[kind]}


def source_diff(kind: str, before: dict[str, Any], after: dict[str, Any],
                old_cmd: str | None = None, new_cmd: str | None = None) -> dict[str, dict[str, Any]]:
    """Return changed display fields; never put a portal command in the result."""
    changes = {}
    for label, _attr in _FIELDS.get(kind, ()):
        old, new = before.get(label), after.get(label)
        if old != new:
            changes[label] = {"before": _display_value(kind, label, old),
                              "after": _display_value(kind, label, new)}
    if old_cmd != new_cmd:
        changes["Playback link"] = {
            "before": "Portal link" if old_cmd else "No portal link",
            "after": "Updated portal link" if new_cmd else "No portal link",
        }
    return changes


def decode_changes(raw: str | None) -> dict[str, dict[str, Any]]:
    try:
        parsed = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def record_changes(db, portal_id: int, kind: str,
                         events: list[tuple[int, str, dict[str, Any]]]) -> int:
    """Upsert the latest change state for each source; caller commits the session.

    A pending "new" item remains new if its metadata changes before review. A
    reviewed/ignored source that changes later becomes pending again as
    "changed". `detected_at` preserves the first observation; `updated_at`
    records the most recent one.
    """
    if kind not in KINDS or not events:
        return 0
    source_ids = list(dict.fromkeys(int(source_id) for source_id, _, _ in events))
    existing = {
        row.source_id: row
        for row in (await db.execute(
            select(CatalogChange).where(
                CatalogChange.portal_id == portal_id,
                CatalogChange.kind == kind,
                CatalogChange.source_id.in_(source_ids),
            )
        )).scalars().all()
    }
    now = utcnow()
    for source_id, change_type, changes in events:
        row = existing.get(source_id)
        encoded = json.dumps(changes or {}, ensure_ascii=False, separators=(",", ":"))
        if row is None:
            row = CatalogChange(
                portal_id=portal_id,
                kind=kind,
                source_id=source_id,
                change_type=change_type,
                status="pending",
                changes_json=encoded,
                detected_at=now,
                updated_at=now,
            )
            db.add(row)
            existing[source_id] = row
            continue
        keep_new = row.status == "pending" and row.change_type == "new"
        row.change_type = "new" if keep_new else change_type
        row.status = "pending"
        row.changes_json = encoded
        row.updated_at = now
    await db.flush()
    return len(events)


# --------------------------------------------------------------------------- #
# Review: copying detected source changes into existing Playlist items
# --------------------------------------------------------------------------- #
# A catalogue scan has already refreshed the *source* rows by the time a
# CatalogChange is reviewed. Playback-only source fields (cmd, link flags,
# catch-up, duration) are therefore already live. The Playlist tables also
# carry denormalised copies of a few source fields, though, and those copies
# are what Playlist Builder/M3U/Xtream display. Reviewing a change now copies
# the mapped fields below into the existing Playlist item for which this
# source is primary/canonical. Fallback-only sources never rename/re-group a
# shared channel. Playlist order, enabled state, live channel number/lock and
# FFmpeg templates are deliberately not part of this map.
_PLAYLIST_TRIGGER_LABELS = {
    "Name", "Title", "Logo", "Poster", "EPG ID", "Year", "Rating",
    "Description", "Genre", "Season metadata",
}


def _values_equal(current: Any, proposed: Any) -> bool:
    if isinstance(current, (str, type(None))) and isinstance(proposed, (str, type(None))):
        return (current or "") == (proposed or "")
    return current == proposed


def _playlist_display(field: str, value: Any) -> Any:
    if field == "logo":
        return _display_value("live", "Logo", value)
    if field == "poster":
        return _display_value("vod", "Poster", value)
    if field == "overview":
        return _display_value("vod", "Description", value)
    return _display_value("live", "Name", value)


def _playlist_value_map(kind: str, source, genre) -> dict[str, tuple[str, Any, set[str]]]:
    """Playlist field -> (GUI label, latest source value, diff labels)."""
    genre_name = genre.name if genre else None
    if kind == "live":
        return {
            "custom_name": ("Playlist name", source.original_name, {"Name"}),
            "group_name": ("Playlist group", genre_name, {"Genre"}),
            "logo": ("Playlist logo", source.logo_original, {"Logo"}),
            "epg_id": ("Playlist EPG ID", source.epg_original, {"EPG ID"}),
        }
    if kind == "vod":
        return {
            "custom_name": ("Playlist title", best_title(source.original_name), {"Title"}),
            "group_name": ("Playlist group", genre_name or source.genre,
                           {"Genre"}),
            "poster": ("Playlist poster", source.poster, {"Poster"}),
            "year": ("Playlist year", source.year, {"Year"}),
            "rating": ("Playlist rating", source.rating, {"Rating"}),
            "overview": ("Playlist description", source.description, {"Description"}),
        }
    if kind == "series":
        return {
            "custom_name": ("Playlist title", best_title(source.original_name), {"Title"}),
            "group_name": ("Playlist group", genre_name or source.category_name,
                           {"Genre"}),
            "poster": ("Playlist poster", source.poster, {"Poster"}),
            "year": ("Playlist year", source.year, {"Year"}),
            "rating": ("Playlist rating", source.rating, {"Rating"}),
            "overview": ("Playlist description", source.description, {"Description"}),
        }
    return {}


def _is_derived_group(kind: str, current: Any, genre_names: set[str]) -> bool:
    group = str(current or "").strip()
    default = {"live": "Live", "vod": "VOD", "series": "Series"}.get(kind, "")
    return group in ("", default) or group in genre_names


def _compute_target_updates(kind: str, source, genre, playlist,
                            change_labels: set[str], genre_names: set[str]):
    """Internal (raw) Playlist field updates for one primary target."""
    updates: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for field, (label, proposed, triggers) in _playlist_value_map(kind, source, genre).items():
        if field in ("custom_name", "group_name") and not str(proposed or "").strip():
            continue
        current = getattr(playlist, field, None)
        if _values_equal(current, proposed):
            continue
        entry = {"field": field, "label": label,
                 "before_raw": current, "after_raw": proposed}
        if field == "group_name":
            # A detected genre move follows the portal. Without that signal,
            # only a group that still looks portal-derived (a genre name or
            # the generic default) is moved; a genuinely custom Playlist
            # group is reported as kept instead of silently overwritten.
            if triggers & change_labels or _is_derived_group(kind, current, genre_names):
                updates.append(entry)
            else:
                kept.append({**entry, "reason": "custom Playlist group kept"})
        elif triggers & change_labels:
            updates.append(entry)
    return updates, kept


async def _genre_names(db, portal_id: int, kind: str) -> set[str]:
    model = {"live": LiveGenre, "vod": VodGenre, "series": SerieGenre}.get(kind)
    if model is None:
        return set()
    rows = (await db.execute(select(model.name).where(model.portal_id == portal_id))).all()
    return {str(name).strip() for (name,) in rows if str(name or "").strip()}


async def _playlist_targets(db, kind: str, source_ids: list[int]):
    """Return (primary/canonical targets, fallback-only placements)."""
    primary: dict[int, list[tuple[Any, int | None]]] = {}
    fallback: dict[int, list[dict[str, Any]]] = {}
    if not source_ids:
        return primary, fallback

    if kind == "live":
        links = (await db.execute(
            select(LivePlaylistSource.live_source_id,
                   LivePlaylistSource.live_playlist_id,
                   LivePlaylistSource.priority)
            .where(LivePlaylistSource.live_source_id.in_(source_ids)))).all()
        playlist_ids = {pid for _sid, pid, _prio in links}
        if not playlist_ids:
            return primary, fallback
        stats = {pid: (int(min_prio or 0), int(count or 0)) for pid, min_prio, count in (await db.execute(
            select(LivePlaylistSource.live_playlist_id,
                   func.min(LivePlaylistSource.priority), func.count())
            .where(LivePlaylistSource.live_playlist_id.in_(playlist_ids))
            .group_by(LivePlaylistSource.live_playlist_id))).all()}
        playlists = {row.id: row for row in (await db.execute(
            select(LivePlaylist).where(LivePlaylist.id.in_(playlist_ids)))).scalars().all()}
        for sid, pid, prio in links:
            playlist = playlists.get(pid)
            if playlist is None:
                continue
            min_prio, chain_len = stats.get(pid, (prio, 1))
            if prio == min_prio:
                primary.setdefault(sid, []).append((playlist, prio))
            else:
                fallback.setdefault(sid, []).append({
                    "playlist_id": pid, "name": playlist.custom_name,
                    "priority": prio, "chain_len": chain_len,
                })
        return primary, fallback

    if kind == "vod":
        playlist_model, canonical_col = VodPlaylist, VodPlaylist.vod_source_id
        link_model = VodPlaylistSource
        link_playlist_col, link_source_col = link_model.vod_playlist_id, link_model.vod_source_id
    elif kind == "series":
        playlist_model, canonical_col = SeriePlaylist, SeriePlaylist.serie_source_id
        link_model = SeriePlaylistSource
        link_playlist_col, link_source_col = link_model.serie_playlist_id, link_model.serie_source_id
    else:
        return primary, fallback

    playlists = (await db.execute(
        select(playlist_model).where(canonical_col.in_(source_ids)))).scalars().all()
    links = (await db.execute(
        select(link_source_col, link_playlist_col, link_model.priority, playlist_model)
        .join(playlist_model, playlist_model.id == link_playlist_col)
        .where(link_source_col.in_(source_ids)))).all()
    priority_by_pair = {(sid, pid): prio for sid, pid, prio, _pl in links}
    for playlist in playlists:
        sid = getattr(playlist, canonical_col.key)
        primary.setdefault(sid, []).append(
            (playlist, priority_by_pair.get((sid, playlist.id))))
    fallback_pids = {pid for sid, pid, _prio, pl in links
                     if getattr(pl, canonical_col.key) != sid}
    chain_lengths = {}
    if fallback_pids:
        chain_lengths = {pid: int(count or 0) for pid, count in (await db.execute(
            select(link_playlist_col, func.count())
            .where(link_playlist_col.in_(fallback_pids))
            .group_by(link_playlist_col))).all()}
    for sid, pid, prio, playlist in links:
        if getattr(playlist, canonical_col.key) == sid:
            continue
        fallback.setdefault(sid, []).append({
            "playlist_id": pid, "name": playlist.custom_name,
            "priority": prio, "chain_len": chain_lengths.get(pid, 1),
        })
    return primary, fallback


async def _missing_season_links(db, primary) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """SeriePlaylistSeason rows a review would add for canonical targets."""
    source_ids = list(primary)
    playlist_ids = {playlist.id for targets in primary.values() for playlist, _prio in targets}
    if not source_ids or not playlist_ids:
        return {}
    seasons = (await db.execute(
        select(SerieSeason).where(SerieSeason.serie_source_id.in_(source_ids))
        .order_by(SerieSeason.serie_source_id, SerieSeason.season_number))).scalars().all()
    if not seasons:
        return {}
    existing = {(pid, season_id) for pid, season_id in (await db.execute(
        select(SeriePlaylistSeason.serie_playlist_id,
               SeriePlaylistSeason.serie_season_id)
        .where(SeriePlaylistSeason.serie_playlist_id.in_(playlist_ids)))).all()}
    counts = {season_id: int(count or 0) for season_id, count in (await db.execute(
        select(SerieEpisode.serie_season_id, func.count())
        .where(SerieEpisode.serie_season_id.in_([s.id for s in seasons]))
        .group_by(SerieEpisode.serie_season_id))).all()}
    by_source: dict[int, list[SerieSeason]] = {}
    for season in seasons:
        by_source.setdefault(season.serie_source_id, []).append(season)
    missing: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for sid, targets in primary.items():
        for playlist, _prio in targets:
            rows = []
            for season in by_source.get(sid, []):
                if (playlist.id, season.id) in existing:
                    continue
                rows.append({
                    "season_id": season.id,
                    "season_number": season.season_number,
                    "name": season.name or f"Season {season.season_number}",
                    "enabled": bool(season.enabled),
                    "episode_count": counts.get(season.id, 0),
                })
            if rows:
                missing[(sid, playlist.id)] = rows
    return missing


def _display_updates(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "field": entry["field"],
        "label": entry["label"],
        "before": _playlist_display(entry["field"], entry["before_raw"]),
        "after": _playlist_display(entry["field"], entry["after_raw"]),
        **({"reason": entry["reason"]} if entry.get("reason") else {}),
    } for entry in entries]


async def playlist_update_plans(db, portal_id: int, kind: str, entries) -> dict[int, dict[str, Any]]:
    """Display-safe, exact Playlist consequences of reviewing each change.

    `entries` are `(CatalogChange, source, genre)` triples for one kind. The
    returned plan is keyed by CatalogChange id and is also the GUI contract:
    primary targets list every Playlist field that *Update Playlist & mark
    reviewed* will set, fallback placements explain why channel-level fields
    stay put, and source-only labels have no Playlist counterpart.
    """
    entries = list(entries)
    if not entries:
        return {}
    source_ids = [source.id for _change, source, _genre in entries]
    genre_names = await _genre_names(db, portal_id, kind)
    primary, fallback = await _playlist_targets(db, kind, source_ids)
    season_missing = await _missing_season_links(db, primary) if kind == "series" else {}

    plans: dict[int, dict[str, Any]] = {}
    for change, source, genre in entries:
        changes = decode_changes(change.changes_json)
        labels = set(changes)
        targets = []
        update_count = 0
        for playlist, priority in primary.get(source.id, []):
            updates, kept = _compute_target_updates(
                kind, source, genre, playlist, labels, genre_names)
            season_links = season_missing.get((source.id, playlist.id), [])
            update_count += len(updates) + len(season_links)
            targets.append({
                "playlist_id": playlist.id,
                "name": playlist.custom_name,
                "role": "primary",
                "priority": priority,
                "updates": _display_updates(updates),
                "kept": _display_updates(kept),
                "season_links": season_links,
            })
        plans[change.id] = {
            "state": "primary" if targets else ("fallback" if fallback.get(source.id) else "none"),
            "targets": targets,
            "fallback_targets": fallback.get(source.id, []),
            "source_only": [label for label in changes if label not in _PLAYLIST_TRIGGER_LABELS],
            "update_count": update_count,
        }
    return plans


async def apply_reviewed_playlist_updates(db, portal_id: int, rows) -> dict[str, Any]:
    """Apply the plans for rows being marked reviewed; caller commits."""
    totals: dict[str, Any] = {
        "playlist_items_updated": 0,
        "playlist_fields_updated": 0,
        "season_links_added": 0,
        "epg_changed": False,
    }
    updated_playlists: set[tuple[str, int]] = set()
    by_kind: dict[str, list[CatalogChange]] = {}
    for row in rows:
        by_kind.setdefault(row.kind, []).append(row)

    source_models = {"live": LiveSource, "vod": VodSource, "series": SerieSource}
    genre_models = {"live": LiveGenre, "vod": VodGenre, "series": SerieGenre}
    genre_fk = {"live": "live_genre_id", "vod": "vod_genre_id", "series": "serie_genre_id"}
    for kind, kind_rows in by_kind.items():
        source_model = source_models.get(kind)
        if source_model is None:
            continue
        source_ids = list(dict.fromkeys(row.source_id for row in kind_rows))
        sources = {source.id: source for source in (await db.execute(
            select(source_model).where(source_model.id.in_(source_ids),
                                       source_model.portal_id == portal_id))).scalars().all()}
        genre_ids = {getattr(source, genre_fk[kind]) for source in sources.values()
                     if getattr(source, genre_fk[kind], None)}
        genres = {}
        if genre_ids:
            genre_model = genre_models[kind]
            genres = {genre.id: genre for genre in (await db.execute(
                select(genre_model).where(genre_model.id.in_(genre_ids)))).scalars().all()}
        genre_names = await _genre_names(db, portal_id, kind)
        primary, _fallback = await _playlist_targets(db, kind, list(sources))
        season_missing = await _missing_season_links(db, primary) if kind == "series" else {}

        for change in kind_rows:
            source = sources.get(change.source_id)
            if source is None:
                continue
            genre = genres.get(getattr(source, genre_fk[kind], None))
            labels = set(decode_changes(change.changes_json))
            for playlist, _priority in primary.get(source.id, []):
                updates, _kept = _compute_target_updates(
                    kind, source, genre, playlist, labels, genre_names)
                changed = False
                for update in updates:
                    setattr(playlist, update["field"], update["after_raw"])
                    totals["playlist_fields_updated"] += 1
                    changed = True
                    if kind == "live" and update["field"] == "epg_id":
                        totals["epg_changed"] = True
                for season in season_missing.get((source.id, playlist.id), []):
                    db.add(SeriePlaylistSeason(
                        serie_playlist_id=playlist.id,
                        serie_season_id=season["season_id"],
                        enabled=bool(season["enabled"])))
                    totals["season_links_added"] += 1
                    changed = True
                if changed:
                    updated_playlists.add((kind, playlist.id))
    await db.flush()
    totals["playlist_items_updated"] = len(updated_playlists)
    return totals


def aware_utc(value: datetime | None) -> datetime | None:
    """SQLite may return naive UTC datetimes for timezone-aware columns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
