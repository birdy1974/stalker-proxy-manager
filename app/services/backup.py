"""Complete, portable database backups. No raw SQL or uploaded identifiers are executed.

V2 carries original primary keys solely as references: inserts allocate fresh IDs,
then rebind every FK (including the polymorphic area playlist reference). Existing
natural identities are never updated. Dependencies accompany individual tables.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Integer, String, or_, select, true

from ..models import Base

APP = "stalker-proxy-manager"
VERSION = 2
TABLES = Base.metadata.tables
PLAYLISTS = {"live": "live_playlist", "vod": "vod_playlist",
             "series": "serie_playlist", "local": "local_playlist"}
# Explicit identities also cover tables without database uniqueness constraints.
IDENTITIES = {
    "active_streams": ("id",), "settings": ("key",),
    "portals": ("name",), "ffmpeg_templates": ("name",),
    "areas": ("name",), "users": ("name",), "enigma2_profiles": ("name",),
    "epg_sources": ("url",), "epg_channels": ("epg_source_id", "tvg_id"),
    "epg_programmes": ("epg_source_id", "tvg_id", "start_ts", "title"),
    "epg_channel_sources": ("live_playlist_id", "epg_source_id", "tvg_id"),
    "logs": ("ts", "level", "module", "message"),
    "mac_addresses": ("portal_id", "mac"),
    "local_sources": ("directory",), "local_files": ("local_source_id", "relative_path"),
    "live_genres": ("portal_id", "genre_portal_id"),
    "vod_genres": ("portal_id", "genre_portal_id"),
    "serie_genres": ("portal_id", "genre_portal_id"),
    "live_sources": ("portal_id", "portal_channel_id"),
    "vod_sources": ("portal_id", "portal_item_id"),
    "serie_sources": ("portal_id", "portal_item_id"),
    "serie_seasons": ("serie_source_id", "season_number"),
    "serie_episodes": ("serie_season_id", "portal_item_id"),
    "live_playlist": ("custom_name",), "vod_playlist": ("custom_name",),
    "serie_playlist": ("custom_name",), "local_playlist": ("local_file_id",),
    "live_playlist_sources": ("live_playlist_id", "live_source_id"),
    "vod_playlist_sources": ("vod_playlist_id", "vod_source_id"),
    "serie_playlist_sources": ("serie_playlist_id", "serie_source_id"),
    "serie_playlist_seasons": ("serie_playlist_id", "serie_season_id"),
    "area_item_templates": ("area_id", "kind", "playlist_id"),
}
NOTES = [
    "All database columns are included. Backups contain passwords and tokens; store them securely.",
    "Active-stream rows are exported for reference only, never restored as running processes.",
    "Media files, uploaded favicon files, filesystem caches and environment/Docker settings are not included.",
    "Individual tables include their referenced parent rows. Restore adds missing rows only; existing values stay unchanged.",
]


def pk(table):
    return next(iter(table.primary_key.columns))


def table_names(names):
    if not isinstance(names, list) or not names or any(not isinstance(n, str) or n not in TABLES for n in names):
        raise ValueError("Select one or more known database tables.")
    return list(dict.fromkeys(names))


def references(name, row):
    for fk in TABLES[name].foreign_keys:
        value = row.get(fk.parent.name)
        if value is not None:
            yield fk.parent.name, fk.column.table.name, value
    if name == "area_item_templates":
        parent = PLAYLISTS.get(row.get("kind"))
        if not parent:
            raise ValueError("Unknown area item kind.")
        yield "playlist_id", parent, row.get("playlist_id")


def subset(tables, roots, setting_keys=None):
    """Select requested rows and their transitive parents, never unrelated rows."""
    indexes = {n: {r[pk(TABLES[n]).name]: r for r in rows} for n, rows in tables.items()}
    chosen = {n: {} for n in roots}

    def include(name, ident):
        if ident in chosen.setdefault(name, {}):
            return
        row = indexes.get(name, {}).get(ident)
        if row is None:
            raise ValueError(f"Missing referenced row in {name}; include its parent table in the backup.")
        chosen[name][ident] = row
        for _col, parent, ref in references(name, row):
            include(parent, ref)

    for name in roots:
        if name not in tables:
            raise ValueError(f"Backup does not contain {name}.")
        for row in tables[name]:
            if name == "settings" and setting_keys is not None and row["key"] not in setting_keys:
                continue
            include(name, row[pk(TABLES[name]).name])
    return {n: list(rows.values()) for n, rows in chosen.items()}


async def export(db, names, setting_keys=None):
    names = table_names(names)
    # Load only selected tables, then referenced rows in batches. Backing up
    # one setting must not read a potentially huge EPG/catalog/log database.
    indexes = {}
    pending = []
    for name in names:
        table = TABLES[name]
        query = select(table).order_by(pk(table))
        if name == "settings" and setting_keys is not None:
            query = query.where(table.c.key.in_(setting_keys))
        records = [dict(r) for r in (await db.execute(query)).mappings()]
        indexes[name] = {r[pk(table).name]: r for r in records}
        pending.extend((name, r) for r in records)
    while pending:
        missing = {}
        for name, row in pending:
            for _col, parent, ref in references(name, row):
                if ref not in indexes.get(parent, {}):
                    missing.setdefault(parent, set()).add(ref)
        pending = []
        for name, ids in missing.items():
            table = TABLES[name]
            ids = list(ids)
            for offset in range(0, len(ids), 500):
                chunk = ids[offset:offset + 500]
                records = [dict(r) for r in (await db.execute(
                    select(table).where(pk(table).in_(chunk)).order_by(pk(table)))).mappings()]
                if len(records) != len(chunk):
                    raise ValueError(f"{name}: orphaned reference in database; repair it before backing up.")
                indexes.setdefault(name, {}).update({r[pk(table).name]: r for r in records})
                pending.extend((name, r) for r in records)
    tables = {name: list(records.values()) for name, records in indexes.items()}
    for rows in tables.values():
        for row in rows:
            for key, value in row.items():
                if isinstance(value, datetime):
                    row[key] = value.isoformat()
    return {"app": APP, "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
            "selected_tables": names, "tables": tables, "notes": NOTES}


def validate(data):
    if not isinstance(data, dict) or data.get("app") != APP or data.get("version") != VERSION:
        raise ValueError("Choose a Stalker Proxy Manager version 2 backup.")
    tables = data.get("tables")
    if not isinstance(tables, dict) or not tables:
        raise ValueError("Backup has no tables.")
    table_names(list(tables))
    clean = {}
    for name, rows in tables.items():
        if not isinstance(rows, list):
            raise ValueError(f"{name}: expected a list of rows.")
        table = TABLES[name]
        seen = set()
        clean[name] = []
        for raw in rows:
            if not isinstance(raw, dict) or set(raw) - set(table.c.keys()):
                raise ValueError(f"{name}: invalid row or unknown columns (possibly a newer backup).")
            row = dict(raw)
            required = {pk(table).name, *IDENTITIES[name]}
            required.update(c.name for c in table.c if not c.nullable and c.default is None and c.server_default is None)
            if required - row.keys():
                raise ValueError(f"{name}: missing required columns.")
            for col in table.c:
                if col.name not in row:
                    continue
                value = row[col.name]
                if value is None:
                    if not col.nullable:
                        raise ValueError(f"{name}.{col.name}: null is not allowed.")
                    continue
                typ = col.type
                if isinstance(typ, DateTime):
                    try:
                        value = datetime.fromisoformat(value)
                        if value.tzinfo is None:
                            value = value.replace(tzinfo=timezone.utc)
                        row[col.name] = value
                    except (ValueError, TypeError):
                        raise ValueError(f"{name}.{col.name}: invalid timestamp.") from None
                elif isinstance(typ, Boolean):
                    if type(value) is not bool:
                        raise ValueError(f"{name}.{col.name}: expected a boolean.")
                elif isinstance(typ, Integer):
                    if type(value) is not int:
                        raise ValueError(f"{name}.{col.name}: expected an integer.")
                elif isinstance(typ, Float):
                    if type(value) not in (float, int):
                        raise ValueError(f"{name}.{col.name}: expected a number.")
                elif isinstance(typ, String):
                    if not isinstance(value, str) or (typ.length and len(value) > typ.length):
                        raise ValueError(f"{name}.{col.name}: invalid text.")
            ident = row[pk(table).name]
            if ident in seen:
                raise ValueError(f"{name}: duplicate backup primary key.")
            seen.add(ident)
            clean[name].append(row)
    return clean


async def restore(db, data, names=None, setting_keys=None):
    tables = validate(data)
    roots = table_names(names if names is not None else data.get("selected_tables", list(tables)))
    tables = subset(tables, roots, setting_keys)
    mapped = {}
    visiting = set()
    indexes = {n: {r[pk(TABLES[n]).name]: r for r in rows} for n, rows in tables.items()}
    counts = {n: {"added": 0, "existing": 0, "runtime_skipped": 0} for n in tables}

    async def add(name, old_id):
        key = (name, old_id)
        if key in mapped:
            return mapped[key]
        if key in visiting:
            raise ValueError("Backup contains cyclic references.")
        visiting.add(key)
        table = TABLES[name]
        row = dict(indexes[name][old_id])
        if name == "active_streams":
            counts[name]["runtime_skipped"] += 1
            mapped[key] = None
            visiting.remove(key)
            return None
        for col, parent, ref in references(name, row):
            row[col] = await add(parent, ref)
        identity = IDENTITIES[name]
        # Unnamed VOD/series playlist rows are identified by their primary source.
        if name in ("vod_playlist", "serie_playlist") and not row.get("custom_name"):
            identity = ("vod_source_id" if name == "vod_playlist" else "serie_source_id",)
        matches = (await db.execute(select(pk(table)).where(*[
            table.c[c] == row[c] for c in identity]).limit(2))).scalars().all()
        if len(matches) > 1:
            raise ValueError(f"{name}: ambiguous existing identity; resolve duplicates before restoring.")
        if matches:
            new_id = matches[0]
            counts[name]["existing"] += 1
        else:
            if name != "settings":
                row.pop(pk(table).name)
            result = await db.execute(table.insert().values(**row).returning(pk(table)))
            new_id = result.scalar_one()
            counts[name]["added"] += 1
        mapped[key] = new_id
        visiting.remove(key)
        return new_id

    for name, rows in indexes.items():
        for ident in rows:
            await add(name, ident)
    return {"tables": counts, "added": sum(c["added"] for c in counts.values()),
            "existing": sum(c["existing"] for c in counts.values()),
            "runtime_skipped": sum(c["runtime_skipped"] for c in counts.values())}


def deletion_filters(names, setting_keys=None):
    """Build row-level cascade predicates, including the non-FK area references.

    EPG channels have a restrictive FK; explicitly remove them with their source.
    SET NULL children stay in place and are reported separately by the router.
    """
    filters = {n: true() for n in table_names(names)}
    if setting_keys is not None and "settings" in filters:
        filters["settings"] = TABLES["settings"].c.key.in_(setting_keys)
    order = list(Base.metadata.sorted_tables)
    # Area exceptions have an implicit dependency on all four playlist tables.
    order = [t for t in order if t.name != "area_item_templates"] + [TABLES["area_item_templates"]]
    nulls = {}
    for table in order:
        clauses = []
        for fk in table.foreign_keys:
            parent = fk.column.table
            if parent.name not in filters:
                continue
            affected = fk.parent.in_(select(pk(parent)).where(filters[parent.name]))
            if fk.ondelete == "SET NULL":
                nulls.setdefault(table.name, []).append(affected)
            else:
                clauses.append(affected)
        if table.name == "area_item_templates":
            for kind, parent_name in PLAYLISTS.items():
                if parent_name in filters:
                    parent = TABLES[parent_name]
                    clauses.append((table.c.kind == kind) & table.c.playlist_id.in_(
                        select(pk(parent)).where(filters[parent_name])))
        if clauses:
            filters[table.name] = or_(*([filters[table.name]] if table.name in filters else []), *clauses)
    return order, filters, nulls
