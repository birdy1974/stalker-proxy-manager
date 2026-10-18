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

from sqlalchemy import select

from ..models import CatalogChange, utcnow

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


def aware_utc(value: datetime | None) -> datetime | None:
    """SQLite may return naive UTC datetimes for timezone-aware columns."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
