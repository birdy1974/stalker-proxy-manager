"""
Stalker Proxy Manager - central configuration.

Everything is environment-driven so the same image works on the Synology NAS
(docker-compose) and in a plain dev shell. Defaults are chosen so that
`uvicorn app.main:app` works out-of-the-box with a local SQLite database.

Heavy logging note: this module intentionally logs a lot to stdout as well,
so messages are visible in Portainer / `docker logs` on the NAS.
"""

from __future__ import annotations

import os
import shutil
import logging
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# DATA_DIR is the persistent config volume (sqlite dev db, caches, exports).
def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".spm-write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


DATA_DIR = Path(os.environ.get("SPM_DATA_DIR", "/config")).resolve()
if not _writable(DATA_DIR):
    # dev-shell fallback: container paths are not writable outside docker
    DATA_DIR = Path("./data").resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
# MEDIA_ROOT is the read-only mount with local video files for the "local" tab.
MEDIA_ROOT = Path(os.environ.get("SPM_MEDIA_ROOT", "/media")).resolve()
try:
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
except OSError:
    MEDIA_ROOT = DATA_DIR / "media"
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
# EPG/logo caches live under the data dir so they survive container restarts.
CACHE_DIR = DATA_DIR / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# Production (docker-compose): postgresql+asyncpg://spm:...@db:5432/spm
# Dev fallback:                sqlite+aiosqlite:///<DATA_DIR>/spm.db
DATABASE_URL = os.environ.get(
    "SPM_DATABASE_URL",
    f"sqlite+aiosqlite:///{(DATA_DIR / 'spm.db').as_posix()}",
)

# ---------------------------------------------------------------------------
# Admin GUI authentication (Phase-1 decision Q5: login required)
# ---------------------------------------------------------------------------
ADMIN_USERNAME = os.environ.get("SPM_ADMIN_USERNAME") or os.environ.get("SPM_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("SPM_ADMIN_PASSWORD", "admin")  # change via env!
SECRET_KEY = os.environ.get("SPM_SECRET_KEY", "change-me-in-production-please")
SESSION_MAX_AGE = int(os.environ.get("SPM_SESSION_MAX_AGE", str(60 * 60 * 12)))  # 12h

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
HTTP_PORT = int(os.environ.get("SPM_PORT", "8880"))
# Optional override for generated playlist/stream URLs (reverse proxy / NAT).
# When empty we derive the base URL from the incoming request (STB-Proxy style).
OUTPUT_BASE_URL = os.environ.get("SPM_OUTPUT_BASE_URL", "").rstrip("/")

# ---------------------------------------------------------------------------
# ffmpeg / hardware transcoding
# ---------------------------------------------------------------------------
def _find_ffmpeg() -> str:
    """Locate an ffmpeg binary: env override -> PATH -> imageio-ffmpeg static build."""
    env = os.environ.get("SPM_FFMPEG_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:  # dev convenience: imageio-ffmpeg ships a static binary
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"  # last resort; will fail loudly in the logs


FFMPEG_BIN = _find_ffmpeg()
FFPROBE_BIN = os.environ.get("SPM_FFPROBE_BIN", FFMPEG_BIN.replace("ffmpeg", "ffprobe"))

# VAAPI render nodes on the DS918+ (Apollo Lake): usually only renderD128 exists.
VAAPI_DEVICE_CANDIDATES = ["/dev/dri/renderD128", "/dev/dri/renderD129"]
VAAPI_DEVICE = next((d for d in VAAPI_DEVICE_CANDIDATES if Path(d).exists()), VAAPI_DEVICE_CANDIDATES[0])

# How long a stream may produce no data before we treat it as dead (fallback trigger).
STREAM_START_TIMEOUT = float(os.environ.get("SPM_STREAM_START_TIMEOUT", "12"))
# Max time allowed for a single portal HTTP request.
PORTAL_HTTP_TIMEOUT = float(os.environ.get("SPM_PORTAL_HTTP_TIMEOUT", "10"))
# Pages fetched per genre per batch (portal pages are ~14 items; 30 pages ~= 420 items).
FETCH_PAGE_BUDGET = int(os.environ.get("SPM_FETCH_PAGE_BUDGET", "30"))
# Global fallback strategy (spec): try all MACs of a portal first, or hop portals directly.
FALLBACK_STRATEGY = os.environ.get("SPM_FALLBACK_STRATEGY", "macs_first")  # or portal_first

# ---------------------------------------------------------------------------
# Feature toggles
# ---------------------------------------------------------------------------
# The built-in mock portal lets you test the full flow without a real portal.
# Real Stalker portals typically block datacenter IPs, so this is also our
# development/CI target.
MOCK_PORTAL_ENABLED = os.environ.get("SPM_MOCK_PORTAL", "1") == "1"

# MOCKUP/DEV ONLY: bypass GUI admin login entirely (for sandbox previews &
# early UI testing). NEVER default-on; production images must not set this.
SKIP_LOGIN = os.environ.get("SPM_SKIP_LOGIN", "0") == "1"

LOG_LEVEL = os.environ.get("SPM_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
)
log = logging.getLogger("spm.config")
log.info("DATA_DIR=%s MEDIA_ROOT=%s", DATA_DIR, MEDIA_ROOT)
log.info("DATABASE_URL=%s", DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL)
log.info("FFMPEG_BIN=%s VAAPI_DEVICE=%s (exists=%s)", FFMPEG_BIN, VAAPI_DEVICE, Path(VAAPI_DEVICE).exists())
