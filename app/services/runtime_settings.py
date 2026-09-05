"""
Runtime settings: the GUI (DB) is the source of truth, env is the fallback.

The Settings tab writes JSON values into the `settings` table. Several engine
knobs used to be env-only (`SPM_FALLBACK_STRATEGY`, `SPM_FETCH_PAGE_BUDGET`,
`SPM_OUTPUT_BASE_URL`), so saving the GUI had no effect until a container
restart with matching env. These helpers read the DB on every use (one PK
lookup) so a Save takes effect on the next request. An empty/missing/invalid
DB value falls back to the env default from config.py.

Boot seeds the table from those env defaults, so a first start still honours
docker-compose; later GUI edits win without a restart.
"""
from __future__ import annotations

import json

from ..database import SessionLocal
from ..models import Setting

VALID_STRATEGIES = ("macs_first", "portal_first")


async def get_setting(key: str, default=None):
    """JSON-decoded `settings` row, or `default` when missing/undecodable."""
    async with SessionLocal() as s:
        row = await s.get(Setting, key)
        if row is None or row.value is None:
            return default
        try:
            return json.loads(row.value)
        except (json.JSONDecodeError, TypeError):
            return default


async def fallback_strategy() -> str:
    """macs_first | portal_first — GUI setting, then SPM_FALLBACK_STRATEGY."""
    from ..config import FALLBACK_STRATEGY
    val = await get_setting("fallback_strategy", None)
    if isinstance(val, str) and val.strip() in VALID_STRATEGIES:
        return val.strip()
    return FALLBACK_STRATEGY if FALLBACK_STRATEGY in VALID_STRATEGIES else "macs_first"


async def fetch_page_budget() -> int:
    """Pages per genre. GUI setting, then SPM_FETCH_PAGE_BUDGET, then 30."""
    from ..config import FETCH_PAGE_BUDGET
    val = await get_setting("fetch_page_budget", None)
    try:
        n = int(val)
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    try:
        n = int(FETCH_PAGE_BUDGET)
        return n if n > 0 else 30
    except (TypeError, ValueError):
        return 30


async def vlc_local_network_caching_ms() -> int:
    """VLC network cache advertised for direct local-file M3U entries.

    Zero disables the VLC-specific directive. Keep bad restored/imported values
    from producing unreasonable playlists.
    """
    val = await get_setting("vlc_local_network_caching_ms", 500)
    try:
        return min(60_000, max(0, int(val)))
    except (TypeError, ValueError):
        return 500


async def output_base_url() -> str:
    """Public URL override (no trailing slash). Empty = derive from the request.

    GUI `output_base_url` wins when non-empty; otherwise SPM_OUTPUT_BASE_URL.
    """
    from ..config import OUTPUT_BASE_URL
    val = await get_setting("output_base_url", None)
    if isinstance(val, str) and val.strip():
        return val.strip().rstrip("/")
    return (OUTPUT_BASE_URL or "").rstrip("/")
