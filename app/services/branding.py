"""
Branding: which picture the browser tab shows (favicon).

Two sources, one active choice:

* **built-ins** ship inside the image (`app/static/img/favicons/*.svg`) so a
  fresh install already has a tab icon on every page - no upload needed;
* a **custom** picture uploaded from Settings, stored in the persistent data
  volume (`DATA_DIR/branding/`) so it survives an image rebuild. The static
  tree is part of the image and would lose the file on every `docker pull`.

The active choice is one `settings` row (`favicon`): the id of a built-in, or
`"custom"`. That keeps it inside the existing GUI-is-the-source-of-truth model
(and rides along in the settings backup) instead of adding a table.

`/favicon.ico` resolves that row on every request, so a change is live for the
next tab that asks. Browsers cache favicons very aggressively, though, so the
`<link>` tags carry a `?v=` fingerprint of the active file - `token()` is a
plain in-memory value that page rendering can read without touching the DB, and
it is bumped whenever the icon changes.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from markupsafe import Markup

from ..config import DATA_DIR, log

SETTING_KEY = "favicon"
DEFAULT_ID = "broadcast"
CUSTOM_ID = "custom"

# Where the built-ins live (relative to the working directory, like the rest of
# the app's asset paths: templates="app/templates", static="app/static").
BUILTIN_DIR = Path("app/static/img/favicons")
# Uploads land on the config volume, next to the sqlite db / caches.
BRANDING_DIR = DATA_DIR / "branding"

# id -> label/file. The order is the order of the picker in Settings.
BUILTINS: tuple[dict[str, str], ...] = (
    {"id": "broadcast", "label": "Broadcast", "file": "broadcast.svg"},
    {"id": "satellite", "label": "Satellite dish", "file": "satellite.svg"},
    {"id": "tv", "label": "TV screen", "file": "tv.svg"},
    {"id": "play", "label": "Play", "file": "play.svg"},
    {"id": "signal", "label": "Signal bars", "file": "signal.svg"},
    {"id": "tower", "label": "Antenna tower", "file": "tower.svg"},
    {"id": "dot", "label": "Minimal dot", "file": "dot.svg"},
)
BUILTIN_IDS = tuple(b["id"] for b in BUILTINS)

# Formats a browser can actually use as a tab icon. Anything else is refused
# with a readable message instead of silently producing a blank tab.
MEDIA_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
MAX_UPLOAD_BYTES = 512 * 1024        # a tab icon; anything bigger is a mistake

_token = "0"


# --------------------------------------------------------------- built-ins
def builtin_path(ident: str) -> Path | None:
    for b in BUILTINS:
        if b["id"] == ident:
            p = BUILTIN_DIR / b["file"]
            return p if p.exists() else None
    return None


def builtin_url(ident: str) -> str:
    for b in BUILTINS:
        if b["id"] == ident:
            return f"/static/img/favicons/{b['file']}"
    return ""


# --------------------------------------------------------------- custom file
def custom_path() -> Path | None:
    """The uploaded picture, whatever extension it was saved with (one at a time)."""
    if not BRANDING_DIR.is_dir():
        return None
    for p in sorted(BRANDING_DIR.glob("favicon.*")):
        if p.is_file() and p.suffix.lower() in MEDIA_TYPES:
            return p
    return None


def _looks_dangerous_svg(data: bytes) -> bool:
    """Reject scripted SVG.

    A favicon is loaded as an image, so `<script>` inside it never runs from the
    `<link>` tag - but the same file is reachable at `/favicon.ico`, and opening
    THAT url renders the SVG as a document on the admin origin (session cookie
    and all). Refusing the upload is cheaper than sanitising, and no legitimate
    tab icon needs script or an external reference.
    """
    text = data[:200_000].decode("utf-8", "ignore").lower()
    return bool(re.search(r"<script|\son\w+\s*=|javascript:|<foreignobject|<!entity", text))


def save_custom(data: bytes, filename: str) -> Path:
    """Validate + store an uploaded picture. Raises ValueError with a GUI message."""
    suffix = Path(filename or "").suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise ValueError(
            f"unsupported image type '{suffix or filename}'. Use "
            + ", ".join(sorted(MEDIA_TYPES)))
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"file is {len(data) // 1024} kB, the limit is "
                         f"{MAX_UPLOAD_BYTES // 1024} kB")
    if suffix == ".svg":
        if b"<svg" not in data[:200_000].lower():
            raise ValueError("that .svg does not contain an <svg> element")
        if _looks_dangerous_svg(data):
            raise ValueError("that .svg contains script or external references - "
                             "export it as a plain image (or upload a .png)")
    BRANDING_DIR.mkdir(parents=True, exist_ok=True)
    for old in BRANDING_DIR.glob("favicon.*"):     # only one custom icon at a time
        try:
            old.unlink()
        except OSError:                            # pragma: no cover - race/permissions
            log.warning("could not remove previous favicon %s", old)
    target = BRANDING_DIR / f"favicon{suffix}"
    target.write_bytes(data)
    return target


def decode_data_url(payload: str) -> bytes:
    """`data:image/png;base64,....` (what the Settings page posts) -> bytes."""
    raw = payload.split(",", 1)[1] if payload.startswith("data:") else payload
    try:
        return base64.b64decode(raw, validate=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("could not decode the uploaded file") from exc


def remove_custom() -> bool:
    removed = False
    if BRANDING_DIR.is_dir():
        for p in BRANDING_DIR.glob("favicon.*"):
            try:
                p.unlink()
                removed = True
            except OSError:                        # pragma: no cover
                log.warning("could not remove custom favicon %s", p)
    return removed


# --------------------------------------------------------------- resolution
async def selected_id() -> str:
    from .runtime_settings import get_setting
    val = await get_setting(SETTING_KEY, DEFAULT_ID)
    val = (val or "").strip() if isinstance(val, str) else ""
    return val or DEFAULT_ID


def resolve_id(ident: str) -> tuple[Path, str, str]:
    """(file, media type, effective id) for a choice, with a working fallback.

    A selection can go stale - the custom file was deleted from the volume, or a
    built-in was renamed in a later release. Falling back to the default keeps
    every tab showing *something* instead of a broken-image icon.
    """
    if ident == CUSTOM_ID:
        p = custom_path()
        if p is not None:
            return p, MEDIA_TYPES.get(p.suffix.lower(), "image/png"), CUSTOM_ID
        ident = DEFAULT_ID
    p = builtin_path(ident)
    if p is None:
        ident = DEFAULT_ID
        p = builtin_path(DEFAULT_ID)
    if p is None:                                   # pragma: no cover - image is broken
        raise FileNotFoundError("no favicon asset found")
    return p, MEDIA_TYPES.get(p.suffix.lower(), "image/svg+xml"), ident


async def resolve() -> tuple[Path, str, str]:
    """Active favicon file for this install (reads the settings row)."""
    return resolve_id(await selected_id())


# --------------------------------------------------------------- cache token
def token() -> str:
    return _token


def _fingerprint(path: Path, ident: str) -> str:
    try:
        st = path.stat()
        raw = f"{ident}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:                                 # pragma: no cover
        raw = ident
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


async def refresh() -> str:
    """Recompute the `?v=` fingerprint (boot, and after every change)."""
    global _token
    try:
        path, _mt, ident = await resolve()
        _token = _fingerprint(path, ident)
    except Exception:                               # noqa: BLE001 - never break a page
        log.exception("favicon fingerprint refresh failed")
    return _token


# --------------------------------------------------------------- templates
def favicon_tags() -> Markup:
    """The <link> tags every page (and the login screen) puts in its <head>."""
    href = f"/favicon.ico?v={token()}"
    return Markup(
        f'<link rel="icon" href="{href}">'
        f'<link rel="shortcut icon" href="{href}">'
        f'<link rel="apple-touch-icon" href="/apple-touch-icon.png?v={token()}">'
    )
