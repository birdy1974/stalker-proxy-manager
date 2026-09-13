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
     updates the N/total counter).

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
function stubFetch(path) {
  if (typeof path !== 'string') path = String(path);
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
    window.fetch = (path) => Promise.resolve(stubFetch(path));
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

  const fatal = pageErrors.filter((e) => !/Could not load|css|script/i.test(e));
  if (fatal.length) fail('page errors: ' + fatal.slice(0, 3).join(' | '));

  if (problems.length) {
    console.error('check-genre-popup: FAILED');
    problems.forEach((p) => console.error('  - ' + p));
    process.exit(1);
  }
  console.log('check-genre-popup: OK — popup opens, 48 genres render in the genre-cols band, filter works');
  process.exit(0);
})();
