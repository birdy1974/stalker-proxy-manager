"""Global default selection, restart persistence and the Duo2 live preset."""
import shlex
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.database import SessionLocal
from app.main import app, _hardware_sanity, _seed_defaults
from app.models import FFmpegTemplate
from app.services.ffmpeg_templates import (
    COPY_PRESET_NAME, E2_DUO2_LIVE_PRESET_NAME, FFmpegOptions,
    REDIRECT_PRESET_NAME, REFERENCE_PRESET_NAME, build_command, parse_command,
)
from app.services.playback import TemplateMap


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


async def rows():
    async with SessionLocal() as db:
        return list((await db.scalars(select(FFmpegTemplate))).all())


async def catalog():
    return {row.name: row for row in await rows()}


async def assert_default(tid):
    saved = await rows()
    assert [row.id for row in saved if row.is_default] == [tid]
    assert TemplateMap(saved, None, {}).default.id == tid


async def test_initial_default_is_redirect_and_choice_survives_reseeding_and_hardware_check():
    await _seed_defaults()
    items = await catalog()
    await assert_default(items[REDIRECT_PRESET_NAME].id)
    chosen = items[E2_DUO2_LIVE_PRESET_NAME]
    async with client() as c:
        result = await c.post(f'/api/ffmpeg/{chosen.id}/default')
        assert result.status_code == 200, result.text
        assert result.json()['item']['is_default'] is True
    await assert_default(chosen.id)
    await _seed_defaults()
    with patch('os.path.exists', return_value=False), patch('app.main.db_log', new_callable=AsyncMock) as log:
        await _hardware_sanity()
        assert log.await_count == 1
        assert 'unchanged' in log.call_args.args[2]
    await assert_default(chosen.id)
    async with client() as c:
        result = await c.post(f'/api/ffmpeg/{items[REDIRECT_PRESET_NAME].id}/default')
        assert result.status_code == 200
    await assert_default(items[REDIRECT_PRESET_NAME].id)


async def test_default_selection_does_not_rewrite_a_manual_command():
    await _seed_defaults()
    command = 'ffmpeg -i <url> -c copy -metadata title=Mine -f mpegts pipe:1'
    async with client() as c:
        item = (await c.post('/api/ffmpeg', json={
            'name':'My command', 'command_source':'manual', 'command':command})).json()['item']
        response = await c.post(f'/api/ffmpeg/{item["id"]}/default')
        assert response.status_code == 200, response.text
        assert response.json()['item']['command'] == command
    await _seed_defaults()
    await assert_default(item['id'])
    assert (await catalog())['My command'].command == command


@pytest.mark.parametrize('method', ['post', 'put'])
async def test_crud_default_flags_also_replace_the_previous_default(method):
    await _seed_defaults()
    async with client() as c:
        if method == 'post':
            # Default enabled=True is materialized before checking eligibility.
            result = await c.post('/api/ffmpeg', json={'name':'My fallback', 'is_default':True})
        else:
            target = (await catalog())[COPY_PRESET_NAME]
            result = await c.put(f'/api/ffmpeg/{target.id}', json={'is_default':True})
        assert result.status_code == 200, result.text
        await assert_default(result.json()['item']['id'])


async def test_disabled_or_missing_templates_cannot_be_selected():
    await _seed_defaults()
    items = await catalog()
    target = items[COPY_PRESET_NAME]
    async with client() as c:
        assert (await c.put(f'/api/ffmpeg/{target.id}', json={'enabled':False})).status_code == 200
        assert (await c.post(f'/api/ffmpeg/{target.id}/default')).status_code == 422
        assert (await c.put(f'/api/ffmpeg/{target.id}', json={'is_default':True})).status_code == 422
        assert (await c.post('/api/ffmpeg/999999/default')).status_code == 404
        assert (await c.post('/api/ffmpeg', json={'name':'Invalid default', 'enabled':False, 'is_default':True})).status_code == 422
        assert (await c.put(f'/api/ffmpeg/{target.id}', json={'is_default':'false'})).status_code == 422
    await assert_default(items[REDIRECT_PRESET_NAME].id)


async def test_choose_another_default_before_disabling_or_deleting_current_default():
    await _seed_defaults()
    items = await catalog()
    default, replacement = items[REDIRECT_PRESET_NAME], items[COPY_PRESET_NAME]
    async with client() as c:
        for payload in ({'enabled':False}, {'enabled':0}, {'enabled':None}, {'is_default':False}):
            assert (await c.put(f'/api/ffmpeg/{default.id}', json=payload)).status_code == 409
        assert (await c.delete(f'/api/ffmpeg/{default.id}')).status_code == 409
        await assert_default(default.id)
        assert (await c.post(f'/api/ffmpeg/{replacement.id}/default')).status_code == 200
        assert (await c.put(f'/api/ffmpeg/{default.id}', json={'enabled':False})).status_code == 200
        assert (await c.delete(f'/api/ffmpeg/{default.id}')).status_code == 200
    # Recreating Redirect at startup must not steal the user's new default.
    await _seed_defaults()
    await assert_default(replacement.id)


async def test_seed_recovers_missing_and_multiple_legacy_defaults():
    await _seed_defaults()
    async with SessionLocal() as db:
        for row in (await db.scalars(select(FFmpegTemplate))).all():
            row.is_default = False
        await db.commit()
    await _seed_defaults()
    items = await catalog()
    await assert_default(items[REDIRECT_PRESET_NAME].id)
    async with SessionLocal() as db:
        for row in (await db.scalars(select(FFmpegTemplate))).all():
            row.is_default = True
        await db.commit()
    await _seed_defaults()  # does not raise MultipleResultsFound
    assert len([row for row in await rows() if row.is_default and row.enabled]) == 1


async def test_duo2_live_ships_vbr_and_source_fps_and_upgrades_existing_fields_rows():
    await _seed_defaults()
    row = (await catalog())[E2_DUO2_LIVE_PRESET_NAME]
    async with SessionLocal() as db:
        old = await db.get(FFmpegTemplate, row.id)
        old.rc_mode, old.fps = 'CQP', '25'
        await db.commit()
    await _seed_defaults()
    updated = (await catalog())[E2_DUO2_LIVE_PRESET_NAME]
    assert updated.rc_mode == 'VBR' and updated.fps == ''
    tokens = shlex.split(updated.command)
    assert tokens[tokens.index('-rc_mode') + 1] == 'VBR'
    assert '-r' not in tokens and 'fps=' not in updated.command
    assert '-b:v 4000k' in updated.command
    assert '-maxrate 4400k' in updated.command and '-bufsize 8000k' in updated.command
    assert '-global_quality' not in tokens
    opts = FFmpegOptions(**{key:getattr(updated,key) for key in FFmpegOptions.__dataclass_fields__})
    assert updated.command == build_command(opts)
    assert (await catalog())[REFERENCE_PRESET_NAME].rc_mode == 'CQP'


def test_parsing_source_fps_does_not_invent_a_25_fps_override():
    command = build_command(FFmpegOptions(fps="", rc_mode="VBR"))
    parsed = parse_command(command)["options"]
    assert parsed["fps"] == ""
    assert build_command(FFmpegOptions(**parsed)) == command
    # A partial parse still retains an explicitly supplied dormant field.
    copy = build_command(FFmpegOptions(video_codec="copy", fps="50"))
    assert parse_command(copy, base={"fps":"50"})["options"]["fps"] == "50"
