"""
Background MAC health + multi-MAC genre comparison.

Two related jobs live here because both answer the same operator question:
"I have several MACs on this portal — are they all still good, and do they
even see the same catalogue?"

  1. **Status refresh** (`refresh_all_macs` / `mac_health_scheduler`)
     Walks every enabled portal and handshakes *every* of its MACs, writing
     `status` / `online` / `expire_date` / `last_checked`. The manual
     *Test portal* button already does this; the scheduler keeps the badges
     honest overnight without anyone clicking. MACs that are currently holding
     a live stream are skipped so we never kick a viewer off the box.

  2. **Genre comparison** (`compare_genres`)
     Asks each online MAC of a portal for its live/VOD/series genre lists and
     reports what is common vs only-on-this-MAC. Used by the Portals UI when
     the operator wants to know whether a "secondary" MAC is actually a
     different package (common with shared-login resellers).

  3. **Channel-id comparison** (`compare_genres` second phase, requested via
     `channel_kinds`) — runs ONLY when the genre phase found ≥2 distinct
     packages. Asks each compared MAC for the channels of the portal's
     *enabled* genres and diffs them by normalized name: same channel name
     with a different `id` on another MAC is the case that breaks
     primary/fallback chains (one stored cmd, two id spaces). Live uses
     `get_all_channels` (1 request/MAC where supported); VOD/series page per
     enabled genre and are opt-in from the GUI.

The genre comparison does not fetch source items, but it upserts the discovered
genre union so the matrix can enable/disable those rows without losing
package-only categories when the dialog closes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from sqlalchemy import select

from ..database import SessionLocal
from ..models import LiveGenre, MacAddress, Portal, SerieGenre, VodGenre
from ..portal.account import mac_status
from ..portal.client import (RATE_LIMITED_CODE, PortalError,
                             status_for_error)
from ..portal.pool import POOL, PortalSession
from ..portal.resolver import resolve_portal
from .portal_pace import pace_for_playback
from .db_logging import db_log
from .runtime_settings import fetch_page_budget, get_setting
from .stream_manager import MANAGER
from .channel_translations import count_translations

log = logging.getLogger("spm.mac_health")

# How long between full sweeps when the GUI has not set a preference.
DEFAULT_INTERVAL_MIN = 60
# Never hammer a panel harder than this, even if the setting is "1 minute".
MIN_INTERVAL_MIN = 5
# Soft per-MAC budget so a slow panel cannot stall the whole sweep forever.
MAC_TIMEOUT_S = 25.0

# Channel-id second phase (option B): a MAC may have to handshake plus walk
# get_all_channels and/or every enabled genre's pages — 25 s is a genre sweep.
CHANNEL_MAC_TIMEOUT_S = float(os.environ.get("SPM_COMPARE_CHANNEL_TIMEOUT", "120"))
# Rows per kind returned to the GUI. Counts are always exact; only the row
# LIST is capped (diff rows are kept first — they are the actionable signal).
CHANNEL_ROW_CAP = int(os.environ.get("SPM_COMPARE_CHANNEL_ROWS", "2000"))
# Content kinds the channel-id scan understands.
CHANNEL_KINDS = ("live", "vod", "series")


def _norm_genre_name(name: str | None) -> str:
    return " ".join((name or "").casefold().split())


def _genre_key(g: dict) -> str:
    """Stable identity of a genre across MACs: portal id first, name as fallback.

    Live uses `get_genres` (dict keyed by id → often just a title string we
    already normalized to `{id, title}`); VOD/series use `get_categories`
    (list of `{id, title}` or `{id, name, alias}`). Both shapes land here.
    """
    gid = str(g.get("id") or g.get("cid") or g.get("category_id")
              or g.get("genre_portal_id") or "").strip()
    if gid and gid not in ("*", "0", "-1", "null", "None"):
        return f"id:{gid}"
    return f"name:{_norm_genre_name(g.get('title') or g.get('name') or g.get('alias') or '')}"


def _genre_label(g: dict) -> str:
    return (g.get("title") or g.get("name") or g.get("alias")
            or g.get("id") or "?").strip() or "?"


async def _portal_url(p: Portal, first_mac: MacAddress | None) -> str | None:
    if p.resolved_url:
        return p.resolved_url
    res = await resolve_portal(p.base_url, mac=first_mac.mac if first_mac else None,
                               proxy=p.proxy_url, tls_insecure=p.tls_insecure)
    if not res.ok:
        return None
    return res.portal_url


def _busy_mac_ids() -> set[int]:
    """mac_ids currently occupied — ffmpeg pipes AND recent redirect leases.

    Redirect/direct plays 302 the player to the panel CDN, so there is no
    StreamHandle and no mac_lock afterwards. Without the lease those MACs would
    look free and a health handshake mid-play could kick the viewer. See
    ``StreamManager.is_mac_busy`` / ``lease_mac``.
    """
    try:
        return set(MANAGER.busy_mac_ids())
    except Exception:  # noqa: BLE001 - a registry glitch must not abort the sweep
        return set()


async def refresh_mac(portal: Portal, mac: MacAddress, *, url: str) -> dict:
    """Handshake + account_info for one MAC; persist status/expiry. No commit."""
    client = await POOL.get(PortalSession.from_rows(portal, mac, portal_url=url))
    code = ""
    detail = ""
    try:
        async with asyncio.timeout(MAC_TIMEOUT_S):
            await client.handshake()
            verdict = await client.refresh_account()
        mac.expire_date = verdict.expire_date
        mac.force_ch_link_check = verdict.force_ch_link_check
        mac.status, mac.online = mac_status(verdict), verdict.online
        mac.last_error = None if mac.online else (verdict.reason or "")[:200]
        if mac.online:
            mac.fail_count = 0
        detail = verdict.reason
        if not mac.online:
            code = verdict.status
    except PortalError as exc:
        code = exc.code
        if exc.code == RATE_LIMITED_CODE:
            # A portal-wide pause says nothing about this MAC: the panel is
            # refusing *traffic*, not this account. Demoting it (or bumping its
            # fail_count) would take a healthy MAC out of the pool for a day
            # because somebody else's play tripped a rate limiter.
            detail = exc.detail()
            mac.last_error = detail[:200]
            mac.last_checked = datetime.now(timezone.utc)
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
            return {"mac": mac.mac, "status": mac.status, "online": mac.online,
                    "expire_date": mac.expire_date, "code": code,
                    "detail": detail, "skipped": "portal rate limited",
                    "last_checked": mac.last_checked.isoformat()
                    if mac.last_checked else None}
        mac.status = status_for_error(exc)
        mac.online = False
        mac.fail_count = (mac.fail_count or 0) + 1
        detail = exc.detail()
        mac.last_error = detail[:200]
    except TimeoutError:
        code = "timeout"
        mac.status = "offline"
        mac.online = False
        mac.fail_count = (mac.fail_count or 0) + 1
        detail = f"no answer within {MAC_TIMEOUT_S:.0f}s"
        mac.last_error = detail
    except Exception as exc:  # noqa: BLE001
        code = type(exc).__name__
        mac.status = "error"
        mac.online = False
        mac.fail_count = (mac.fail_count or 0) + 1
        detail = f"{type(exc).__name__}: {exc}"
        mac.last_error = detail[:200]
    finally:
        mac.last_checked = datetime.now(timezone.utc)
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return {"mac": mac.mac, "status": mac.status, "online": mac.online,
            "expire_date": mac.expire_date, "code": code or None, "detail": detail,
            "last_checked": mac.last_checked.isoformat() if mac.last_checked else None}


async def refresh_portal_macs(portal_id: int, *, skip_busy: bool = True) -> dict:
    """Refresh every MAC of one portal. Returns a small report."""
    async with SessionLocal() as s:
        p = await s.get(Portal, portal_id)
        if not p:
            return {"ok": False, "error": "portal not found"}
        macs = list((await s.execute(select(MacAddress).where(MacAddress.portal_id == portal_id)
                                     .order_by(MacAddress.order))).scalars().all())
        if not macs:
            return {"ok": True, "portal_id": portal_id, "name": p.name,
                    "results": [], "skipped_busy": 0}
        url = await _portal_url(p, macs[0])
        if not url:
            now = datetime.now(timezone.utc)
            for m in macs:
                m.status, m.online = "offline", False
                m.last_checked = now
                m.last_error = "portal URL could not be resolved"
            await s.commit()
            return {"ok": False, "portal_id": portal_id, "name": p.name,
                    "error": "resolve failed",
                    "results": [{"mac": m.mac, "status": "offline", "online": False,
                                 "detail": "resolve failed"} for m in macs]}

        # Persist a newly resolved URL so the next sweep skips discovery.
        if not p.resolved_url:
            p.resolved_url = url

        busy = _busy_mac_ids() if skip_busy else set()
        results = []
        skipped = 0
        for m in macs:
            # A health sweep is cheap per MAC and there may be many: while a
            # play is running on this portal, the sweep takes its time instead
            # of competing for the panel's connection budget.
            await pace_for_playback(portal_id)
            if m.id in busy:
                skipped += 1
                results.append({"mac": m.mac, "status": m.status, "online": m.online,
                                "expire_date": m.expire_date, "skipped": "busy",
                                "detail": "holding an active stream (ffmpeg or recent redirect) — left alone"})
                continue
            results.append(await refresh_mac(p, m, url=url))
        await s.commit()
        return {"ok": True, "portal_id": portal_id, "name": p.name,
                "results": results, "skipped_busy": skipped}


async def refresh_all_macs(*, only_multi: bool = True, skip_busy: bool = True) -> dict:
    """Sweep every enabled portal (optionally only those with ≥2 MACs)."""
    async with SessionLocal() as s:
        portals = list((await s.execute(
            select(Portal).where(Portal.enabled.is_(True)).order_by(Portal.name)
        )).scalars().all())
        counts = {}
        for p in portals:
            n = len((await s.execute(select(MacAddress.id).where(
                MacAddress.portal_id == p.id))).scalars().all())
            counts[p.id] = n
        targets = [p for p in portals if (counts.get(p.id, 0) >= 2 if only_multi
                                          else counts.get(p.id, 0) >= 1)]

    reports = []
    changed = 0
    for p in targets:
        try:
            rep = await refresh_portal_macs(p.id, skip_busy=skip_busy)
        except Exception as exc:  # noqa: BLE001 - one portal must not abort the sweep
            log.exception("mac health sweep failed for portal %s", p.id)
            rep = {"ok": False, "portal_id": p.id, "name": p.name, "error": str(exc)}
        reports.append(rep)
        for r in rep.get("results") or []:
            if r.get("status") in ("expired", "banned", "offline", "error", "unauthorized"):
                changed += 1

    online = sum(1 for rep in reports for r in (rep.get("results") or []) if r.get("online"))
    total = sum(len(rep.get("results") or []) for rep in reports)
    await db_log("INFO", "portal",
                 f"mac health sweep: {len(targets)} portal(s), {online}/{total} MAC(s) online"
                 + (f", {changed} not usable" if changed else ""))
    return {"ok": True, "portals": len(targets), "macs_online": online,
            "macs_total": total, "reports": reports}


async def mac_health_scheduler(interval_s: float | None = None) -> None:
    """Background loop: keep multi-MAC portals' status/expiry fresh.

    Interval comes from the GUI setting `mac_health_minutes` (default 60).
    Set it to 0 to pause the scheduler without a restart.
    """
    # First pass a little after boot so startup work finishes first.
    await asyncio.sleep(45)
    while True:
        try:
            minutes = int(await get_setting("mac_health_minutes", DEFAULT_INTERVAL_MIN)
                          or DEFAULT_INTERVAL_MIN)
            if minutes <= 0:
                await asyncio.sleep(60)
                continue
            minutes = max(MIN_INTERVAL_MIN, minutes)
            only_multi = bool(await get_setting("mac_health_multi_only", True))
            t0 = time.monotonic()
            await refresh_all_macs(only_multi=only_multi, skip_busy=True)
            spent = time.monotonic() - t0
            wait = max(30.0, minutes * 60 - spent) if interval_s is None else interval_s
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - scheduler must not die
            log.exception("mac health scheduler tick failed")
            await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Genre comparison across MACs of the same portal
# ---------------------------------------------------------------------------
async def _genres_for_mac(portal: Portal, mac: MacAddress, url: str) -> dict:
    """Fetch live/vod/series genre lists through one MAC. Never raises."""
    out = {"mac": mac.mac, "mac_id": mac.id, "ok": False, "error": "",
           "live": [], "vod": [], "series": []}
    client = await POOL.get(PortalSession.from_rows(portal, mac, portal_url=url))
    try:
        async with asyncio.timeout(MAC_TIMEOUT_S * 2):
            await client.ensure_auth()
            live = await client.live_genres()
            try:
                vod = await client.vod_genres()
            except Exception as exc:  # noqa: BLE001
                vod, out["vod_error"] = [], f"{type(exc).__name__}: {exc}"
            try:
                series = await client.series_genres()
            except Exception as exc:  # noqa: BLE001
                series, out["series_error"] = [], f"{type(exc).__name__}: {exc}"
        out["live"] = [{"key": _genre_key(g), "name": _genre_label(g),
                        "id": str(g.get("id") or "")} for g in live]
        out["vod"] = [{"key": _genre_key(g), "name": _genre_label(g),
                       "id": str(g.get("id") or "")} for g in vod]
        out["series"] = [{"key": _genre_key(g), "name": _genre_label(g),
                          "id": str(g.get("id") or "")} for g in series]
        out["ok"] = True
    except PortalError as exc:
        out["error"] = exc.detail()
    except TimeoutError:
        out["error"] = f"no answer within {MAC_TIMEOUT_S * 2:.0f}s"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return out


async def _enabled_genre_ids(portal_id: int) -> dict[str, list[tuple[str, str]]]:
    """{kind: [(genre_portal_id, name), …]} for genres the operator enabled.

    The channel-id scan only covers enabled genres: those are the rows that
    feed LiveSource and thus the chains a mismatched id can break.
    """
    out: dict[str, list[tuple[str, str]]] = {"live": [], "vod": [], "series": []}
    async with SessionLocal() as s:
        for kind, model in (("live", LiveGenre), ("vod", VodGenre), ("series", SerieGenre)):
            rows = (await s.execute(select(model).where(
                model.portal_id == portal_id, model.enabled.is_(True))
                .order_by(model.genre_portal_id))).scalars().all()
            out[kind] = [(r.genre_portal_id, r.name) for r in rows]
    return out


def _norm_channel_name(name: str | None) -> str:
    """Channel identity across MACs: casefolded, whitespace-collapsed name."""
    return " ".join((name or "").casefold().split())


def _resolve_enabled_for_mac(enabled: list[tuple[str, str]],
                             mac_genres: list[dict]) -> list[str]:
    """Map the portal's enabled genres onto ONE MAC's view of the genre list.

    Packages can renumber genre ids, so matching only on the stored id would
    fetch the wrong category (or nothing) on the other MAC. Match id first —
    the common case — then normalized name, and drop genres this MAC cannot
    see at all (they are a package difference the genre phase already shows).
    """
    by_id = {str(g.get("id") or ""): g for g in mac_genres}
    by_name = {_norm_channel_name(g.get("name")): g for g in mac_genres}
    out: list[str] = []
    for gid, gname in enabled:
        hit = by_id.get(str(gid)) or by_name.get(_norm_channel_name(gname))
        if hit is not None:
            mid = str(hit.get("id") or "").strip()
            if mid and mid not in ("*", "0", "-1") and mid not in out:
                out.append(mid)
    return out


async def _channels_for_mac(portal: Portal, mac: MacAddress, url: str,
                            kinds: list[str],
                            genre_ids: dict[str, list[str]],
                            budget: int) -> dict:
    """Fetch channel rows of the *enabled* genres through one MAC. Never raises.

    live  → get_all_channels when the panel supports it (split by tv_genre_id
            etc. locally), else paged get_ordered_list per enabled genre.
    vod/series → paged get_ordered_list per enabled genre, capped at `budget`
            pages like a normal fetch (SPM_FETCH_PAGE_BUDGET).
    Returns {mac, mac_id, ok, error, live: [rows], …, <kind>_error}.
    """
    out: dict = {"mac": mac.mac, "mac_id": mac.id, "ok": False, "error": ""}
    for kind in CHANNEL_KINDS:
        out[kind] = []
    client = await POOL.get(PortalSession.from_rows(portal, mac, portal_url=url))
    try:
        async with asyncio.timeout(CHANNEL_MAC_TIMEOUT_S):
            await client.ensure_auth()

            async def fetch_pages(fetch_page, gid: str, kind: str) -> None:
                """Page one genre up to `budget`; rows land in out[kind]."""
                page = 1
                while page <= budget:
                    if page > 1:
                        # A deep VOD scan must not starve a play that starts
                        # mid-compare — same rule as the content fetch.
                        await pace_for_playback(portal.id)
                    data = await fetch_page(gid, page)
                    for item in data.items or []:
                        _channel_row(out, kind, item, gid)
                    if len(data.items or []) < 14:   # short page = end (see fetch_jobs)
                        return
                    page += 1

            if "live" in kinds and genre_ids.get("live"):
                live_ids = set(genre_ids["live"])
                try:
                    complete = await client.all_channels()
                except Exception:  # noqa: BLE001 - panel without get_all_channels
                    complete = []
                if complete:
                    for item in complete:
                        gid = _live_genre_id(item)
                        if gid in live_ids:
                            _channel_row(out, "live", item, gid)
                else:
                    for gid in genre_ids["live"]:
                        await fetch_pages(client.live_channels, gid, "live")

            for kind in ("vod", "series"):
                if kind not in kinds or not genre_ids.get(kind):
                    continue
                try:
                    fetch_page = client.vod_list if kind == "vod" else client.series_list
                    for gid in genre_ids[kind]:
                        await fetch_pages(fetch_page, gid, kind)
                except PortalError as exc:
                    out[f"{kind}_error"] = exc.detail()
                except TimeoutError:
                    out[f"{kind}_error"] = f"no answer within {CHANNEL_MAC_TIMEOUT_S:.0f}s"
                except Exception as exc:  # noqa: BLE001
                    out[f"{kind}_error"] = f"{type(exc).__name__}: {exc}"
            out["ok"] = True
    except PortalError as exc:
        out["error"] = exc.detail()
    except TimeoutError:
        out["error"] = f"no answer within {CHANNEL_MAC_TIMEOUT_S:.0f}s"
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass
    return out


def _live_genre_id(item: dict) -> str:
    """Portal genre id of a live row — same field sniffing as fetch_jobs."""
    for key in ("tv_genre_id", "genre_id", "genre", "tv_genre"):
        val = item.get(key)
        if val not in (None, ""):
            return str(val)
    return ""


def _channel_row(out: dict, kind: str, item: dict, gid: str) -> None:
    """Normalize one portal channel/item row into a diffable row."""
    from .titles import portal_item_title

    cid = str(item.get("id") or "").strip()
    if not cid or cid in ("*", "0", "-1"):
        return
    if kind == "live":
        name = str(item.get("name") or "").strip() or "?"
    else:
        # vod_list/series_list already split movies vs series (client.py).
        name = portal_item_title(item, limit=300)
    out[kind].append({
        "id": cid,
        "name": name,
        "key": _norm_channel_name(name),
        "genre": gid,
        "cmd": str(item.get("cmd") or "")[:400],
    })


def _diff_channel_ids(per_mac: list[dict], kind: str) -> dict:
    """Compare one kind's channel rows across MACs by normalized name.

    Classification per name key:
      * ``same``    — every MAC that returned the kind has it, one id
      * ``differ``  — same name, ≥2 distinct ids  ← breaks fallback chains
      * ``missing`` — present on ≥1 MAC, absent on another
    Only ``differ``/``missing`` rows are returned (counts are exact; the row
    list is capped with diff rows kept first).
    """
    ok_macs = [b for b in per_mac
               if b.get("ok") and not b.get(f"{kind}_error")
               and not b.get("no_genres")
               and (b.get(kind) is not None)]
    # name key -> {mac: row}
    table: dict[str, dict[str, dict]] = {}
    labels: dict[str, str] = {}
    for block in ok_macs:
        for row in block.get(kind) or []:
            key = row.get("key") or _norm_channel_name(row.get("name"))
            if not key:
                continue
            table.setdefault(key, {})[block["mac"]] = row
            labels.setdefault(key, row.get("name") or key)

    mac_list = [b["mac"] for b in ok_macs]
    counts = {"same": 0, "differ": 0, "missing": 0}
    rows: list[dict] = []
    for key, by_mac in table.items():
        ids = {r["id"] for r in by_mac.values()}
        absent = [m for m in mac_list if m not in by_mac]
        if len(ids) > 1:
            status = "differ"
        elif absent:
            status = "missing"
        else:
            status = "same"
        counts[status] += 1
        if status == "same":
            continue
        rows.append({
            "name": labels[key],
            "key": key,
            "status": status,
            "ids": {m: r["id"] for m, r in by_mac.items()},
            "cmds": {m: r["cmd"] for m, r in by_mac.items() if r.get("cmd")},
            "genres": {m: r["genre"] for m, r in by_mac.items()},
            "absent": absent,
        })
    # Actionable signal first, then stable name order; cap the list only.
    rows.sort(key=lambda r: (0 if r["status"] == "differ" else 1, r["name"].casefold()))
    truncated = len(rows) > CHANNEL_ROW_CAP
    if truncated:
        rows = rows[:CHANNEL_ROW_CAP]
    failed: dict[str, str] = {}
    for b in per_mac:
        if b in ok_macs:
            continue
        if b.get("no_genres"):
            failed[b["mac"]] = "none of the enabled genres are in this MAC's package"
        elif b.get(f"{kind}_error") or b.get("error"):
            failed[b["mac"]] = b.get(f"{kind}_error") or b.get("error")
    return {
        "kind": kind,
        "macs": mac_list,
        "counts": counts,
        "rows": rows,
        "total": sum(counts.values()),
        "truncated": truncated,
        "failed": failed,
        "identical": counts["differ"] == 0 and counts["missing"] == 0,
    }


def _diff_kind(per_mac: list[dict], kind: str) -> dict:
    """Build common / only-on-MAC sets for one content kind."""
    sets: dict[str, set[str]] = {}
    labels: dict[str, str] = {}
    for block in per_mac:
        if not block.get("ok") or block.get(f"{kind}_error"):
            continue
        keys = set()
        for g in block.get(kind) or []:
            keys.add(g["key"])
            labels.setdefault(g["key"], g["name"])
        sets[block["mac"]] = keys

    if not sets:
        return {"common": [], "only": {}, "counts": {}, "identical": True,
                "macs_compared": 0}

    common_keys = set.intersection(*sets.values()) if sets else set()
    only = {mac: sorted(({"key": k, "name": labels.get(k, k)} for k in keys - common_keys),
                        key=lambda x: x["name"].casefold())
            for mac, keys in sets.items()}
    # Drop empty only-lists so the GUI can say "identical" cleanly.
    only = {m: v for m, v in only.items() if v}
    return {
        "common": sorted(({"key": k, "name": labels.get(k, k)} for k in common_keys),
                         key=lambda x: x["name"].casefold()),
        "only": only,
        "counts": {mac: len(keys) for mac, keys in sets.items()},
        "identical": not only,
        "macs_compared": len(sets),
    }


async def _persist_mac_genre_counts(per_mac: list[dict]) -> int:
    """Store successful per-kind counts without erasing older partial results."""
    updated = 0
    compared_at = datetime.now(timezone.utc)
    async with SessionLocal() as s:
        for block in per_mac:
            if not block.get("ok") or not block.get("mac_id"):
                continue
            mac = await s.get(MacAddress, int(block["mac_id"]))
            if mac is None:
                continue
            wrote = False
            for kind in ("live", "vod", "series"):
                if not block.get(f"{kind}_error"):
                    setattr(mac, f"genre_count_{kind}", len(block.get(kind) or []))
                    wrote = True
            if wrote:
                mac.genres_compared_at = compared_at
                updated += 1
        await s.commit()
    return updated


async def _persist_genres_from_compare(portal_id: int, per_mac: list[dict]) -> dict:
    """
    Union every successfully fetched genre list into the portal's genre tables.

    Same upsert rules as a normal fetch (`_sync_genres`): existing rows keep
    their `enabled` flag; brand-new genres land disabled so the operator still
    has to opt them into the catalogue. Nothing is deleted — a genre only one
    MAC sees is still a genre the portal offers, and wiping it would drop a
    user enablement the next compare would just re-add.
    """
    from .fetch_jobs import _sync_genres

    # Rebuild the raw portal-shaped dicts the sync helper expects.
    by_kind: dict[str, dict[str, dict]] = {"live": {}, "vod": {}, "series": {}}
    for block in per_mac:
        if not block.get("ok"):
            continue
        for kind in ("live", "vod", "series"):
            for g in block.get(kind) or []:
                gid = str(g.get("id") or "").strip()
                if not gid or gid in ("*", "0", "-1"):
                    # Name-only genres (rare) — invent a stable key from the name
                    # so they can still land in the table without colliding.
                    name = (g.get("name") or "").strip()
                    if not name:
                        continue
                    gid = f"name:{_norm_genre_name(name)}"[:40]
                by_kind[kind].setdefault(gid, {"id": gid, "title": g.get("name") or gid})

    stored = {"live": 0, "vod": 0, "series": 0}
    async with SessionLocal() as s:
        for kind, model in (("live", LiveGenre), ("vod", VodGenre), ("series", SerieGenre)):
            incoming = list(by_kind[kind].values())
            if not incoming:
                continue
            known = await _sync_genres(s, model, portal_id, incoming)
            stored[kind] = len(known)
        await s.commit()
    return stored


async def compare_genres(portal_id: int, mac_ids: list[int] | None = None,
                         channel_kinds: list[str] | None = None) -> dict:
    """
    Ask selected (or, for legacy callers, every) online MAC for genre lists.

    Offline / expired / banned MACs are listed but not asked — their package
    is unknown, and a handshake failure is already visible on the status badge.

    Every genre any compared MAC returned is **upserted** into the portal's
    live/vod/series genre tables (enabled flags preserved). That way a
    secondary package's categories are not lost the moment the modal closes.

    `channel_kinds` (subset of live/vod/series) additionally runs the
    channel-id second phase, but ONLY when the genre phase found ≥2 distinct
    packages: same channel name with a different id across MACs is what breaks
    a shared primary/fallback chain, and on identical packages there is nothing
    to warn about. None/empty = genre-only compare (legacy callers). The phase
    covers the portal's *enabled* genres only.
    """
    async with SessionLocal() as s:
        p = await s.get(Portal, portal_id)
        if not p:
            return {"ok": False, "error": "portal not found"}
        macs = list((await s.execute(select(MacAddress).where(MacAddress.portal_id == portal_id)
                                     .order_by(MacAddress.order))).scalars().all())
        name = p.name

    if len(macs) < 2:
        return {"ok": True, "portal_id": portal_id, "name": name,
                "macs": [{"mac": m.mac, "status": m.status, "online": m.online}
                         for m in macs],
                "compared": 0, "skipped": len(macs),
                "identical": True, "message": "portal has fewer than 2 MACs — nothing to compare",
                "live": {"identical": True}, "vod": {"identical": True},
                "series": {"identical": True}, "channel_ids": None,
                "stored": {"live": 0, "vod": 0, "series": 0}}

    url = await _portal_url(p, macs[0])
    if not url:
        return {"ok": False, "portal_id": portal_id, "name": name,
                "error": "portal URL could not be resolved"}

    selected = {int(i) for i in (mac_ids or []) if str(i).isdigit()}
    candidates = [m for m in macs if not selected or m.id in selected]
    if selected and len(candidates) < 2:
        return {"ok": False, "portal_id": portal_id, "name": name,
                "error": "select at least two MAC addresses from this portal"}
    usable = [m for m in candidates
              if m.online and m.status not in ("expired", "banned")]
    skipped = [{"mac": m.mac, "mac_id": m.id, "status": m.status, "online": m.online,
                "reason": "not online — refresh status first"}
               for m in candidates if m not in usable]
    if selected and len(usable) < 2:
        return {"ok": False, "portal_id": portal_id, "name": name,
                "error": "fewer than two selected MACs are online and usable"}

    # Selected comparisons are an operator action, but do not hammer a portal
    # without bound when it has many accounts.
    gate = asyncio.Semaphore(4)

    async def fetch_one(mac):
        async with gate:
            return await _genres_for_mac(p, mac, url)

    per_mac = list(await asyncio.gather(*(fetch_one(m) for m in usable)))

    live = _diff_kind(per_mac, "live")
    vod = _diff_kind(per_mac, "vod")
    series = _diff_kind(per_mac, "series")
    identical = live["identical"] and vod["identical"] and series["identical"]
    failed = [b for b in per_mac if not b.get("ok") or any(
        b.get(f"{kind}_error") for kind in ("live", "vod", "series"))]

    # Persist both the package summary on each MAC and the union used by the
    # portal genre editor. Either write may fail without losing the report.
    stored = {"live": 0, "vod": 0, "series": 0}
    counts_stored = 0
    try:
        if any(b.get("ok") for b in per_mac):
            counts_stored = await _persist_mac_genre_counts(per_mac)
            stored = await _persist_genres_from_compare(portal_id, per_mac)
    except Exception:  # noqa: BLE001 - compare report must still return
        log.exception("genre persist after compare failed for portal %s", portal_id)

    # Compact per-MAC summary for the GUI table.
    mac_summary = []
    for b in per_mac:
        mac_summary.append({
            "mac": b["mac"], "ok": b["ok"], "error": b.get("error") or "",
            "live": len(b.get("live") or []), "vod": len(b.get("vod") or []),
            "series": len(b.get("series") or []),
            "only_live": len((live.get("only") or {}).get(b["mac"]) or []),
            "only_vod": len((vod.get("only") or {}).get(b["mac"]) or []),
            "only_series": len((series.get("only") or {}).get(b["mac"]) or []),
        })

    signatures: dict[tuple, list[dict]] = {}
    for block in per_mac:
        signature = tuple(
            (kind, str(block.get(f"{kind}_error") or ""),
             tuple(sorted(g["key"] for g in (block.get(kind) or []))))
            for kind in ("live", "vod", "series")
        ) if block.get("ok") else (("failed", block.get("error") or ""),)
        signatures.setdefault(signature, []).append(block)
    packages = [{
        "id": index + 1,
        "name": f"Package {chr(65 + index) if index < 26 else index + 1}",
        "mac_ids": [b["mac_id"] for b in blocks],
        "macs": [b["mac"] for b in blocks],
        "counts": {kind: len(blocks[0].get(kind) or [])
                   for kind in ("live", "vod", "series")},
        "ok": all(b.get("ok") for b in blocks),
    } for index, blocks in enumerate(signatures.values())]

    # ---- channel-id second phase (option B) ------------------------------
    # Only when packages actually differ AND the caller asked for it. The GUI
    # always requests at least live; None/empty means genre-only (legacy).
    # Every failure is recorded per kind — a refused get_all_channels must not
    # lose the genre report already in hand.
    kinds = [k for k in dict.fromkeys(channel_kinds or []) if k in CHANNEL_KINDS]
    channel_ids = None
    ok_packages = [p for p in packages if p.get("ok")]
    if kinds and len(ok_packages) > 1:
        enabled = await _enabled_genre_ids(portal_id)
        if any(enabled.get(k) for k in kinds):
            budget = await fetch_page_budget()
            # Phase-1 genre lists, keyed by MAC: packages may renumber genre
            # ids, so each MAC gets the enabled set resolved against ITS list.
            genres_by_mac = {b["mac"]: b for b in per_mac if b.get("ok")}

            async def fetch_channels_one(mac):
                view = genres_by_mac.get(mac.mac) or {}
                gids = {k: _resolve_enabled_for_mac(
                    enabled.get(k) or [], view.get(k) or []) for k in kinds}
                if not any(gids.values()):
                    return {"mac": mac.mac, "mac_id": mac.id, "ok": True,
                            "error": "", "no_genres": True,
                            **{k: [] for k in CHANNEL_KINDS}}
                async with gate:
                    return await _channels_for_mac(p, mac, url, kinds, gids, budget)
            ch_per_mac = list(await asyncio.gather(
                *(fetch_channels_one(m) for m in usable)))
            channel_ids = {
                "requested": kinds,
                "enabled": {k: len(enabled.get(k) or []) for k in kinds},
                "kinds": {k: _diff_channel_ids(ch_per_mac, k)
                          for k in kinds},
                "results": ch_per_mac,
            }
            bits = []
            for k in kinds:
                d = channel_ids["kinds"][k]
                if d["counts"]["differ"]:
                    bits.append(f"{k}: {d['counts']['differ']} channel(s) with different ids")
                if d["counts"]["missing"]:
                    bits.append(f"{k}: {d['counts']['missing']} channel(s) missing somewhere")
                if d["failed"]:
                    bits.append(f"{k}: {len(d['failed'])} MAC(s) could not be listed")
            if bits:
                msg_tail = "; ".join(bits)
                channel_ids["message"] = msg_tail
                await db_log("WARNING", "portal",
                             f"[{name}] channel-id compare: {msg_tail}")
        else:
            channel_ids = {"requested": kinds, "kinds": {}, "results": [],
                           "enabled": {k: 0 for k in kinds},
                           "message": "no enabled genres — enable genres first"}
    elif kinds:
        # Not enough distinct successful packages to diff against: either the
        # packages are genuinely identical, or MACs failed to answer.
        same = len(packages) == 1 and packages[0].get("ok")
        channel_ids = {"requested": kinds, "kinds": {}, "results": [],
                       "enabled": {},
                       "message": ("packages are identical — channel ids not compared"
                                   if same else
                                   "fewer than two usable packages — channel ids not compared")}

    if identical and not failed:
        msg = f"all {len(per_mac)} online MAC(s) see the same genres"
    elif identical and failed:
        msg = f"compared MACs match, but {len(failed)} MAC(s) could not be asked"
    else:
        bits = []
        for kind, d in (("live", live), ("vod", vod), ("series", series)):
            if not d.get("identical"):
                n = sum(len(v) for v in (d.get("only") or {}).values())
                bits.append(f"{kind}: {n} genre(s) only on some MACs")
        msg = "; ".join(bits) or "differences found"
    if any(stored.values()):
        msg += (f" · stored {stored['live']} live / {stored['vod']} vod / "
                f"{stored['series']} series genre(s)")
    if channel_ids and channel_ids.get("message"):
        msg += f" · {channel_ids['message']}"

    await db_log("INFO" if identical else "WARNING", "portal",
                 f"[{name}] genre compare: {msg}")
    # Seed the tab's "N saved for this portal" badge. The None-guard is the
    # contract: the legacy channel_kinds=[] answer must stay None (and the
    # offline / message-dict paths still get their count).
    if channel_ids is not None:
        channel_ids["translations"] = {"count": await count_translations(portal_id)}
    return {
        "ok": True, "portal_id": portal_id, "name": name,
        "compared": len(per_mac), "skipped": skipped,
        "identical": identical and not failed, "message": msg,
        "macs": mac_summary, "results": per_mac, "packages": packages,
        "live": live, "vod": vod, "series": series,
        "channel_ids": channel_ids,
        "stored": stored, "mac_counts_stored": counts_stored,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
