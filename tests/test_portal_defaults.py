"""Keep the new-portal dialog and the backend identity defaults aligned."""
from pathlib import Path
import re

from app.database import SessionLocal
from app.models import Portal
from app.routers import api_portals


def test_new_portal_dialog_selects_minimal_and_preserves_saved_identity():
    template = (Path(__file__).resolve().parents[1] / "app/templates/portals.html").read_text()
    defaults = re.search(
        r'const p = id \? table\.items\.find\(x => x\.id === id\) : (\{[^\n]+\});',
        template,
    )
    assert defaults is not None, "Editing must use the saved portal rather than new-portal defaults"
    assert 'identity_mode: "minimal"' in defaults.group(1)
    assert '<option value="minimal"${(p.identity_mode || "minimal") === "minimal" ? " selected" : ""}>' in template
    assert '<option value="mag250"${(p.identity_mode || "") === "mag250" ? " selected" : ""}>' in template
    assert 'identity_mode: $("#p-identity", body).value' in template


async def test_new_portal_api_defaults_to_minimal_but_accepts_mag250():
    async with SessionLocal() as db:
        minimal = await api_portals.create_portal(
            {"name": "Default identity", "base_url": "http://default.invalid/c/"}, db=db)
        explicit = await api_portals.create_portal(
            {"name": "Explicit fingerprint", "base_url": "http://fingerprint.invalid/c/",
             "identity_mode": "mag250"}, db=db)
        assert (await db.get(Portal, minimal["id"])).identity_mode == "minimal"
        assert (await db.get(Portal, explicit["id"])).identity_mode == "mag250"
