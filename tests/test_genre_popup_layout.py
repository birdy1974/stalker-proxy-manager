"""Run the JS genre-popup layout check (dev/check-genre-popup.js) when node exists.

The "Edit portal:" popup's genre lists are client-side DOM: Jinja renders the
page and the API answers 200, but a regression in the inline script (a dropped
class on the grid container, a typo in loadGenres' row template, a filter that
no longer hides) ships to the user as a broken popup that no Python test sees.
This test renders the REAL /portals page through the app, then executes it in
jsdom with the REAL app.js (dev/check-genre-popup.js) and asserts the
multi-column band wiring: genre-cols grid container, one ellipsis span per
genre row, the band's CSS (column flow, 15 rows/column, fixed column width,
overflow-x auto + overflow-y hidden) and the live genre filter.

Same convention as test_player_runtime_js.py / dev/check-player.js.
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
async def test_genre_popup_multi_column_band():
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
            ["node", str(ROOT / "dev" / "check-genre-popup.js"),
             str(page_file), str(ROOT / "app" / "static" / "js" / "app.js"),
             str(ROOT / "app" / "static" / "css" / "app.css")],
            capture_output=True, text=True, timeout=60, cwd=ROOT)
        if proc.returncode == 2 and "SKIP:" in proc.stdout:
            pytest.skip(proc.stdout.strip())
        assert proc.returncode == 0, (
            "genre popup runtime check failed:\n"
            f"{proc.stdout}\n{proc.stderr}")
        assert "OK" in proc.stdout
    finally:
        page_file.unlink(missing_ok=True)
