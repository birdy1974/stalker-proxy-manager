"""Inactive controls follow generated-command semantics, not just appearance."""
from dataclasses import asdict
import shlex

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.ffmpeg_editor import disabled_parameters, field_errors, schema
from app.services.ffmpeg_templates import FFmpegOptions, build_command


@pytest.mark.parametrize('changes,disabled,enabled', [
    ({}, ['video_bitrate','maxrate','bufsize'], ['global_quality','profile','level','low_power']),
    ({'rc_mode':'VBR'}, ['global_quality'], ['video_bitrate','maxrate','bufsize','rc_mode','async_depth']),
    ({'rc_mode':'ICQ'}, ['video_bitrate','maxrate','bufsize'], ['global_quality']),
    ({'rc_mode':'QVBR'}, [], ['video_bitrate','maxrate','bufsize','global_quality']),
    ({'rc_mode':'AVBR'}, ['maxrate','bufsize','global_quality'], ['video_bitrate']),
    ({'rc_mode':'CBR'}, ['maxrate','global_quality'], ['video_bitrate','bufsize']),
    ({'rc_mode':'CBR','bufsize':''}, ['global_quality'], ['video_bitrate','maxrate','bufsize']),
    ({'rc_mode':'CBR','bufsize':'0'}, ['global_quality'], ['maxrate']),
    ({'rc_mode':'AUTO'}, ['global_quality'], ['video_bitrate','maxrate','bufsize']),
    ({'video_codec':'h264_qsv','hw_accel':'qsv'}, ['rc_mode','async_depth','global_quality','low_power'], ['profile','level','video_bitrate','device']),
    ({'video_codec':'libx264','hw_accel':'none'}, ['device','rc_mode','async_depth','global_quality','low_power'], ['profile','level','video_bitrate','fps']),
    ({'video_codec':'libx265','hw_accel':'none'}, ['device','profile','level','rc_mode','async_depth','global_quality','low_power'], ['video_bitrate','fps']),
    ({'video_codec':'hevc_vaapi'}, ['profile','level','low_power','video_bitrate','maxrate','bufsize'], ['rc_mode','global_quality','async_depth']),
    ({'video_codec':'vp9_vaapi'}, ['profile','level','low_power','video_bitrate'], ['rc_mode','global_quality','async_depth']),
    ({'video_codec':'copy'}, ['hw_accel','device','resolution','aspect','fps','gop','profile','level','vf_preset','low_power','rc_mode','global_quality','async_depth','video_bitrate','maxrate','bufsize'], ['video_codec','audio_codec','subs','output_format','audio_rate']),
    ({'resolution':'source'}, ['aspect'], ['resolution','fps','vf_preset']),
    ({'resolution':'1600x1000'}, ['aspect'], ['resolution','fps']),
    ({'audio_codec':'copy'}, ['audio_bitrate','audio_channels','audio_rate'], ['audio_codec','output_format']),
    ({'audio_codec':'none'}, ['audio_bitrate','audio_channels','audio_rate'], ['audio_codec','subs']),
    ({'audio_codec':'flac'}, ['audio_bitrate'], ['audio_channels','audio_rate']),
    ({'audio_codec':'alac'}, ['audio_bitrate'], ['audio_channels','audio_rate']),
    ({'audio_codec':'pcm_s16le'}, ['audio_bitrate'], ['audio_channels','audio_rate']),
])
def test_known_field_dependencies(changes, disabled, enabled):
    reasons = disabled_parameters(FFmpegOptions(**changes))
    assert all(reasons.get(k) for k in disabled)
    assert all(k not in reasons for k in enabled)


@pytest.mark.parametrize('changes', [
    {}, {'rc_mode':'VBR'}, {'rc_mode':'ICQ'}, {'rc_mode':'QVBR'}, {'rc_mode':'AVBR'}, {'rc_mode':'CBR'}, {'video_codec':'copy','audio_codec':'copy'},
    {'video_codec':'libx264','hw_accel':'none'}, {'video_codec':'h264_qsv','hw_accel':'qsv'},
    {'video_codec':'hevc_vaapi'}, {'resolution':'source'}, {'resolution':'1600x1000'},
    {'audio_codec':'none'}, {'audio_codec':'flac'}, {'audio_codec':'pcm_s24le'},
])
def test_changing_each_inactive_field_does_not_change_the_generated_command(changes):
    opts = FFmpegOptions(**changes)
    original = build_command(opts)
    alternatives = dict(hw_accel='qsv', device='/dev/dri/renderD129', resolution='1080p',
        aspect='4:3', fps='60', gop='120', profile='baseline', level='5.1', vf_preset='null',
        low_power=False, rc_mode='VBR', global_quality='34', async_depth='8',
        video_bitrate='8000k', maxrate='10000k', bufsize='20000k',
        audio_bitrate='320k', audio_channels='6', audio_rate='44100')
    for key in disabled_parameters(opts):
        altered = FFmpegOptions(**{**asdict(opts), key: alternatives[key]})
        assert build_command(altered) == original, (changes, key)


@pytest.mark.parametrize('changes,disabled,enabled', [
    ({}, ['-preset','-crf','-tune','-hls_time','-hls_list_size','-hls_flags','-live'], ['-mpegts_flags','-muxdelay','-rw_timeout','-threads']),
    ({'video_codec':'libx264','hw_accel':'none'}, [], ['-preset','-crf','-tune']),
    ({'video_codec':'h264_qsv','hw_accel':'qsv'}, ['-crf','-tune'], ['-preset']),
    ({'video_codec':'h264_nvenc','hw_accel':'none'}, ['-crf'], ['-preset','-tune']),
    ({'video_codec':'copy','audio_codec':'aac'}, ['-preset','-crf','-tune'], ['-threads']),
    ({'video_codec':'copy','audio_codec':'copy'}, ['-threads'], ['-rw_timeout']),
    ({'output_format':'hls'}, ['-mpegts_flags','-muxdelay','-live'], ['-hls_time','-hls_list_size','-hls_flags']),
    ({'output_format':'matroska'}, ['-mpegts_flags','-muxdelay','-hls_time'], ['-live']),
])
def test_advanced_options_match_codec_and_container(changes, disabled, enabled):
    reasons = disabled_parameters(FFmpegOptions(**changes), 'advanced')
    assert all(reasons.get(k) for k in disabled)
    assert all(k not in reasons for k in enabled)


@pytest.mark.parametrize('hardware', ['none','vaapi','qsv'])
def test_filter_choices_match_the_actual_filter_renderer(hardware):
    from app.services.ffmpeg_templates import VF_PRESETS, vf_snippet
    reasons = disabled_parameters(FFmpegOptions(hw_accel=hardware), 'options')
    for preset in VF_PRESETS:
        if preset.id != 'none':
            assert ('vf_preset:' + preset.id in reasons) == (not bool(vf_snippet(preset.id, hardware)))
    assert 'subs:keep' in reasons
    assert 'subs:keep' not in disabled_parameters(FFmpegOptions(output_format='matroska'), 'options')


async def test_disabled_values_are_saved_unchanged_and_validated_when_reactivated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        result = await c.post('/api/ffmpeg', json={'name':'Latent settings', 'command_source':'fields',
            'rc_mode':'VBR', 'global_quality':'52', 'audio_codec':'copy', 'audio_rate':'0'})
        assert result.status_code == 200, result.text
        item = result.json()['item']
        assert item['global_quality'] == '52' and item['audio_rate'] == '0'
        assert '-global_quality' not in shlex.split(item['command'])
        assert '-ar' not in shlex.split(item['command'])
        for payload in ({'rc_mode':'CQP'}, {'audio_codec':'aac'}):
            failed = await c.put(f'/api/ffmpeg/{item["id"]}', json=payload)
            assert failed.status_code == 422, failed.text
        saved = (await c.get('/api/ffmpeg')).json()['items'][0]
        assert saved['rc_mode'] == 'VBR' and saved['audio_codec'] == 'copy'
        restored = await c.put(f'/api/ffmpeg/{item["id"]}', json={
            'rc_mode':'CQP', 'global_quality':'26', 'audio_codec':'aac', 'audio_rate':'48000'})
        assert restored.status_code == 200, restored.text


async def test_helper_rejects_known_inactive_options_even_if_typed_as_custom():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        for flag, side in [('-crf','output'), ('-hls_time','output'), ('-live','output')]:
            result = await c.post('/api/ffmpeg/extra-option', json={
                'raw':'', 'side':side, 'flag':flag, 'value':'1', 'options':asdict(FFmpegOptions())})
            assert result.status_code == 422, result.text
        result = await c.post('/api/ffmpeg/extra-option', json={
            'raw':'', 'side':'output', 'flag':'-hls_time', 'value':'6',
            'options':asdict(FFmpegOptions(output_format='hls'))})
        assert result.status_code == 200, result.text


def test_inactive_values_still_respect_database_storage_limits():
    assert field_errors(FFmpegOptions(video_codec='copy', fps='1' * 100))
    assert schema()['applicability']


@pytest.mark.parametrize('mode,flags', [
    ('CQP', {'-global_quality'}), ('ICQ', {'-global_quality'}),
    ('QVBR', {'-b:v','-maxrate','-bufsize','-global_quality'}),
    ('VBR', {'-b:v','-maxrate','-bufsize'}), ('CBR', {'-b:v','-bufsize'}),
    ('AVBR', {'-b:v'}), ('AUTO', {'-b:v','-maxrate','-bufsize'}),
])
def test_all_vaapi_rate_modes_render_only_effective_flags(mode, flags):
    tokens = shlex.split(build_command(FFmpegOptions(rc_mode=mode)))
    assert set(tokens) & {'-b:v','-maxrate','-bufsize','-global_quality'} == flags
    if mode == 'CBR':
        # maxrate is otherwise ignored as a rate target, but FFmpeg uses it
        # as a buffer-size fallback when bufsize is absent or zero.
        for bufsize in ('', '0'):
            assert '-maxrate' in shlex.split(build_command(FFmpegOptions(rc_mode=mode, bufsize=bufsize)))
