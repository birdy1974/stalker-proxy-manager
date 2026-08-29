"""
Minimal TMDB lookup for the detail popups. Fully optional: without an API key
(Settings → tmdb_api_key) the detail popup simply hides the TMDB block.
"""

from __future__ import annotations

import json
import re
import time

import httpx
from sqlalchemy import select

from ..database import SessionLocal
from ..models import Setting

_BASE = "https://api.themoviedb.org/3"
_IMG = "https://image.tmdb.org/t/p/w342"
# Successes only; failures/errors are never cached, so a fixed API key or a
# transient network blip takes effect on the very next popup.
_CACHE: dict[str, tuple[float, dict]] = {}
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


def _clean_query(name: str) -> str:
    """Strip the noise panels append to titles so the search actually hits:
    trailing years, episode tags, resolution/quality tags and stray
    separators. 'Movie.Name (1999) HD' -> 'Movie Name'."""
    q = (name or "").strip()
    # order matters: strip episode/quality noise FIRST, then the (now trailing)
    # year, then flatten the leftover separators.
    q = re.sub(r"\bS\d{1,2}E\d{1,2}\b", "", q, flags=re.I)    # episode tag
    q = re.sub(r"\b(?:2160p|1080p|720p|4k|uhd|fhd|hd|sd|hevc|x264|x265)\b", "", q, flags=re.I)
    q = re.sub(r"[(\[]?(?:19|20)\d{2}[)\]]?\s*$", "", q)      # trailing year
    q = re.sub(r"[(\[]\s*[)\]]", "", q)                        # empty ()/[] leftovers
    q = re.sub(r"[._\-]+", " ", q)
    q = re.sub(r"\s+", " ", q).strip(" .-")
    return q


async def tmdb_lookup(name: str, year: str | None = None, kind: str = "vod") -> dict | None:
    """kind: vod -> /search/movie, series -> /search/tv.

    Return contract (the GUI distinguishes them):
      * None               -> no API key configured (nothing to do)
      * {"error": "..."}   -> a match/lookup problem worth surfacing to the user
      * {...}              -> enriched metadata
    """
    key = await _api_key()
    if not key:
        return None
    clean = _clean_query(name)
    if not clean:
        return {"error": "no searchable title"}
    # the key suffix participates in the cache key so a changed/added key
    # cannot keep serving results cached under an older (or broken) one
    cache_key = f"{key[-4:]}|{kind}|{clean.lower()}|{year or ''}"
    hit = _CACHE.get(cache_key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    what = "movie" if kind == "vod" else "tv"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"{_BASE}/search/{what}",
                            params={"api_key": key, "query": clean, "language": "en-US",
                                    **({"primary_release_year" if what == "movie" else "first_air_date_year": str(year)}
                                       if year and str(year).isdigit() else {})})
            if r.status_code in (401, 403):
                return {"error": "TMDB rejected the API key (HTTP "
                                 f"{r.status_code}) - re-check it in Settings"}
            r.raise_for_status()
            results = r.json().get("results") or []
            if not results:
                return {"error": f"no TMDB match for \"{clean}\""}
            tid = results[0]["id"]
            r = await c.get(f"{_BASE}/{what}/{tid}",
                            params={"api_key": key, "append_to_response": "credits",
                                    "language": "en-US"})
            r.raise_for_status()
            d = r.json()
    except Exception as exc:  # noqa: BLE001 - surface a reason instead of "no hit"
        return {"error": f"TMDB request failed ({type(exc).__name__})"}

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
