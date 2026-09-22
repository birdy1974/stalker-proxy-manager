#!/usr/bin/env node
/*
Runtime-check the "Edit portal:" popup's genre lists (app/templates/portals.html):
open the REAL popup in jsdom with the REAL app.js, load a 48-genre list through
the stubbed API, and assert the multi-column band wiring:

  1. /portals renders and the old single-column scroll box is gone;
  2. the genre container has class `genre-cols` and no inline max-height;
  3. every rendered genre row carries the `genre-item-name` ellipsis span;
  4. app.css defines the band: grid, column flow, 15 rows/column, fixed
     column width, overflow-x auto + overflow-y hidden;
  5. the live filter still works on the multi-column list (hides non-matches,
     updates the N/total counter);
  6. clicking a genre's NAME opens the channels popup (lists the genre's
     items, does NOT flip the pane switch) and its own switch posts
     genres/toggle, mirrors onto the pane switch — while the pane's switch
     keeps posting that same endpoint on its own.

Usage: node check-genre-popup.js <rendered-portals.html> <app.js> <app.css>
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

const [PAGE, APPJS, APPCSS] = process.argv.slice(2);
if (!PAGE || !APPJS || !APPCSS) {
  console.error('usage: node check-genre-popup.js <page.html> <app.js> <app.css>');
  process.exit(1);
}
const pageHtml = fs.readFileSync(PAGE, 'utf8');
const appJs = fs.readFileSync(APPJS, 'utf8');
const css = fs.readFileSync(APPCSS, 'utf8');

const problems = [];
const fail = (p) => problems.push(p);
process.on('uncaughtException', (e) => {
  console.error('check-genre-popup: FAILED (uncaught)');
  console.error(e.stack || String(e));
  process.exit(1);
});

/* ---- 1. served page markup -------------------------------------------- */
for (const s of ['genre-cols', 'genre-item-name', 'genre-empty'])
  if (!pageHtml.includes(s)) fail(`served page is missing "${s}"`);
if (pageHtml.includes('max-height:260px')) fail('old single-column scroll box (max-height:260px) still in the page');

/* ---- 4. CSS band rule --------------------------------------------------- */
const rule = css.match(new RegExp('\\.genre-cols\\s*\\{[^}]*\\}'));
if (!rule) fail('app.css: .genre-cols rule missing');
else for (const prop of ['display: grid', 'grid-auto-flow: column',
                         'grid-template-rows: repeat(15, min-content)',
                         'grid-auto-columns: 300px',
                         'overflow-x: auto', 'overflow-y: hidden'])
  if (!rule[0].includes(prop)) fail(`app.css: .genre-cols missing "${prop}"`);
if (!new RegExp('\\.genre-cols\\s+\\.genre-item-name\\s*\\{[^}]*text-overflow:\\s*ellipsis').test(css))
  fail('app.css: .genre-cols .genre-item-name ellipsis rule missing');
const ROWS_PER_COL = parseInt((rule && rule[0].match(/repeat\((\d+),/))?.[1] || '0', 10);
const COL_W = parseInt((rule && rule[0].match(/grid-auto-columns:\s*(\d+)px/))?.[1] || '0', 10);

/* ---- canned API data ----------------------------------------------------- */
const LIVE = Array.from({ length: 48 }, (_, i) => ({
  id: i + 1, name: `Genre ${i % 12} ${i + 1}`, enabled: i % 3 === 0,
  genre_portal_id: String(i + 1), item_count: (i * 7) % 120 + 1, fetched: true,
}));

function stubResponse(data, status = 200) {
  return { status, ok: status < 400, statusText: 'OK',
           json: async () => data, text: async () => JSON.stringify(data) };
}
const CALLS = [];
function stubFetch(path, opts = {}) {
  if (typeof path !== 'string') path = String(path);
  const method = (opts && opts.method) || 'GET';
  /* the channels popup's endpoints live UNDER the /genres prefix, so these
     two matches must run before the generic branches below */
  if (path.includes('/genres/toggle')) {
    CALLS.push({ path, method, body: (opts && opts.body) || null });
    return stubResponse({ ok: true, count: 1 });
  }
  if (path.includes('/items?'))
    return stubResponse({ total: 3, page: 1, per_page: 100,
                          items: [
                            { id: 11, name: 'News 1', enabled: true, number: '1', channel_id: '101', poster: null },
                            { id: 12, name: 'News 2', enabled: true, number: '2', channel_id: '102', poster: null },
                            { id: 13, name: 'News 3', enabled: false, number: '3', channel_id: '103', poster: null },
                          ] });
  if (path.startsWith('/api/portals/1/genres'))
    return stubResponse({ live: LIVE, vod: [], series: [] });
  if (path.startsWith('/api/portals'))
    return stubResponse({ items: [{ id: 1, name: 'Demo', status: 'online', macs: [] }], jobs: [] });
  return stubResponse({ items: [], jobs: [] });
}

/* ---- jsdom: real page, real app.js, stubbed fetch ----------------------- */
const pageErrors = [];
const vc = new VirtualConsole();
vc.on('jsdomError', (e) => pageErrors.push(String(e.message || e)));
vc.on('error', (m) => pageErrors.push(String(m)));

/* splice the REAL app.js source into its <script src> tag, so every script
   runs as a real script (browser semantics: shared global scope) instead of
   as a strict-mode eval whose top-level consts would not leak. app.js
   contains no </script> literal (verified), so a plain splice is safe. */
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
(async () => {
  await sleep(300); // let makeTable's first /api/portals fetch resolve
  if (typeof window.editPortal !== 'function') { fail('editPortal is not a global function'); }
  else {
    try { window.editPortal(1); } catch (e) { fail(`editPortal(1) threw: ${e.stack}`); }
    await sleep(250); // let loadGenres' fetch round-trip finish
  }

  const host = document.querySelector('#genre-list-live');
  if (!host) fail('#genre-list-live not found in the opened popup');
  else {
    if (!host.classList.contains('genre-cols')) fail('genre container lost its genre-cols class');
    if (/max-height/i.test(host.getAttribute('style') || '')) fail('genre container still has inline max-height');
    const labels = [...host.querySelectorAll('label[data-name]')];
    if (labels.length !== LIVE.length)
      fail(`expected ${LIVE.length} genre rows, rendered ${labels.length}`);
    const noName = labels.filter((l) => !l.querySelector('.genre-item-name')).length;
    if (noName) fail(`${noName} genre rows missing the .genre-item-name span`);
    /* geometry the CSS implies: 48 rows / 15 per column -> 4 x 300px columns */
    if (ROWS_PER_COL === 0 || COL_W === 0) fail('could not parse grid constants from app.css');
    else {
      const cols = Math.ceil(LIVE.length / ROWS_PER_COL);
      const width = cols * COL_W + (cols - 1) * 16; /* 1rem gap */
      console.log(`  grid: ${LIVE.length} genres -> ${cols} columns of ${ROWS_PER_COL} rows, ` +
                  `band width ~${width}px (xl modal inner ~1050px -> horizontal scroll kicks in)`);
    }
    /* 5. live filter on the multi-column list */
    const inp = document.querySelector('#genre-q-live');
    if (!inp) fail('#genre-q-live filter box not found');
    else {
      inp.value = 'genre 1'; // matches "Genre 1 1", "Genre 1 13", ... i%12===1
      inp.dispatchEvent(new window.Event('input', { bubbles: true }));
      await sleep(50);
      const visible = [...host.querySelectorAll('label[data-name]')]
        .filter((l) => !l.classList.contains('d-none'));
      const expected = LIVE.filter((g) => g.name.toLowerCase().includes('genre 1')).length;
      if (visible.length !== expected)
        fail(`filter "genre 1": ${visible.length} visible, expected ${expected}`);
      const cnt = document.querySelector('#genre-count-live');
      if (cnt && cnt.textContent !== `${expected}/${LIVE.length}`)
        fail(`filter counter says "${cnt.textContent}", expected "${expected}/${LIVE.length}"`);
    }
  }

  /* ---- 6. genre name → channels popup + mirrored genre switch ---------- */
  const firstRow = document.querySelector('#genre-list-live label[data-name]');
  if (!firstRow) fail('no genre row to click in the live pane');
  else {
    const gid = LIVE[0].id;
    const paneSw = firstRow.querySelector('input[type=checkbox]');
    const wasChecked = paneSw ? !!paneSw.checked : false;
    const nameBtn = firstRow.querySelector('.genre-item-name');
    if (!nameBtn) fail('genre row lost its clickable .genre-item-name control');
    else {
      nameBtn.click();
      await sleep(80);
      const gch = document.querySelector('#gch-enabled');
      const pop = gch && gch.closest('.modal');
      if (!pop) fail('clicking the genre name did not open the channels popup');
      else {
        if (paneSw && paneSw.checked !== wasChecked)
          fail('clicking the name must NOT flip the pane switch');
        const names = [...pop.querySelectorAll('tbody tr')]
          .map(tr => tr.cells[1] && tr.cells[1].textContent.trim());
        if (names.length !== 3 || !names.includes('News 1'))
          fail(`popup should list the genre's 3 channels, got: ${JSON.stringify(names)}`);
        if (!/3 channels fetched/.test(pop.textContent))
          fail('popup should show "3 channels fetched"');
        /* the popup's OWN switch posts the SAME genres/toggle endpoint … */
        const want = !wasChecked;
        gch.checked = want;
        gch.dispatchEvent(new window.Event('change', { bubbles: true }));
        await sleep(80);
        const tog = CALLS.find(c => c.path.includes('/genres/toggle'));
        if (!tog) fail('popup switch did not POST genres/toggle');
        else {
          let b = null;
          try { b = typeof tog.body === 'string' ? JSON.parse(tog.body) : tog.body; } catch {}
          if (!b || b.kind !== 'live' || !Array.isArray(b.ids) || b.ids[0] !== gid
              || b.enabled !== want)
            fail(`toggle body should be {kind:"live", ids:[${gid}], enabled:${want}}, got ${tog.body}`);
        }
        /* …and mirrors itself onto the Edit popup's switch */
        if (paneSw && paneSw.checked !== want)
          fail('popup toggle must mirror onto the pane switch');
      }
    }
    /* the pane switch still works on its own (and posts the same endpoint) */
    if (paneSw) {
      const before = CALLS.filter(c => c.path.includes('/genres/toggle')).length;
      paneSw.checked = !paneSw.checked;
      paneSw.dispatchEvent(new window.Event('change', { bubbles: true }));
      await sleep(80);
      const after = CALLS.filter(c => c.path.includes('/genres/toggle')).length;
      if (after <= before) fail('the pane switch no longer posts genres/toggle');
    }
  }

  const fatal = pageErrors.filter((e) => !/Could not load|css|script/i.test(e));
  if (fatal.length) fail('page errors: ' + fatal.slice(0, 3).join(' | '));

  if (problems.length) {
    console.error('check-genre-popup: FAILED');
    problems.forEach((p) => console.error('  - ' + p));
    process.exit(1);
  }
  console.log('check-genre-popup: OK — popup opens, 48 genres render in the genre-cols band, filter works, name opens the channels popup, both switches toggle');
  process.exit(0);
})();
