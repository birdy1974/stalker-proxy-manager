"""
tv-logo/tv-logos GitHub repo logo matcher (Phase 3).

The repo tree (one API call, ~thousands of PNG paths) is cached in a Setting
row. Channel names are fuzzy-matched against the normalized logo filenames of
the configured country directory and its "all"-quality siblings; hits are
served via raw.githubusercontent URLs (?raw=true equivalent).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from ..database import SessionLocal, get_db  # noqa: F401  (SessionLocal used below)
from ..models import LivePlaylist, Setting
from .db_logging import db_log
from .epg import norm_name

TREE_API = "https://api.github.com/repos/tv-logo/tv-logos/git/trees/main?recursive=1"
RAW_BASE = "https://raw.githubusercontent.com/tv-logo/tv-logos/main/"
INDEX_KEY = "logo_index"
CACHE_HOURS = 7 * 24


async def _get_setting(key: str, default=None):
    """Settings are stored JSON-encoded by /api/settings; decode leniently."""
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row is None or row.value in (None, ""):
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return row.value


async def _set_setting(key: str, value: str) -> None:
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row is None:
            s.add(Setting(key=key, value=value))
        else:
            row.value = value
        await s.commit()


async def _config():
    country = (await _get_setting("logo_country", "netherlands") or "netherlands").strip().strip("/")
    return country


async def refresh_index() -> dict:
    """Fetch the repo tree and build {normalized_filename: raw_url} for the
    configured country (plus `countries/all` as an international fallback)."""
    country = await _config()
    prefixes = [f"countries/{country}/", "countries/all/"]
    if country == "germany":
        prefixes.append("countries/austria/")
    from .http_client import outbound_client
    async with outbound_client(timeout=httpx.Timeout(45, connect=10)) as c:
        r = await c.get(TREE_API, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        tree = r.json().get("tree", [])
    idx: dict[str, str] = {}
    for ent in tree:
        p = ent.get("path", "")
        if not p.endswith(".png") or not any(p.startswith(px) for px in prefixes):
            continue
        base = p.rsplit("/", 1)[-1][:-4]
        n = norm_name(base)
        if not n or len(n) < 2:
            continue
        prefer_country = p.startswith(f"countries/{country}/")
        prev = idx.get(n)
        # country-specific directory wins over countries/all for a duplicate name
        if prev is None or (prefer_country and prev.startswith(RAW_BASE + "countries/all/")):
            idx[n] = RAW_BASE + p
    payload = {"fetched": datetime.now(timezone.utc).isoformat(),
               "country": country, "count": len(idx), "index": idx}
    await _set_setting(INDEX_KEY, json.dumps(payload))
    await db_log("INFO", "logos", f"tv-logos index refreshed: {len(idx)} logos for '{country}'")
    return {"ok": True, "count": len(idx), "country": country}


async def _index() -> dict:
    payload = await _get_setting(INDEX_KEY)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = None
    if isinstance(payload, dict):
        try:
            fetched = datetime.fromisoformat(payload["fetched"])
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - fetched < timedelta(hours=CACHE_HOURS) \
                    and payload.get("country") == await _config():
                return payload
        except Exception:  # noqa: BLE001 - corrupt cache -> refetch
            pass
    await refresh_index()
    payload = await _get_setting(INDEX_KEY)
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload if isinstance(payload, dict) else {}


async def suggest_logo(name: str) -> str | None:
    """Best single-logo hit for a channel name (exact-normalized, prefix, then
    substring), or None when nothing fits."""
    idx = (await _index()).get("index", {})
    want = norm_name(name)
    if not want:
        return None
    if want in idx:
        return idx[want]
    for n, url in idx.items():
        if n.startswith(want) or want.startswith(n):
            if min(len(n), len(want)) >= max(3, int(0.8 * max(len(n), len(want)))):
                return url
    return None


async def auto_logos() -> dict:
    """Match every live playlist channel without a user-set logo against the
    tv-logos index and write the best URL. Returns a report."""
    idx = (await _index()).get("index", {})
    if not idx:
        return {"ok": False, "error": "logo index empty"}
    changed = checked = 0
    async with SessionLocal() as s:
        items = (await s.execute(select(LivePlaylist))).scalars().all()
        for it in items:
            checked += 1
            want = norm_name(it.custom_name)
            if not want:
                continue
            url = idx.get(want)
            if not url:
                for n, u in idx.items():
                    if n.startswith(want) or want.startswith(n):
                        if min(len(n), len(want)) >= max(3, int(0.8 * max(len(n), len(want)))):
                            url = u
                            break
            if url and it.logo != url:
                it.logo = url
                changed += 1
        await s.commit()
    await db_log("INFO", "logos", f"auto-logo pass: {changed}/{checked} live channels got a logo")
    return {"ok": True, "checked": checked, "set": changed}
