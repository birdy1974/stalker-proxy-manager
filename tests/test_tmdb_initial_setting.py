"""Compose TMDB key initializes missing settings without overriding GUI edits."""
import json
import os
from pathlib import Path
import subprocess
import sys

from httpx import ASGITransport, AsyncClient
import pytest

from app.database import SessionLocal
from app.main import app, _seed_defaults
from app.models import Setting
from app.routers.api_misc import DEFAULT_SETTINGS
from app.services.tmdb import _api_key


@pytest.mark.parametrize('env_value, expected', [
    (None, ''), ('', ''), ('  test-only-tmdb-key  ', 'test-only-tmdb-key'),
])
def test_environment_populates_default_without_logging_key(env_value, expected):
    env = dict(os.environ)
    if env_value is None:
        env.pop('SPM_TMDB_API_KEY', None)
    else:
        env['SPM_TMDB_API_KEY'] = env_value
    script = (
        'from app.config import TMDB_API_KEY\n'
        'from app.routers.api_misc import DEFAULT_SETTINGS\n'
        f'assert TMDB_API_KEY == {expected!r}\n'
        'assert DEFAULT_SETTINGS["tmdb_api_key"] == TMDB_API_KEY\n'
    )
    result = subprocess.run([sys.executable, '-c', script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    if expected:
        assert expected not in result.stdout + result.stderr


async def test_initial_key_is_persisted_visible_and_used_by_tmdb(monkeypatch):
    monkeypatch.setitem(DEFAULT_SETTINGS, 'tmdb_api_key', 'test-only-initial-key')
    await _seed_defaults()
    async with SessionLocal() as db:
        row = await db.get(Setting, 'tmdb_api_key')
        assert json.loads(row.value) == 'test-only-initial-key'
    assert await _api_key() == 'test-only-initial-key'
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        response = await c.get('/api/settings')
        assert response.status_code == 200
        assert response.json()['settings']['tmdb_api_key'] == 'test-only-initial-key'
    # Removing/changing the environment seed on a later boot must not erase it.
    monkeypatch.setitem(DEFAULT_SETTINGS, 'tmdb_api_key', '')
    await _seed_defaults()
    assert await _api_key() == 'test-only-initial-key'


@pytest.mark.parametrize('saved', ['test-only-gui-key', ''])
async def test_existing_gui_setting_wins_including_explicitly_empty(monkeypatch, saved):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        response = await c.post('/api/settings', json={'tmdb_api_key': saved})
        assert response.status_code == 200
        monkeypatch.setitem(DEFAULT_SETTINGS, 'tmdb_api_key', 'test-only-environment-key')
        await _seed_defaults()
        await _seed_defaults()
        assert await _api_key() == saved
        response = await c.get('/api/settings')
        assert response.json()['settings']['tmdb_api_key'] == saved


async def test_empty_initial_key_keeps_tmdb_optional(monkeypatch):
    monkeypatch.setitem(DEFAULT_SETTINGS, 'tmdb_api_key', '')
    await _seed_defaults()
    assert await _api_key() == ''


def test_compose_and_env_example_offer_optional_initial_key():
    root = Path(__file__).resolve().parents[1]
    assert 'SPM_TMDB_API_KEY: "${SPM_TMDB_API_KEY:-}"' in (root / 'docker-compose.yml').read_text()
    assert 'SPM_TMDB_API_KEY=\n' in (root / '.env.example').read_text()
