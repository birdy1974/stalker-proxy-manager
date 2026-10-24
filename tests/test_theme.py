"""Light / dark / auto color theme: pre-paint script, navbar control, palettes.

The theme is applied by theme.js setting Bootstrap 5.3's data-bs-theme on
<html> before the first paint; these tests pin that every GUI surface loads
that script early enough, offers the control, and that app.css carries a dark
palette (with the light palette - the original look - unchanged).
"""

from pathlib import Path
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("page", ["dashboard", "portals", "settings"])
async def test_admin_pages_apply_the_theme_before_paint_and_offer_the_control(page):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/" + page)
    assert r.status_code == 200
    m = re.search(r"/static/js/theme\.js\?v=[0-9a-f]{10}", r.text)
    assert m, "theme.js must be loaded with the static-asset fingerprint"
    # Anti-flash: the script that resolves the theme must run before the
    # first stylesheet can paint anything.
    assert m.start() < r.text.index("bootstrap.min.css")
    assert 'name="color-scheme"' in r.text
    for value in ("light", "dark", "auto"):
        assert f'data-spm-theme-value="{value}"' in r.text


async def test_web_player_carries_the_shared_theme():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/player")
    assert r.status_code == 200
    # No ?v= fingerprint here: the player renders through its own Jinja env
    # without the static_v global (like its other assets).
    assert "/static/js/theme.js" in r.text
    assert "data-spm-theme-toggle" in r.text
    # Dark stays the no-JS default (the page's original look); light is the
    # admin palette, applied when the shared choice resolves to it.
    assert '<html lang="en" data-bs-theme="dark">' in r.text
    assert '[data-bs-theme="light"]' in r.text
    assert "--pl-bg: #f4f6f8" in r.text
    assert "--pl-bg: #101820" in r.text


def test_login_template_carries_the_theme_and_no_cdn():
    tpl = (ROOT / "app/templates/login.html").read_text()
    assert "theme.js?v={{ static_v.theme_js }}" in tpl
    assert "data-spm-theme-toggle" in tpl
    # Vendored like every other page: the login screen must not depend on a
    # CDN being reachable from the box.
    assert "cdn.jsdelivr.net" not in tpl
    assert "/static/vendor/bootstrap/bootstrap.min.css" in tpl
    assert "/static/vendor/bootstrap/bootstrap-icons.min.css" in tpl


def test_theme_script_contract():
    js = (ROOT / "app/static/js/theme.js").read_text()
    for needle in ('"spm-theme"',                     # localStorage key
                   "prefers-color-scheme: dark",       # auto follows the OS
                   'setAttribute("data-bs-theme"',     # the Bootstrap switch
                   "data-spm-theme-value",             # navbar dropdown items
                   "data-spm-theme-toggle",            # flip buttons
                   '"storage"'):                       # tabs stay in sync
        assert needle in js, needle
    # Nothing stored (or garbage stored) resolves to auto, never crashes.
    assert 'MODES.includes(v) ? v : "auto"' in js


def test_css_dark_palette_and_bridges_with_light_unchanged():
    css = (ROOT / "app/static/css/app.css").read_text()
    # The light palette is the original look, value for value.
    assert "--spm-bg: #f4f6f8" in css
    assert "--spm-surface: #fff" in css
    # Dark overrides the same semantic variables...
    assert '[data-bs-theme="dark"]' in css
    assert "--spm-bg: #10161d" in css
    # ...and aligns Bootstrap's own body background with the slate surfaces.
    assert "--bs-body-bg: #1a222c" in css
    # Custom surfaces read the variables, not the old hardcoded light colours.
    assert "background: var(--spm-surface)" in css
    assert ".card-header { background: #fff" not in css
    # Fixed-light Bootstrap utilities + semantic status/log pairs get a
    # dark reading instead of glowing white / going unreadable.
    for sel in ('[data-bs-theme="dark"] .btn-light',
                '[data-bs-theme="dark"] .text-bg-light',
                '[data-bs-theme="dark"] .bg-light',
                '[data-bs-theme="dark"] .table-light',
                '[data-bs-theme="dark"] .st-online',
                '[data-bs-theme="dark"] .log-ERROR'):
        assert sel in css, sel
