"""
Every tab needs a picture, and the admin gets to choose which one.

What is pinned here:

* `/favicon.ico` answers for anyone, logged in or not (the login page has a tab
  too) and returns the picture the `favicon` setting names;
* every rendered page - dashboard AND login - carries the <link> tags, with a
  `?v=` fingerprint that CHANGES when the icon changes (browsers cache a
  favicon until roughly the heat death of the universe otherwise);
* the picker validates: unknown built-in, unsupported file type, oversized
  file and scripted SVG are all refused with a message the GUI can show;
* an uploaded picture lands on the data volume (not in the image) and a stale
  selection falls back to a working built-in instead of a broken tab icon.
"""
from __future__ import annotations

import base64
import json

from httpx import ASGITransport, AsyncClient

from app.database import SessionLocal
from app.main import app
from app.models import Setting
from app.routers.web import templates
from app.services import branding

BASE = "http://testserver"

# smallest valid PNG (1x1, transparent)
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg==")


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url=BASE)


async def _selected() -> str:
    async with SessionLocal() as s:
        row = await s.get(Setting, branding.SETTING_KEY)
        return json.loads(row.value) if row else ""


def _cleanup() -> None:
    branding.remove_custom()


async def test_favicon_is_served_and_follows_the_setting():
    async with _client() as c:
        r = await c.get("/favicon.ico")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert b"<svg" in r.content                      # the default built-in

        assert (await c.post("/api/branding/favicon", json={"id": "tv"})).json()["ok"]
        assert await _selected() == "tv"
        after = await c.get("/favicon.ico")
        assert b"Television" in after.content            # a *different* picture


async def test_every_page_and_the_login_screen_carry_the_link_tags():
    async with _client() as c:
        page = (await c.get("/dashboard")).text
    assert 'rel="icon" href="/favicon.ico?v=' in page
    assert 'rel="apple-touch-icon"' in page
    # /login redirects while SPM_SKIP_LOGIN is on, so render the template itself
    login = templates.get_template("login.html").render(request=None, error="")
    assert 'rel="icon" href="/favicon.ico?v=' in login


async def test_fingerprint_changes_when_the_picture_changes():
    async with _client() as c:
        await c.post("/api/branding/favicon", json={"id": "tv"})
        first = branding.token()
        await c.post("/api/branding/favicon", json={"id": "play"})
        assert branding.token() != first
        assert f"?v={branding.token()}" in (await c.get("/dashboard")).text


async def test_upload_becomes_the_active_icon_and_survives_on_the_data_volume():
    try:
        async with _client() as c:
            body = {"filename": "mylogo.png",
                    "data": "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()}
            r = (await c.post("/api/branding/favicon/upload", json=body)).json()
            assert r["ok"] and r["selected"] == "custom"
            # stored under DATA_DIR, so a container rebuild keeps it
            stored = branding.custom_path()
            assert stored is not None and stored.parent == branding.BRANDING_DIR
            assert stored.read_bytes() == PNG_1PX

            served = await c.get("/favicon.ico")
            assert served.content == PNG_1PX
            assert served.headers["content-type"] == "image/png"

            listing = (await c.get("/api/branding/favicons")).json()
            assert listing["selected"] == "custom"
            assert listing["custom"]["filename"] == "favicon.png"
            assert len(listing["items"]) == len(branding.BUILTINS)
    finally:
        _cleanup()


async def test_deleting_the_upload_falls_back_to_a_built_in():
    async with _client() as c:
        await c.post("/api/branding/favicon/upload",
                     json={"filename": "x.png",
                           "data": base64.b64encode(PNG_1PX).decode()})
        r = (await c.delete("/api/branding/favicon/custom")).json()
        assert r["removed"] and r["selected"] == branding.DEFAULT_ID
        assert branding.custom_path() is None
        assert (await c.get("/favicon.ico")).status_code == 200


async def test_a_stale_selection_still_renders_a_tab_icon():
    """The custom file was wiped from the volume but the setting still says
    "custom": every page must keep a working icon, not a broken image."""
    async with SessionLocal() as s:
        s.add(Setting(key=branding.SETTING_KEY, value=json.dumps("custom")))
        await s.commit()
    _cleanup()
    async with _client() as c:
        r = await c.get("/favicon.ico")
        assert r.status_code == 200 and b"<svg" in r.content
        assert (await c.get("/api/branding/favicons")).json()["effective"] == branding.DEFAULT_ID


async def test_bad_uploads_are_refused_with_a_readable_reason():
    try:
        async with _client() as c:
            unknown = await c.post("/api/branding/favicon", json={"id": "not-an-icon"})
            assert unknown.status_code == 400 and "unknown favicon" in unknown.text

            no_upload = await c.post("/api/branding/favicon", json={"id": "custom"})
            assert no_upload.status_code == 400

            exe = await c.post("/api/branding/favicon/upload",
                               json={"filename": "evil.exe", "data": "AAAA"})
            assert exe.status_code == 400 and "unsupported image type" in exe.text

            big = await c.post("/api/branding/favicon/upload",
                               json={"filename": "huge.png",
                                     "data": base64.b64encode(b"\x89PNG" + b"0" * (
                                         branding.MAX_UPLOAD_BYTES + 1)).decode()})
            assert big.status_code == 400 and "limit" in big.text

            # a scripted SVG is reachable at /favicon.ico on the admin origin
            svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
            scripted = await c.post("/api/branding/favicon/upload",
                                    json={"filename": "bad.svg",
                                          "data": base64.b64encode(svg).decode()})
            assert scripted.status_code == 400 and "script" in scripted.text
            assert branding.custom_path() is None      # nothing was written
    finally:
        _cleanup()


async def test_favicon_needs_no_session_but_changing_it_does():
    """The picture is public (the login page shows it); the picker is not."""
    from app.security import require_admin

    async def _deny():
        from fastapi import HTTPException
        raise HTTPException(401, "not authenticated")

    app.dependency_overrides[require_admin] = _deny
    try:
        async with _client() as c:
            assert (await c.get("/favicon.ico")).status_code == 200
            assert (await c.get("/api/branding/favicons")).status_code == 401
            assert (await c.post("/api/branding/favicon",
                                 json={"id": "tv"})).status_code == 401
    finally:
        app.dependency_overrides.pop(require_admin, None)


async def test_setting_rides_along_in_a_settings_backup():
    async with _client() as c:
        await c.post("/api/branding/favicon", json={"id": "signal"})
        dump = (await c.get("/api/export?section=settings")).json()
        assert dump["settings"]["favicon"] == "signal"

        # restoring a backup made elsewhere re-points the tab icon
        await c.post("/api/branding/favicon", json={"id": "dot"})
        before = branding.token()
        await c.post("/api/import", json={"mode": "merge",
                                          "data": {"settings": {"favicon": "tower"}}})
        assert await _selected() == "tower"
        assert branding.token() != before
