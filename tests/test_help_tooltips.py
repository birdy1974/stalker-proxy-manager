"""The shared help component is present throughout the admin UI."""
from pathlib import Path
import re

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.parametrize('page', ['dashboard','portals','sources','playlist','ffmpeg',
                                  'settings','enigma2','areas','users'])
async def test_main_pages_and_their_dynamic_dialogs_load_shared_tooltips(page):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        response = await c.get('/' + page)
        assert response.status_code == 200
        assert re.search(r'/static/js/help-tooltips.js\?v=[0-9a-f]{10}', response.text)
        assert 'data-help=' in response.text or 'title=' in response.text


def test_help_is_explicit_and_does_not_hide_status_classes_or_actions():
    root = Path(__file__).resolve().parents[1]
    css = (root / 'app/static/css/app.css').read_text()
    assert '[data-help], [data-help-detail] { display: none !important; }' in css
    assert '.help-tip-button:focus-visible' in css
    script = (root / 'app/static/js/help-tooltips.js').read_text()
    assert "html:false" in script
    assert 'MutationObserver' in script
    assert "record.instance?.dispose()" in script
    # The installer action used to live inside an explanatory paragraph. It
    # must remain outside the hidden help source after the conversion.
    template = (root / 'app/templates/enigma2.html').read_text()
    assert '</div><button class="btn btn-sm btn-link p-0 align-baseline" id="e2-rotate"' in template
    backup = (root / 'app/templates/settings_backup.html').read_text()
    assert 'id="restore-confirm"' in backup and 'id="delete-confirm"' in backup
    assert '<p class="small mb-2"><strong>Permanent and cannot be undone.</strong>' in backup
