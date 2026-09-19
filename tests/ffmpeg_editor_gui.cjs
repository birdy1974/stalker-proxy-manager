// Optional GUI regression checks: npm install --no-save --package-lock=false jsdom
// node tests/ffmpeg_editor_gui.cjs  (uses .venv/bin/python, or set PYTHON)
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {execFileSync} = require('node:child_process');
const {JSDOM} = require('jsdom');
const config = JSON.parse(execFileSync(process.env.PYTHON || '.venv/bin/python', ['-c',
  'import json; from app.services.ffmpeg_editor import schema; print(json.dumps(schema()))'], {encoding:'utf8'}));
const dom = new JSDOM(fs.readFileSync('app/templates/ffmpeg.html', 'utf8'), {runScripts:'outside-only', pretendToBeVisual:true});
const w = dom.window, node = id => w.document.getElementById(id);
w.el = (tag, attrs = {}, ...children) => {
  const n = w.document.createElement(tag);
  Object.entries(attrs).forEach(([k,v]) => k.startsWith('on') ? n.addEventListener(k.slice(2),v) : n.setAttribute(k,v));
  children.flat().forEach(c => n.append(c)); return n;
};
w.bootstrap = {Tooltip: class {setContent(){} show(){} hide(){} dispose(){}}};
w.eval(fs.readFileSync('app/static/js/help-tooltips.js','utf8'));
const calls = [];
w.api = async (url, request) => { calls.push({url,...request}); return {extra:'-preset slow'}; };
w.eval(fs.readFileSync('app/static/js/ffmpeg-editor.js','utf8') + '; window.Editor = FFmpegEditor;');
const editor = w.Editor;
const tick = () => new Promise(resolve => setImmediate(resolve));
// Closing jsdom queues observer notifications. Do not let those notifications
// create a new animation timer after jsdom has torn down its document.
const closeWindow = () => { w.requestAnimationFrame = () => 0; dom.window.close(); };
(async () => {
  editor.setup(config);
  editor.setValue('video_codec','h264_vaapi'); editor.setValue('hw_accel','vaapi');
  editor.setValue('rc_mode','CQP'); editor.refresh();
  assert.equal(w.document.querySelectorAll('[data-ff-help]').length, Object.keys(config.fields).length);
  editor.setValue('fps','47.5');
  assert.equal(editor.value('fps'),'47.5');
  assert.equal(node('f-fps-custom').hidden,false);
  editor.setValue('fps','25');
  assert.equal(node('f-fps-custom').hidden,true);
  editor.setValue('global_quality','52');
  assert.equal(editor.valid(),false);
  assert.match(node('f-global_quality-custom').validationMessage,/at most 51/);
  editor.setValue('global_quality','26');
  assert.equal(editor.valid(),true);
  editor.setValue('resolution','1600x1000');
  assert.equal(editor.value('resolution'),'1600x1000');
  editor.setValue('extra_input','-rw_timeout 20000000 -user_agent "My Player"');
  assert.match(editor.value('extra_input'),/20000000/);
  editor.mode(true);
  assert.equal(node('ff-advanced-add').disabled,true);
  editor.mode(false);
  editor.setValue('video_codec','libx264'); editor.setValue('hw_accel','none'); editor.refresh();
  const preset = config.advanced.findIndex(d => d.flag === '-preset');
  node('ff-advanced-option').value = String(preset);
  node('ff-advanced-option').dispatchEvent(new w.Event('change'));
  node('ff-advanced-value').value = 'slow';
  node('ff-advanced-add').click(); await tick();
  assert.equal(calls[0].body.flag,'-preset');
  assert.equal(calls[0].body.side,'output');
  assert.equal(editor.value('extra_output'),'-preset slow');

  // Mode changes disable both the preset and its custom input, without
  // discarding the value. Invalid dormant values do not block another mode.
  editor.setValue('video_bitrate','2250k');
  editor.setValue('video_codec','h264_vaapi'); editor.setValue('hw_accel','vaapi');
  editor.setValue('rc_mode','CQP'); editor.refresh();
  for (const key of ['video_bitrate','maxrate','bufsize']) assert.equal(node('f-'+key).disabled,true);
  assert.equal(node('f-video_bitrate-custom').disabled,true);
  assert.match(node('f-video_bitrate-inactive').textContent,/CQP/);
  assert.equal(node('f-global_quality').disabled,false);
  editor.setValue('global_quality','52'); assert.equal(editor.valid(),false);
  editor.setValue('rc_mode','VBR'); editor.refresh();
  assert.equal(node('f-global_quality').disabled,true);
  assert.equal(node('f-global_quality-custom').disabled,true);
  assert.equal(editor.valid(),true);
  assert.equal(node('f-video_bitrate-custom').disabled,false);
  assert.equal(editor.value('video_bitrate'),'2250k');
  editor.setValue('rc_mode','CQP'); editor.refresh(); assert.equal(editor.valid(),false);
  editor.setValue('global_quality','26');
  for (const [mode,disabled] of [
    ['ICQ',['video_bitrate','maxrate','bufsize']], ['QVBR',[]],
    ['AVBR',['maxrate','bufsize','global_quality']], ['CBR',['maxrate','global_quality']],
    ['AUTO',['global_quality']], ['VBR',['global_quality']],
  ]) {
    editor.setValue('rc_mode',mode); editor.refresh();
    for (const key of ['video_bitrate','maxrate','bufsize','global_quality'])
      assert.equal(node('f-'+key).disabled,disabled.includes(key),mode+': '+key);
  }
  editor.setValue('rc_mode','CBR'); editor.setValue('bufsize',''); editor.refresh();
  assert.equal(node('f-maxrate').disabled,false); // FFmpeg buffer-size fallback
  editor.setValue('bufsize','2000k'); editor.refresh(); assert.equal(node('f-maxrate').disabled,true);
  editor.setValue('rc_mode','CQP');
  editor.setValue('video_codec','copy'); editor.refresh();
  for (const key of ['hw_accel','device','resolution','aspect','fps','gop','profile','level',
                    'vf_preset','low_power','rc_mode','global_quality','async_depth'])
    assert.equal(node('f-'+key).disabled,true,key);
  assert.equal(node('f-output_format').disabled,false);
  assert.equal(node('f-audio_codec').disabled,false);
  editor.setValue('video_codec','hevc_vaapi'); editor.refresh();
  assert.equal(node('f-profile').disabled,true);
  assert.equal(node('f-level').disabled,true);
  assert.equal(node('f-low_power').disabled,true);
  assert.equal(node('f-rc_mode').disabled,false);
  editor.setValue('video_codec','libx264'); editor.setValue('hw_accel','none'); editor.refresh();
  assert.equal(node('f-device').disabled,true);
  assert.equal(node('f-rc_mode').disabled,true);
  assert.equal(node('f-profile').disabled,false);
  assert.equal(node('f-video_bitrate').disabled,false); // CQP is VAAPI-only
  assert.equal(node('f-aspect').disabled,true); // explicit dimensions
  editor.setValue('resolution','source'); editor.refresh(); assert.equal(node('f-aspect').disabled,true);
  editor.setValue('resolution','720p'); editor.refresh(); assert.equal(node('f-aspect').disabled,false);
  editor.setValue('resolution','1600x1000'); editor.refresh();
  for (const codec of ['copy','none']) {
    editor.setValue('audio_codec',codec); editor.refresh();
    for (const key of ['audio_bitrate','audio_channels','audio_rate']) assert.equal(node('f-'+key).disabled,true);
  }
  editor.setValue('audio_codec','flac'); editor.refresh();
  assert.equal(node('f-audio_bitrate').disabled,true);
  assert.equal(node('f-audio_rate').disabled,false);
  editor.setValue('audio_codec','aac'); editor.refresh(); assert.equal(node('f-audio_bitrate').disabled,false);
  const subsKeep = [...node('f-subs').options].find(o => o.value === 'keep');
  assert.equal(subsKeep.disabled,true);
  const gpuFilter = [...node('f-vf_preset').options].find(o => o.value === 'deint-vaapi-frame');
  assert.equal(gpuFilter.disabled,true);
  editor.setValue('output_format','matroska'); editor.refresh(); assert.equal(subsKeep.disabled,false);
  const hls = config.advanced.findIndex(d => d.flag === '-hls_time');
  node('ff-advanced-option').value = String(hls);
  node('ff-advanced-option').dispatchEvent(new w.Event('change'));
  assert.equal(node('ff-advanced-value').disabled,true);
  assert.equal(node('ff-advanced-add').disabled,true);
  assert.equal(node('ff-advanced-option').disabled,false); // can select another option
  editor.setValue('output_format','hls'); editor.refresh();
  assert.equal(node('ff-advanced-value').disabled,false);
  assert.equal(node('ff-advanced-add').disabled,false);
  editor.mode(true); assert.equal(node('f-resolution').disabled,true);
  assert.equal(node('f-command').disabled,false);
  assert.equal(node('f-name').disabled,false); assert.equal(node('f-enabled').disabled,false);
  editor.mode(false,true); assert.equal(node('f-command').disabled,true);
  assert.equal(node('f-enabled').disabled,false);
  editor.mode(false); assert.equal(node('f-resolution').disabled,false);
  assert.equal(editor.value('video_bitrate'),'2250k');

  assert.equal(node('ff-expand'),null);
  assert.equal(editor.expand,undefined);
  assert.equal(w.document.querySelector('.modal'),null);
  assert.ok(node('ff-editor-card').isConnected);
  let saves = 0;
  node('tpl-save').addEventListener('click',() => saves++);
  node('tpl-save').click(); assert.equal(saves,1);
  assert.equal([...node('f-fps').options].find(o => o.value === '').textContent,'src (source FPS)');
  assert.equal(editor.value('resolution'),'1600x1000');
  // Exercise the actual page handlers: the header button shares the bottom
  // action and both stay disabled until the same request finishes.
  const page = fs.readFileSync('app/templates/ffmpeg.html','utf8');
  const saveHandlers = page.slice(page.indexOf('$("#tpl-save-top").addEventListener'),
                                 page.indexOf('$("#tpl-delete").addEventListener'));
  w.$ = selector => w.document.querySelector(selector);
  let finishSave, requests = 0;
  w.api = () => { requests++; return new Promise(resolve => { finishSave = resolve; }); };
  w.eval(`
    const FFmpegEditor = window.Editor;
    let current = null, editorRevision = 0, syncTimer, parseTimer;
    const sourceOf = () => 'fields', isRedirectTpl = () => false;
    const readFields = () => ({name:'Header save test'});
    const toast = () => {}, loadList = async () => {}, selectTemplate = () => {};
    ${saveHandlers}
  `);
  assert.ok(node('tpl-save-top').closest('.card-header'));
  node('tpl-save-top').click();
  assert.equal(requests,1);
  assert.equal(node('tpl-save').disabled,true);
  assert.equal(node('tpl-save-top').disabled,true);
  node('tpl-save').click(); node('tpl-save-top').click();
  assert.equal(requests,1,'disabled buttons must not start duplicate saves');
  finishSave({item:{id:42}}); await tick();
  assert.equal(node('tpl-save').disabled,false);
  assert.equal(node('tpl-save-top').disabled,false);
  console.log('FFmpeg editor DOM checks passed: help, validation, dependencies, restored values, advanced helper and main-pane editing without a popup.');
  closeWindow();
})().catch(error => { console.error(error); closeWindow(); process.exitCode = 1; });
