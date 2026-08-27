"""
Minimal TMDB lookup for the detail popups. Fully optional: without an API key
(Settings → tmdb_api_key) the detail popup simply hides the TMDB block.
"""

from __future__ import annotations

import json
import time

import httpx
from sqlalchemy import select

from ..database import SessionLocal
from ..models import Setting

_BASE = "https://api.themoviedb.org/3"
_IMG = "https://image.tmdb.org/t/p/w342"
_CACHE: dict[str, tuple[float, dict | None]] = {}
_CACHE_TTL = 3600


async def _api_key() -> str:
    async with SessionLocal() as s:
        row = await s.get(Setting, "tmdb_api_key")
        if not row or not row.value:
            return ""
        try:
            return str(json.loads(row.value)).strip()
        except (json.JSONDecodeError, TypeError):
            return str(row.value).strip()


async def tmdb_lookup(name: str, year: str | None = None, kind: str = "vod") -> dict | None:
    """kind: vod -> /search/movie, series -> /search/tv. None when no key/hit."""
    key = await _api_key()
    if not key or not name:
        return None
    cache_key = f"{kind}|{name.lower()}|{year or ''}"
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    what = "movie" if kind == "vod" else "tv"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{_BASE}/search/{what}",
                            params={"api_key": key, "query": name, "language": "en-US",
                                    **({"primary_release_year" if what == "movie" else "first_air_date_year": str(year)}
                                       if year and str(year).isdigit() else {})})
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                _CACHE[cache_key] = (time.time(), None)
                return None
            tid = results[0]["id"]
            r = await c.get(f"{_BASE}/{what}/{tid}",
                            params={"api_key": key, "append_to_response": "credits",
                                    "language": "en-US"})
            r.raise_for_status()
            d = r.json()
    except Exception:  # noqa: BLE001 - any network/shape hiccup -> no enrichment
        _CACHE[cache_key] = (time.time(), None)
        return None

    credits = d.get("credits") or {}
    crew = [x["name"] for x in credits.get("crew", []) if x.get("job") == "Director"]
    cast = [x["name"] for x in credits.get("cast", [])[:6]]
    out = {
        "tmdb_id": tid,
        "type": what,
        "title": d.get("title") or d.get("name"),
        "original_title": d.get("original_title") or d.get("original_name"),
        "overview": d.get("overview") or "",
        "tagline": d.get("tagline") or "",
        "vote_average": d.get("vote_average"),
        "release_date": d.get("release_date") or d.get("first_air_date"),
        "runtime_min": (d.get("runtime") or (d.get("episode_run_time") or [None])[0]),
        "status": d.get("status"),
        "genres": [g.get("name") for g in d.get("genres", [])],
        "poster": (_IMG + d["poster_path"]) if d.get("poster_path") else None,
        "director": ", ".join(crew[:3]) or None,
        "cast": cast,
        "seasons_count": d.get("number_of_seasons"),
        "episodes_count": d.get("number_of_episodes"),
        "homepage": d.get("homepage"),
    }
    _CACHE[cache_key] = (time.time(), out)
    return out
