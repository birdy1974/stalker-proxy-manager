// Optional: node tests/help_tooltips_gui.cjs (requires jsdom).
// Uses the real vendored Bootstrap Tooltip, including its manual-trigger lifecycle.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const dom = new JSDOM(`<body>
  <style>[data-help],[data-help-detail]{display:none!important}</style>
  <section class="card"><div class="card-header">Settings</div><div class="card-body">
    <label for="setting">Setting</label><input id="setting">
    <div id="help" data-help="Setting" data-help-for="setting">Explanatory help.</div>
    <div id="status" class="small text-muted" role="status">Saving…</div>
    <div id="warning" class="alert alert-warning">Permanent deletion.</div>
    <label id="native" title="Existing label explanation">Other setting</label>
  </div></section>
</body>`, {runScripts:'outside-only', pretendToBeVisual:true});
const w = dom.window, d = w.document;
w.eval(fs.readFileSync('app/static/vendor/bootstrap/bootstrap.bundle.min.js','utf8'));
w.eval(fs.readFileSync('app/static/js/help-tooltips.js','utf8'));
const helpers = w.HelpTooltips;
const tick = (ms = 30) => new Promise(resolve => setTimeout(resolve, ms));
const tips = () => [...d.querySelectorAll('.help-tooltip.show')];
const button = name => [...d.querySelectorAll('.help-tip-button')].find(n => n.getAttribute('aria-label') === name);
// Closing jsdom queues observer notifications. Do not let those notifications
// create a new animation timer after jsdom has torn down its document.
const closeWindow = () => { w.requestAnimationFrame = () => 0; dom.window.close(); };
(async () => {
  helpers.scan();
  assert.equal(d.querySelectorAll('.help-tip-button').length,2);
  assert.equal(w.getComputedStyle(d.getElementById('help')).display,'none');
  assert.notEqual(w.getComputedStyle(d.getElementById('status')).display,'none');
  assert.notEqual(w.getComputedStyle(d.getElementById('warning')).display,'none');
  assert.equal(d.getElementById('native').hasAttribute('title'),false);
  const b = button('Setting: help');
  assert.equal(b.previousElementSibling.tagName,'LABEL');
  assert.equal(b.type,'button');
  b.dispatchEvent(new w.Event('mouseenter'));
  assert.equal(tips().length,1);
  b.focus(); b.click(); await tick();
  // Repeated show through hover/focus/click must not trigger Bootstrap's
  // own queued hide; this was caught by the real-browser regression.
  assert.equal(tips().length,1);
  assert.equal(tips()[0].textContent,'Explanatory help.');
  assert.ok(d.getElementById(b.getAttribute('aria-describedby')));
  d.getElementById('help').textContent = '<img src=x onerror=alert(1)> is plain text';
  await tick();
  assert.equal(tips().length,1);
  assert.match(tips()[0].textContent,/<img/);
  assert.equal(tips()[0].querySelector('img'),null);
  d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
  assert.equal(tips().length,0);
  b.blur(); b.focus(); assert.equal(tips().length,1);
  d.getElementById('setting').focus(); await tick(220);
  assert.equal(tips().length,0);
  b.click(); assert.equal(tips().length,1);
  b.click(); assert.equal(tips().length,0);
  b.click();
  d.getElementById('status').dispatchEvent(new w.Event('pointerdown',{bubbles:true}));
  assert.equal(tips().length,0);

  const baseline = d.querySelectorAll('.help-tip-button').length;
  for (let i=0;i<3;i++) {
    const modal = d.createElement('div'); modal.className = 'modal';
    modal.innerHTML = '<h6 class="modal-title">Dialog</h6><div id="dynamic" data-help="Dialog details"></div><button id="action">Save</button>';
    d.body.append(modal); await tick();
    const hint = button('Dialog details: help');
    assert.equal(hint.hidden,true);
    d.getElementById('dynamic').textContent = 'New dynamic help'; await tick();
    assert.equal(hint.hidden,false);
    hint.focus(); assert.equal(tips().length,1);
    modal.dispatchEvent(new w.Event('hide.bs.modal',{bubbles:true}));
    assert.equal(tips().length,0);
    modal.remove(); await tick();
    assert.equal(d.querySelectorAll('.help-tip-button').length,baseline);
    assert.equal(d.querySelectorAll('.help-tooltip').length,0);
  }
  helpers.scan(); helpers.scan();
  assert.equal(d.querySelectorAll('.help-tip-button').length,baseline,'mounting is idempotent');
  console.log('Help tooltip DOM checks passed: real Bootstrap hover/focus/click, Escape/outside dismissal, safe text, dynamic refresh, modal cleanup and visible statuses/warnings.');
  closeWindow();
})().catch(error => { console.error(error); closeWindow(); process.exitCode=1; });
