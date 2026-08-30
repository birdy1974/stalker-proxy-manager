"""Run the JS player runtime check (dev/check-player.js) when node exists.

History: app/static/js/app.js shipped a TypeError (openModal never returned a
`footer`, playInModal called m.footer.append) that no Python test could see -
Jinja rendered, the API answered 200, and the user got a dead black popup.
This test executes the REAL app.js playInModal against the REAL vendored
mpegts.js in a node vm sandbox and asserts it does not throw and the popup
diag panel actually renders text.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_player_popup_runs_without_runtime_error():
    proc = subprocess.run(
        ["node", "dev/check-player.js"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        "playInModal runtime check failed:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert "OK" in proc.stdout
