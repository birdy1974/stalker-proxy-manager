"""Parameter help, custom values, range checks, and lossless extra-flag editing."""
import shlex
from dataclasses import asdict

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.ffmpeg_editor import ADVANCED, FIELDS, field_errors, set_extra_option
from app.services.ffmpeg_templates import (
    FFmpegOptions, build_command, parse_command, target_size, template_command_errors,
)


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_every_structured_parameter_has_help_and_all_suggestions_are_valid():
    assert set(FFmpegOptions.__dataclass_fields__) <= set(FIELDS)
    assert all(len(d['help']) > 30 for d in [*FIELDS.values(), *ADVANCED])
    for name, definition in FIELDS.items():
        if name not in FFmpegOptions.__dataclass_fields__:
            continue
        for choice in definition['choices'] or []:
            opts = FFmpegOptions(**{name: choice})
            assert field_errors(opts) == [], (name, choice, field_errors(opts))
    for d in ADVANCED:
        for choice in d['choices']:
            text = set_extra_option('', d['side'], d['flag'], choice)
            assert d['flag'] in shlex.split(text)


@pytest.mark.parametrize('resolution,aspect,expected', [
    ('900p', '16:9', (1600, 900)), ('1080p', '9:16', (608, 1080)),
    ('1600x1000', '4:3', (1600, 1000)), ('4320p', '16:9', (7680, 4320)),
    ('8192x8192', '16:9', (8192, 8192)), ('source', '16:9', None),
    ('0x0', '16:9', None), ('123x455', '16:9', None), ('99999p', '16:9', None),
])
def test_custom_resolution_and_aspect(resolution, aspect, expected):
    assert target_size(resolution, aspect) == expected


@pytest.mark.parametrize('hardware,codec', [('none', 'libx264'), ('vaapi', 'h264_vaapi'), ('qsv', 'h264_qsv')])
async def test_custom_values_roundtrip_through_build_parse_save(hardware, codec):
    opts = FFmpegOptions(hw_accel=hardware, video_codec=codec, resolution='1600x1000',
                        fps='47.5', video_bitrate='2250k', gop='95', rc_mode='VBR')
    opts.extra_input = set_extra_option('', 'input', '-user_agent', 'My Player/1.0 (custom)')
    opts.extra_output = set_extra_option('-preset slow -flush_packets -1', 'output', '-metadata', "title=Viewer's custom stream")
    command = build_command(opts)
    assert template_command_errors(command) == []
    parsed = FFmpegOptions(**parse_command(command, base=asdict(opts))['options'])
    assert target_size(parsed.resolution, parsed.aspect) == (1600, 1000)
    assert parsed.fps == '47.5'
    assert '-preset slow' in parsed.extra_output
    assert shlex.split(build_command(parsed)) == shlex.split(command)
    async with client() as c:
        response = await c.post('/api/ffmpeg', json={**asdict(opts), 'name':'Custom', 'command_source':'fields'})
        assert response.status_code == 200, response.text
        item = response.json()['item']
        assert item['command'] == command
        assert item['resolution'] == '1600x1000'
        assert item['fps'] == '47.5'


@pytest.mark.parametrize('key,value', [
    ('global_quality', '52'), ('global_quality', '-1'), ('gop', '-2'), ('gop', '2.5'),
    ('async_depth', '0'), ('async_depth', '65'), ('fps', '0'), ('fps', 'NaN'),
    ('fps', 'AUTO'), ('video_bitrate', '-500k'), ('audio_rate', '4000'),
    ('audio_channels', '0'), ('resolution', '1921x1080'), ('resolution', '99999p'),
    ('aspect', '0:1'), ('video_codec', 'libx264 -y'),
])
async def test_invalid_structured_values_rejected_for_build_and_save(key, value):
    async with client() as c:
        for path in ('/api/ffmpeg/build', '/api/ffmpeg'):
            context = {'rc_mode':'VBR'} if key == 'video_bitrate' else {}
            response = await c.post(path, json={**context, key:value, 'name':'Invalid', 'command_source':'fields'})
            assert response.status_code == 422, (key, value, response.text)


async def test_advanced_helper_replaces_duplicates_and_preserves_quotes():
    async with client() as c:
        response = await c.post('/api/ffmpeg/extra-option', json={
            'side':'input', 'flag':'-rw_timeout', 'value':'20000000',
            'raw':'-user_agent "My Player" -rw_timeout 1 -rw_timeout 2',
        })
        assert response.status_code == 200, response.text
        assert shlex.split(response.json()['extra']) == ['-user_agent','My Player','-rw_timeout','20000000']
        for flag, value, side in [('-crf','99','output'), ('-probesize','10','input'),
                                  ('-vf','scale=1:1','output'), ('-i','file','input'),
                                  ('-rw_timeout','','input'), ('-threads','AUTO','output')]:
            bad = await c.post('/api/ffmpeg/extra-option', json={'side':side,'flag':flag,'value':value})
            assert bad.status_code == 422, bad.text


async def test_unknown_options_and_manual_command_are_preserved():
    raw = set_extra_option('-preset slow', 'output', '-my_private_switch', '')
    opts = FFmpegOptions(hw_accel='none', video_codec='libx264', extra_output=raw)
    assert template_command_errors(build_command(opts)) == []
    command = 'ffmpeg -i <url> -vf scale=1600:1000,unsharp=5:5:1 -c:v libx264 -crf 21 -preset slow -f mpegts pipe:1'
    async with client() as c:
        response = await c.post('/api/ffmpeg', json={'name':'Manual', 'command_source':'manual','command':command})
        assert response.status_code == 200, response.text
        assert response.json()['item']['command'] == command


async def test_editor_page_renders_help_schema_custom_controls_and_advanced_helper():
    async with client() as c:
        response = await c.get('/ffmpeg')
        assert response.status_code == 200, response.text
        assert 'FFmpegEditor.setup(' in response.text
        assert 'ffmpeg-editor.js?v=' in response.text
        assert 'ff-advanced-option' in response.text
        assert 'ff-advanced-value' in response.text
        assert 'ff-expand' not in response.text
        assert 'FFmpegEditor.expand' not in response.text
        assert 'id="tpl-default"' in response.text
        assert 'id="tpl-set-default"' in response.text
        assert 'id="tpl-save-top"' in response.text
        assert 'id="tpl-save"' in response.text
        assert 'f-timeout' not in response.text, 'timeout must have one owner: extra_input'
