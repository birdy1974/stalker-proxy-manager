// Optional DOM regression: node tests/catalog_review_gui.cjs (requires jsdom).
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');

const dom = new JSDOM('<body></body>', {runScripts: 'outside-only'});
const w = dom.window;
w.$ = (q, root = w.document) => root.querySelector(q);
w.$$ = (q, root = w.document) => [...root.querySelectorAll(q)];
w.el = (tag, attrs = {}, ...kids) => {
  const node = w.document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  });
  kids.flat().forEach(kid => { if (kid != null) node.append(kid); });
  return node;
};
w.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
w.openModal = ({body, footer}) => {
  const root = w.el('div');
  root.append(body, footer);
  w.document.body.append(root);
  return {close: () => root.remove()};
};
w.mBtn = (text, cls, fn) => {
  const button = w.el('button', {class: cls});
  button.textContent = text;
  button.onclick = fn;
  return button;
};
w.fmtTime = value => String(value || '');
w.pollJobs = () => {};
w.waitJob = () => {};
const toasts = [];
w.toast = message => toasts.push(message);
w.table = {
  items: [{
    id: 7,
    name: 'Portal Seven',
    catalog_last_success: '2026-10-30T08:00:00Z',
    catalog_last_error: '',
    catalog_changes: {live: {new: 0, changed: 1}, vod: {new: 0, changed: 0}, series: {new: 0, changed: 0}},
  }],
  reload: async () => {},
};
const requests = [];
const item = {
  id: 99,
  kind: 'live',
  source_id: 12,
  portal_item_id: '101',
  name: 'News HD',
  genre: 'News',
  change_type: 'changed',
  changes: {
    Name: {before: 'News Old', after: 'News HD'},
    'Playback link': {before: 'Portal link', after: 'Updated portal link'},
  },
  enabled: true,
  in_playlist: true,
  updated_at: '2026-10-30T08:01:00Z',
  playlist_update: {
    state: 'primary',
    update_count: 2,
    source_only: ['Playback link'],
    fallback_targets: [],
    targets: [{
      playlist_id: 44,
      name: 'News Old',
      role: 'primary',
      priority: 1,
      kept: [],
      season_links: [],
      updates: [
        {field: 'custom_name', label: 'Playlist name', before: 'News Old', after: 'News HD'},
        {field: 'group_name', label: 'Playlist group', before: 'Old News', after: 'News'},
      ],
    }],
  },
};
w.api = async (url, options = {}) => {
  requests.push({url, options});
  if (url.includes('/catalog-changes/review')) {
    return {ok: true, updated: 1, playlist_items_updated: 1, playlist_fields_updated: 2, season_links_added: 0};
  }
  if (url.includes('/catalog-changes?')) return {total: 1, page: 1, per_page: 50, items: [item]};
  throw new Error(`unexpected API call ${url}`);
};

const template = fs.readFileSync('app/templates/portals.html', 'utf8');
w.eval(template.slice(
  template.indexOf('async function reviewCatalog(pid)'),
  template.indexOf('/* ============================= add / edit modal')));

(async () => {
  await w.reviewCatalog(7);
  const text = w.document.body.textContent;
  assert.match(text, /Playlist update if reviewed/);
  assert.match(text, /Playlist #44 News Old/);
  assert.match(text, /Playlist name: News Old .* News HD/);
  assert.match(text, /Playlist group: Old News .* News/);
  assert.match(text, /Source-only, already refreshed \(no Playlist field\): Playback link/);
  assert.match(text, /Playlist will update/);

  const checkbox = w.document.querySelector('[data-change-id="99"]');
  checkbox.checked = true;
  checkbox.dispatchEvent(new w.Event('change', {bubbles: true}));
  assert.match(w.document.querySelector('#catalog-review-selected').textContent,
               /1 selected · 1 Playlist item\(s\), 2 update\(s\)/);

  const detailsButton = w.document.querySelector('#catalog-review-details-toggle');
  assert.ok(detailsButton, 'details toggle is present');
  assert.equal(detailsButton.textContent.trim(), 'Hide details');
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'true');
  assert.equal(w.document.querySelector('.catalog-review-details').hidden, false);
  assert.equal(w.document.querySelector('.catalog-review-summary').hidden, true);

  detailsButton.click();
  assert.equal(detailsButton.textContent.trim(), 'Show details');
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'false');
  assert.equal(w.document.querySelector('.catalog-review-details').hidden, true);
  const summary = w.document.querySelector('.catalog-review-summary');
  assert.equal(summary.hidden, false);
  assert.match(summary.textContent, /2 source difference\(s\) · Playlist will update \(2\)/);
  assert.equal(w.document.querySelector('#catalog-review-source-heading').textContent, 'Source');
  assert.equal(checkbox.checked, true, 'hiding details keeps the row selected');

  detailsButton.click();
  assert.equal(detailsButton.textContent.trim(), 'Hide details');
  assert.equal(detailsButton.getAttribute('aria-expanded'), 'true');
  assert.equal(w.document.querySelector('.catalog-review-details').hidden, false);
  assert.equal(w.document.querySelector('.catalog-review-summary').hidden, true);
  assert.equal(w.document.querySelector('#catalog-review-source-heading').textContent, 'Source and differences');
  assert.equal(checkbox.checked, true, 'showing details keeps the row selected');

  const button = [...w.document.querySelectorAll('button')]
    .find(node => node.textContent === 'Update Playlist & mark reviewed');
  assert.ok(button, 'review button is present');
  await button.onclick();
  const review = requests.find(r => r.url.includes('/catalog-changes/review'));
  assert.equal(review.options.method, 'POST');
  assert.equal(review.options.body.action, 'reviewed');
  assert.deepEqual(review.options.body.ids, [99]);
  assert.ok(toasts.some(message => /1 Playlist item\(s\) updated \(2 field\(s\)\)/.test(message)));

  console.log('Catalog Review DOM checks passed: exact Playlist preview, details show/hide, selection summary and reviewed action.');
  dom.window.close();
})().catch(error => {
  console.error(error);
  dom.window.close();
  process.exitCode = 1;
});
