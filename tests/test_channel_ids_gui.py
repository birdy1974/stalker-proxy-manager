"""Run the JS Channel-IDs comparison check (dev/check-channel-ids.js).

The Channel IDs tab inside the Compare-packages matrix is client-side DOM:
Jinja renders the page and the API answers 200, but a regression in the
inline script (a dropped ⚠ badge, ids rendered one-per-row instead of
side-by-side, a filter wired to the wrong state key) ships to the user as a
broken popup no Python test sees.

Same convention as test_genre_popup_layout.py / dev/check-genre-popup.js.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
async def test_channel_ids_tab_runtime():
    from app.main import app

    with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
        page_file = Path(f.name)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app),
                                     base_url="http://test") as c:
            r = await c.get("/portals")
        assert r.status_code == 200, r.text[:300]
        page_file.write_text(r.text, encoding="utf-8")

        proc = subprocess.run(
            ["node", str(ROOT / "dev" / "check-channel-ids.js"),
             str(page_file), str(ROOT / "app" / "static" / "js" / "app.js")],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        if proc.returncode == 2 and "SKIP:" in proc.stdout:
            pytest.skip(proc.stdout.strip())
        assert proc.returncode == 0, (
            "channel-ids runtime check failed:\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "OK" in proc.stdout
    finally:
        page_file.unlink(missing_ok=True)
