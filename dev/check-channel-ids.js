#!/usr/bin/env node
/*
Runtime-check the Channel-IDs second phase of the package comparison
(app/templates/portals.html → showGenreMatrix):

  1. /portals renders; showGenreMatrix is reachable;
  2. with channel_ids present and an id conflict, the Summary tab shows the
     danger badge and the "Channel IDs" tab carries the ⚠ marker;
  3. opening the tab lists the conflicted channel with BOTH ids (one column
     per MAC), the missing channel with an em-dash, and the identical channel
     only in the counts line;
  4. the kind/mode/search controls filter the rows;
  5. the Channel IDs tab's Save/Clear id-translation controls POST/DELETE the
     right payload (kind=live + exactly the differ rows) and update the
     saved-count badge;
  6. with channel_ids absent (legacy genre-only response) no Channel IDs tab
     appears at all.

Usage: node check-channel-ids.js <rendered-portals.html> <app.js>
Exit:  0 all assertions hold, 1 otherwise.
*/
'use strict';
const fs = require('fs');
let JSDOM, VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = require('jsdom'));
} catch {
  console.log('SKIP: jsdom not installed (run: npm install --prefix dev jsdom)');
  process.exit(2);
}

const [PAGE, APPJS] = process.argv.slice(2);
if (!PAGE || !APPJS) {
  console.error('usage: node check-channel-ids.js <page.html> <app.js>');
  process.exit(1);
}
const pageHtml = fs.readFileSync(PAGE, 'utf8');
const appJs = fs.readFileSync(APPJS, 'utf8');

const problems = [];
const fail = (p) => problems.push(p);
process.on('uncaughtException', (e) => {
  console.error('check-channel-ids: FAILED (uncaught)');
  console.error(e.stack || String(e));
  process.exit(1);
});

function stubResponse(data, status = 200) {
  return { status, ok: status < 400, statusText: 'OK',
           json: async () => data, text: async () => JSON.stringify(data) };
}
const CALLS = [];
function stubFetch(path, opts = {}) {
  if (typeof path !== 'string') path = String(path);
  const method = (opts && opts.method) || 'GET';
  /* the translation endpoints live UNDER /api/portals/{pid}/…, so this match
     must run before the generic prefix branch below */
  if (path.includes('/channel-id-translations')) {
    CALLS.push({ path, method, body: (opts && opts.body) || null });
    if (method === 'POST')
      return stubResponse({ ok: true, saved: 1, unchanged: 1,
                            skipped: { ambiguous: 0, unmatched: 0,
                                       unknown_mac: 0, empty_cmd: 0 },
                            count: 2 });
    if (method === 'DELETE')
      return stubResponse({ ok: true, cleared: 2 });
    return stubResponse({ ok: true, count: 0 });
  }
  if (path.startsWith('/api/portals'))
    return stubResponse({ items: [{ id: 1, name: 'Demo', status: 'online',
                                    macs: [{ id: 11, mac: '00:1A:79:00:00:01', online: true, status: 'online' },
                                           { id: 12, mac: '00:1A:79:00:00:02', online: true, status: 'online' }] },
                                   { id: 2, name: 'Other', status: 'online', macs: [] }],
                          jobs: [] });
  if (path.startsWith('/api/jobs')) return stubResponse({ items: [] });
  return stubResponse({ items: [], jobs: [] });
}

/* splice the REAL app.js source into its <script src> tag (same trick as
   check-genre-popup.js) so top-level consts share the page's global scope. */
const TAG_OPEN = '<script src="/static/js/app.js';
const tagStart = pageHtml.indexOf(TAG_OPEN);
const closeBracket = tagStart >= 0 ? pageHtml.indexOf('>', tagStart) : -1;
const CLOSE_TAG = '<' + '/script>';
const tagEnd = closeBracket >= 0 ? pageHtml.indexOf(CLOSE_TAG, closeBracket) : -1;
if (tagStart < 0 || tagEnd < 0) {
  fail('could not find the app.js script tag in the page');
  process.exit(1);
}
const injected = pageHtml.slice(0, tagStart) + '<script>' + appJs + pageHtml.slice(tagEnd);

const pageErrors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => pageErrors.push(String(e.message || e)));
vc.on('error', (m) => pageErrors.push(String(m)));

const dom = new JSDOM(injected, {
  url: 'http://localhost/portals',
  runScripts: 'dangerously',
  virtualConsole: vc,
  beforeParse(window) {
    window.bootstrap = { Modal: class { constructor() {} show() {} hide() {} } };
    window.fetch = (path, opts) => Promise.resolve(stubFetch(path, opts));
  },
});
const { window } = dom;
const { document } = window;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* canned compare response: two packages, one id conflict, one missing row */
const FIXTURE = {
  ok: true, portal_id: 1, name: 'Demo', compared: 2, skipped: [],
  identical: false,
  message: 'live: 1 genre(s) only on some MACs · live: 1 channel(s) with different ids',
  fetched_at: '2026-09-22T10:00:00',
  macs: [{ mac: '00:1A:79:00:00:01', ok: true, error: '', live: 2, vod: 0, series: 0,
           only_live: 1, only_vod: 0, only_series: 0 },
         { mac: '00:1A:79:00:00:02', ok: true, error: '', live: 2, vod: 0, series: 0,
           only_live: 1, only_vod: 0, only_series: 0 }],
  results: [
    { mac: '00:1A:79:00:00:01', mac_id: 11, ok: true, error: '',
      live: [{ key: 'id:1', name: 'News', id: '1' },
             { key: 'id:2', name: 'Sport', id: '2' }],
      vod: [], series: [] },
    { mac: '00:1A:79:00:00:02', mac_id: 12, ok: true, error: '',
      live: [{ key: 'id:1', name: 'News', id: '1' },
             { key: 'id:3', name: 'Kids', id: '3' }],
      vod: [], series: [] },
  ],
  packages: [
    { id: 1, name: 'Package A', mac_ids: [11], macs: ['00:1A:79:00:00:01'],
      counts: { live: 2, vod: 0, series: 0 }, ok: true },
    { id: 2, name: 'Package B', mac_ids: [12], macs: ['00:1A:79:00:00:02'],
      counts: { live: 2, vod: 0, series: 0 }, ok: true },
  ],
  live: { common: [{ key: 'id:1', name: 'News' }],
          only: { '00:1A:79:00:00:01': [{ key: 'id:2', name: 'Sport' }],
                  '00:1A:79:00:00:02': [{ key: 'id:3', name: 'Kids' }] },
          counts: {}, identical: false, macs_compared: 2 },
  vod: { common: [], only: {}, counts: {}, identical: true, macs_compared: 2 },
  series: { common: [], only: {}, counts: {}, identical: true, macs_compared: 2 },
  stored: { live: 3, vod: 0, series: 0 },
  mac_counts_stored: 2,
  channel_ids: {
    requested: ['live'],
    enabled: { live: 2 },
    translations: { count: 0 },
    message: 'live: 1 channel(s) with different ids',
    results: [],
    kinds: {
      live: {
        kind: 'live',
        macs: ['00:1A:79:00:00:01', '00:1A:79:00:00:02'],
        counts: { same: 1, differ: 1, missing: 1 },
        total: 3,
        identical: false,
        truncated: false,
        failed: {},
        rows: [
          { name: 'BBC One', key: 'bbc one', status: 'differ',
            ids: { '00:1A:79:00:00:01': '10', '00:1A:79:00:00:02': '99' },
            cmds: { '00:1A:79:00:00:01': 'cmdA', '00:1A:79:00:00:02': 'cmdB' },
            genres: { '00:1A:79:00:00:01': '1', '00:1A:79:00:00:02': '1' },
            absent: [] },
          { name: 'Only A', key: 'only a', status: 'missing',
            ids: { '00:1A:79:00:00:01': '12' }, cmds: {}, genres: { '00:1A:79:00:00:01': '1' },
            absent: ['00:1A:79:00:00:02'] },
        ],
      },
    },
  },
};
const STORED = {
  live: [{ id: 1, genre_portal_id: '1', name: 'News', enabled: true },
         { id: 2, genre_portal_id: '2', name: 'Sport', enabled: true },
         { id: 3, genre_portal_id: '3', name: 'Kids', enabled: false }],
  vod: [], series: [],
};
/* genre-only response: no channel_ids key at all (legacy / kinds=[]) */
const LEGACY = { ...FIXTURE, channel_ids: null, message: 'packages differ' };

(async () => {
  await sleep(300); // let makeTable's first /api/portals fetch resolve
  if (typeof window.showGenreMatrix !== 'function') {
    fail('showGenreMatrix is not a global function');
    finish();
    return;
  }

  /* ---- 2. open with channel_ids: badge + ⚠ tab ---- */
  try {
    window.showGenreMatrix(1, structuredClone(FIXTURE), structuredClone(STORED));
  } catch (e) {
    fail(`showGenreMatrix(fixture) threw: ${e.stack}`);
    finish();
    return;
  }
  await sleep(50);
  const tabBtns = () => [...document.querySelectorAll('.cmp-tab')];
  const byLabel = (t) => tabBtns().find((b) => b.textContent.trim().startsWith(t));

  const chTab = byLabel('Channel IDs');
  if (!chTab) fail('Channel IDs tab missing when channel_ids is present');
  else if (!chTab.textContent.includes('⚠')) fail('Channel IDs tab is missing the ⚠ marker');

  const summaryAlert = [...document.querySelectorAll('.alert')].map(a => a.textContent).join(' ');
  if (!/Channel ids differ across packages/i.test(summaryAlert))
    fail('Summary tab lacks the id-conflict danger badge');
  const openBtn = document.getElementById('sum-ch-open');
  if (!openBtn) fail('"Open Channel IDs" button missing on Summary');

  /* ---- 3. open the tab: conflicted ids side by side ---- */
  (openBtn || chTab)?.click();
  await sleep(30);
  const rowText = (rows, name) => rows.find(r => r.cells[0]?.textContent.trim() === name);
  const currentRows = () => [...document.querySelectorAll('.cmp-matrix tbody tr')];
  let rows = currentRows();
  const bbc = rowText(rows, 'BBC One');
  if (!bbc) fail('BBC One row not rendered in Channel IDs tab');
  else {
    const cells = [...bbc.cells].map(c => c.textContent.trim());
    if (!cells.includes('10') || !cells.includes('99'))
      fail(`BBC One row should show both ids 10 and 99, got: ${cells.join(' | ')}`);
    if (!bbc.querySelector('.text-danger')) fail('BBC One id cells are not marked dangerous');
    if (!/id differs/.test(bbc.textContent)) fail('BBC One row missing "id differs" badge');
  }
  /* default filter = id conflicts only → the missing row stays hidden … */
  if (rowText(rows, 'Only A')) fail('default mode must hide missing rows (id-conflicts only)');
  const countsLine = [...document.querySelectorAll('.col-md-3.small, .text-end.small')]
    .map(n => n.textContent).join(' ');
  if (!/1 id-conflict/.test(countsLine) || !/1 identical/.test(countsLine))
    fail(`counts line should show 1 id-conflict · … · 1 identical, got: "${countsLine}"`);

  /* ---- 4. filters ---- */
  const mode = document.getElementById('cmp-cmode');
  if (!mode) fail('#cmp-cmode mode selector missing');
  else {
    if (mode.value !== 'differ') fail(`default mode should be "differ", got "${mode.value}"`);
    mode.value = 'missing';
    mode.dispatchEvent(new window.Event('change'));
    await sleep(20);
    rows = currentRows();
    const only = rowText(rows, 'Only A');
    if (rows.length !== 1 || !only)
      fail(`mode=missing should show only Only A, got ${rows.length} row(s)`);
    else {
      if (!only.textContent.includes('—')) fail('Only A missing cell should show an em-dash');
      if (!/missing/.test(only.textContent)) fail('Only A row missing "missing" badge');
    }
    mode.value = 'all';
    mode.dispatchEvent(new window.Event('change'));
    await sleep(20);
    rows = currentRows();
    if (!rowText(rows, 'BBC One') || !rowText(rows, 'Only A'))
      fail(`mode=all should show both flagged rows, got ${rows.length} row(s)`);
    if (rowText(rows, 'Same Chan')) fail('identical channels must not be listed as rows');
    mode.value = 'differ';
    mode.dispatchEvent(new window.Event('change'));
    await sleep(20);
    rows = currentRows();
    if (rows.length !== 1 || !rowText(rows, 'BBC One'))
      fail(`mode=differ should show only BBC One, got ${rows.length} row(s)`);
  }
  const search = document.getElementById('cmp-cq');
  if (!search) fail('#cmp-cq channel search box missing');
  else {
    search.value = 'bbc';
    search.dispatchEvent(new window.Event('change'));
    await sleep(20);
    rows = currentRows();
    if (rows.length !== 1 || !rowText(rows, 'BBC One'))
      fail(`search "bbc" should keep BBC One only, got ${rows.length} row(s)`);
  }

  /* ---- 5. id translations: save POSTs the differ rows, clear resets ---- */
  const saveBtn = document.getElementById('ch-save-trans');
  if (!saveBtn) fail('#ch-save-trans missing on the live Channel IDs tab');
  else if (!/^Save 1 id translation$/.test(saveBtn.textContent.trim()))
    fail(`save button should read "Save 1 id translation", got: "${saveBtn.textContent.trim()}"`);
  if (document.getElementById('ch-trans-count'))
    fail('no saved-count badge expected before save (fixture count is 0)');
  if (document.getElementById('ch-clear-trans'))
    fail('no Clear control expected before save (fixture count is 0)');
  if (saveBtn) {
    saveBtn.click();
    await sleep(30);
    const post = CALLS.find(c => c.method === 'POST' && c.path.includes('/channel-id-translations'));
    if (!post) fail('save did not POST /channel-id-translations');
    else {
      let body = null;
      try { body = JSON.parse(post.body); } catch {}
      if (!body || body.kind !== 'live')
        fail(`POST body kind must be "live", got: ${post.body}`);
      else if (!Array.isArray(body.rows) || body.rows.length !== 1 ||
               body.rows[0].key !== 'bbc one' || body.rows[0].status !== 'differ')
        fail(`POST body must carry exactly the differ row (bbc one), got: ` +
             JSON.stringify((body && body.rows || []).map(r => `${r.key}:${r.status}`)));
    }
    const cnt = document.getElementById('ch-trans-count');
    if (!cnt || !/2 saved/.test(cnt.textContent))
      fail(`after save the badge should read "2 saved …", got: "${cnt && cnt.textContent}"`);
    if (!document.getElementById('ch-clear-trans'))
      fail('Clear control should appear after save');
  }
  const clrBtn = document.getElementById('ch-clear-trans');
  if (clrBtn) {
    clrBtn.click();
    await sleep(30);
    if (!CALLS.some(c => c.method === 'DELETE' && c.path.includes('/channel-id-translations')))
      fail('clear did not DELETE /channel-id-translations');
    if (document.getElementById('ch-trans-count'))
      fail('saved-count badge should be gone after clear');
    if (document.getElementById('ch-clear-trans'))
      fail('Clear control should be gone after clear');
    if (!document.getElementById('ch-save-trans'))
      fail('save control should remain after clear (the conflicts are still there)');
  }

  /* ---- 6. legacy response: no Channel IDs tab ----
     The bootstrap Modal stub never fires `hidden`, so drop the first modal
     from the DOM before opening the legacy one (a real browser would). */
  [...document.querySelectorAll('.modal')].forEach(m => m.remove());
  window.showGenreMatrix(1, structuredClone(LEGACY), structuredClone(STORED));
  await sleep(50);
  if (byLabel('Channel IDs')) fail('Channel IDs tab must not appear when channel_ids is null');
  if ([...document.querySelectorAll('.alert')].some(a => /Channel ids differ/i.test(a.textContent)))
    fail('id-conflict badge must not appear when channel_ids is null');
  if (document.getElementById('ch-save-trans'))
    fail('Save id-translation control must not appear when channel_ids is null');

  const fatal = pageErrors.filter((e) => !/Could not load|css|script|favicon/i.test(e));
  if (fatal.length) fail('page errors: ' + fatal.slice(0, 3).join(' | '));

  finish();

  function finish() {
    if (problems.length) {
      console.error('check-channel-ids: FAILED');
      problems.forEach((p) => console.error('  - ' + p));
      process.exit(1);
    }
    console.log('check-channel-ids: OK — badge, ⚠ tab, side-by-side ids, missing cells, filters, translations save/clear, legacy absence');
    process.exit(0);
  }
})();
