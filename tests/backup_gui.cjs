// Optional DOM integration test: npm install --no-save --package-lock=false jsdom
// node tests/backup_gui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const dom = new JSDOM(fs.readFileSync('app/templates/settings_backup.html', 'utf8'), {runScripts: 'outside-only'});
const w = dom.window;
const $ = id => w.document.getElementById(id);
const calls = [];
const data = {app: 'stalker-proxy-manager', version: 2, selected_tables: ['settings', 'portals'],
  tables: {settings: [{key: 'keep', value: 'true'}], portals: []}};
const count = {added: 1, existing: 1, runtime_skipped: 0, tables: {settings: {added: 1, existing: 1}}};
let deferPreview;
w.esc = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
w.loadSettings = w.loadEpg = w.loadFavicons = async () => {};
w.URL.createObjectURL = () => 'blob:backup';
w.URL.revokeObjectURL = () => {};
w.HTMLAnchorElement.prototype.click = () => {};
w.api = async (url, options = {}) => {
  calls.push({url, body: options.body});
  if (url.endsWith('/catalog')) return {tables: [{name:'portals', rows:2}, {name:'settings', rows:1}], setting_keys:['keep']};
  if (url.endsWith('/export')) return data;
  if (url.endsWith('/delete-preview')) return {deleted:2, tables:[{name:'portals', deleted:2, references_cleared:0, selected:true}]};
  if (url.endsWith('/delete')) return {deleted:2};
  if (url.endsWith('/preview') && deferPreview) return new Promise(resolve => deferPreview.resolve = resolve);
  if (url.endsWith('/import')) return {imported:1, updated:0, skipped:[]};
  return count;
};
const tick = () => new Promise(resolve => setImmediate(resolve));
async function change(id) { $(id).dispatchEvent(new w.Event('change')); await tick(); }
async function file(value) {
  Object.defineProperty($('import-file'), 'files', {configurable:true, value:[{name:'backup.json', text:async () => JSON.stringify(value)}]});
  await change('import-file');
}
async function click(id) { $(id).click(); await tick(); }
const writes = () => calls.filter(c => c.url.endsWith('/restore') || c.url.endsWith('/import') || c.url.endsWith('/delete'));
(async () => {
  w.eval(fs.readFileSync('app/static/js/settings-backup.js', 'utf8'));
  await tick();
  assert.equal(w.document.querySelectorAll('.backup-table').length, 2);
  assert.equal($('backup-selected').disabled, true);
  await click('backup-select-all');
  assert.equal($('backup-selected').disabled, false);
  await click('backup-selected');
  assert.deepEqual(Array.from(calls.find(c => c.url.endsWith('/export')).body.tables), ['portals', 'settings']);

  await file(data);
  assert.equal(writes().length, 0, 'file selection must not import');
  assert.equal($('restore-run').disabled, true);
  await click('restore-preview');
  assert.equal(writes().length, 0, 'review must not import');
  assert.equal($('restore-confirm-panel').hidden, false);
  assert.equal($('restore-run').disabled, true);
  $('restore-confirm').checked = true; await change('restore-confirm');
  assert.equal($('restore-run').disabled, false);
  $('restore-scope').value = 'settings'; await change('restore-scope');
  assert.equal($('restore-run').disabled, true, 'scope changes invalidate confirmation');
  await click('restore-preview');
  $('restore-confirm').checked = true; await change('restore-confirm');
  await click('restore-run');
  assert.equal(writes().length, 1);
  assert.equal(writes()[0].body.confirm_add_only, true);
  assert.deepEqual(Array.from(writes()[0].body.tables), ['settings']);
  assert.match($('import-result').textContent, /Restore complete/);
  assert.equal($('restore-run').disabled, true);

  // Ignore stale reviews when the user changes a file or selection mid-request.
  deferPreview = {};
  $('restore-preview').click(); await tick();
  $('restore-scope').value = 'portals'; await change('restore-scope');
  deferPreview.resolve(count); await tick(); deferPreview = null;
  assert.equal($('restore-confirm-panel').hidden, true);

  await file({app:'wrong-app', version:2});
  assert.equal($('restore-options').hidden, true);
  assert.match($('import-result').textContent, /Choose a Stalker/);
  await file({app:'stalker-proxy-manager', version:1, settings:{key:true}});
  await click('restore-preview');
  assert.match($('restore-review').textContent, /Legacy/);
  $('restore-confirm').checked = true; await change('restore-confirm');
  await click('restore-run');
  assert.equal(writes().at(-1).body.mode, 'add_only');

  await click('delete-preview');
  assert.equal($('delete-run').disabled, true);
  $('delete-confirm').checked = true; await change('delete-confirm');
  $('delete-phrase').value = 'DELETE EVERYTHING'; $('delete-phrase').dispatchEvent(new w.Event('input'));
  assert.equal($('delete-run').disabled, true);
  $('delete-phrase').value = 'DELETE SELECTED'; $('delete-phrase').dispatchEvent(new w.Event('input'));
  assert.equal($('delete-run').disabled, false);
  $('delete-scope').value = 'all'; await change('delete-scope');
  assert.equal($('delete-run').disabled, true);
  await click('delete-preview');
  assert.equal($('delete-required').textContent, 'DELETE EVERYTHING');
  $('delete-phrase').value = 'DELETE EVERYTHING'; $('delete-phrase').dispatchEvent(new w.Event('input'));
  $('delete-confirm').checked = true; await change('delete-confirm');
  await click('delete-run');
  assert.equal(writes().at(-1).body.all, true);
  assert.equal(writes().at(-1).body.confirm_delete, true);
  assert.equal(writes().at(-1).body.confirmation, 'DELETE EVERYTHING');
  assert.match($('delete-status').textContent, /Deletion complete/);
  assert.equal($('delete-run').disabled, true);
  console.log('Backup GUI checks passed: downloads, file validation, review, confirmation, stale review, legacy restore and typed deletion.');
  dom.window.close();
})().catch(err => { console.error(err); dom.window.close(); process.exitCode = 1; });
