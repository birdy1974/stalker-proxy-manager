/* Optional DOM regression: node tests/web_player_thumbnails_gui.cjs (requires jsdom). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');

const html = fs.readFileSync('app/templates/player.html', 'utf8');
const items = [
  ...Array.from({ length: 5 }, (_, i) => ({
    kind: 'live', id: i + 1, name: `Channel ${i + 1}`, group: 'News', number: i + 1,
    logo: i === 2 ? '' : `https://images.example/channel-${i + 1}.png`,
  })),
  { kind: 'vod', id: 11, name: 'Movie One', group: 'Movies', logo: 'https://images.example/movie.jpg' },
  {
    kind: 'episode', id: 21, name: 'Show S01E01', group: 'Series: Drama',
    logo: 'https://images.example/show.jpg', series_id: 7, series_name: 'Show',
    series_order: 1, season_id: 71, season_number: 1, season_name: 'Season One',
    episode_number: 1, episode_name: 'Pilot',
    series_season_key: 'series:7:season:1', series_season_name: 'Show - Season One',
  },
  {
    kind: 'episode', id: 22, name: 'Show S01E02', group: 'Series: Drama',
    logo: 'https://images.example/show.jpg', series_id: 7, series_name: 'Show',
    series_order: 1, season_id: 71, season_number: 1, season_name: 'Season One',
    episode_number: 2, episode_name: 'Second',
    series_season_key: 'series:7:season:1', series_season_name: 'Show - Season One',
  },
  {
    kind: 'episode', id: 23, name: 'Show S02E01', group: 'Series: Drama',
    logo: 'https://images.example/show.jpg', series_id: 7, series_name: 'Show',
    series_order: 1, season_id: 72, season_number: 2, season_name: 'Season Two',
    episode_number: 1, episode_name: 'Season Two Premiere',
    series_season_key: 'series:7:season:2', series_season_name: 'Show - Season Two',
  },
  { kind: 'local', id: 31, name: 'Local File', group: 'Family', logo: '' },
];
let createdStream = '';

const dom = new JSDOM(html, {
  url: 'http://localhost/player',
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  beforeParse(window) {
    window.fetch = async (path) => {
      const body = String(path).includes('/player/api/playlist')
        ? { user: 'demo', items }
        : String(path).includes('/player/api/templates')
          ? { items: [{ id: 7, name: 'Browser MPEG-TS', is_default: true }] }
          : {};
      return { ok: true, status: 200, json: async () => body };
    };
    window.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
    window.HTMLMediaElement.prototype.pause = function () {};
    window.HTMLMediaElement.prototype.load = function () {};
    window.Element.prototype.scrollIntoView = function () {};
    window.mpegts = {
      isSupported: () => true,
      Events: { ERROR: 'error', MEDIA_INFO: 'media-info', LOADING_COMPLETE: 'complete' },
      createPlayer: (config) => {
        createdStream = config.url;
        return {
          on() {}, attachMediaElement() {}, load() {}, pause() {}, unload() {}, detachMediaElement() {}, destroy() {},
        };
      },
    };
  },
});

const { window } = dom;
const { document } = window;
const tick = () => new Promise((resolve) => setTimeout(resolve, 30));
const tileNames = (selector) => [...document.querySelectorAll(`${selector} [data-thumb-key]`)]
  .map((tile) => tile.querySelector('.thumb-title').textContent);

(async () => {
  await tick();
  await tick();

  assert.equal(document.getElementById('player-panel').hidden, false);
  assert.equal(document.getElementById('channel').value, 'live:1');
  assert.deepEqual(tileNames('#previous-thumbnails'), ['Channel 5', 'Channel 4']);
  assert.deepEqual(tileNames('#next-thumbnails'), ['Channel 2', 'Channel 3']);
  assert.equal(document.querySelectorAll('#channel-thumbnails [data-thumb-key]').length, items.length);
  assert.equal(
    document.querySelector('#channel-thumbnails [data-thumb-key="live:1"]').classList.contains('is-selected'),
    true,
  );
  assert.equal(
    document.querySelector('#channel-thumbnails [data-thumb-key="live:3"] img'),
    null,
    'missing logo should leave the initials fallback',
  );
  assert.match(
    document.querySelector('#channel-thumbnails [data-thumb-key="live:3"] .thumb-fallback').textContent,
    /C3/,
  );

  const select = document.getElementById('channel');
  select.value = 'live:3';
  select.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick();
  assert.deepEqual(tileNames('#previous-thumbnails'), ['Channel 2', 'Channel 1']);
  assert.deepEqual(tileNames('#next-thumbnails'), ['Channel 4', 'Channel 5']);

  document.querySelector('#next-thumbnails [data-thumb-key="live:4"]').click();
  await tick();
  assert.equal(select.value, 'live:4');
  assert.match(createdStream, /\/player\/stream\/live\/4\.ts\?tpl=7$/);
  assert.equal(
    document.querySelector('#channel-thumbnails [data-thumb-key="live:4"]').classList.contains('is-playing'),
    true,
  );
  assert.equal(
    document.querySelector('#channel-thumbnails [data-thumb-key="live:4"] .thumb-play-label').textContent,
    'Now playing',
  );

  const search = document.getElementById('search-filter');
  search.value = 'movie';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  await tick();
  assert.deepEqual(tileNames('#channel-thumbnails'), ['Movie One']);
  assert.equal(document.getElementById('previous-thumbnails-empty').hidden, false);
  assert.equal(document.getElementById('next-thumbnails-empty').hidden, false);

  search.value = '';
  search.dispatchEvent(new window.Event('input', { bubbles: true }));
  const kindFilter = document.getElementById('kind-filter');
  kindFilter.value = 'episode';
  kindFilter.dispatchEvent(new window.Event('change', { bubbles: true }));
  await tick();

  assert.equal(document.getElementById('series-season-controls').hidden, false);
  assert.deepEqual(
    [...document.querySelectorAll('#channel-thumbnails [data-series-season-key]')]
      .map((tile) => tile.dataset.seriesSeasonKey),
    ['series:7:season:1', 'series:7:season:2'],
  );
  assert.deepEqual(
    [...document.querySelectorAll('#channel-thumbnails [data-series-season-key]')]
      .map((tile) => tile.querySelector('.thumb-title').textContent),
    ['Show - Season One', 'Show - Season Two'],
  );
  assert.equal(document.querySelectorAll('#channel-thumbnails [data-thumb-key]').length, 0);
  assert.equal(
    document.getElementById('browse-heading').textContent,
    'Series — Show–Season folders',
  );
  assert.deepEqual(
    [...select.querySelectorAll('optgroup')].map((group) => group.label),
    ['Show - Season One — 2', 'Show - Season Two — 1'],
  );

  document
    .querySelector('[data-series-season-key="series:7:season:1"]')
    .click();
  await tick();
  assert.equal(
    document.getElementById('browse-heading').textContent,
    'Show - Season One',
  );
  assert.equal(
    document.getElementById('series-season-filter').value,
    'series:7:season:1',
  );
  assert.equal(document.getElementById('series-season-back').hidden, false);
  assert.deepEqual(tileNames('#channel-thumbnails'), [
    'Show S01E01',
    'Show S01E02',
  ]);
  assert.equal(
    document.querySelectorAll('#channel-thumbnails [data-series-season-key]').length,
    0,
  );

  document
    .querySelector('#channel-thumbnails [data-thumb-key="episode:22"]')
    .click();
  await tick();
  assert.equal(select.value, 'episode:22');
  assert.match(createdStream, /\/player\/stream\/episode\/22\.ts\?tpl=7$/);
  assert.deepEqual(tileNames('#previous-thumbnails'), ['Show S01E01']);
  assert.deepEqual(tileNames('#next-thumbnails'), []);
  assert.equal(
    [...tileNames('#previous-thumbnails'), ...tileNames('#next-thumbnails')]
      .includes('Show S02E01'),
    false,
  );

  document.getElementById('series-season-back').click();
  await tick();
  assert.equal(document.getElementById('series-season-filter').value, '');
  assert.equal(
    document.getElementById('browse-heading').textContent,
    'Series — Show–Season folders',
  );
  assert.deepEqual(
    [...document.querySelectorAll('#channel-thumbnails [data-series-season-key]')]
      .map((tile) => tile.dataset.seriesSeasonKey),
    ['series:7:season:1', 'series:7:season:2'],
  );

  console.log('web player thumbnail DOM test: OK');
  window.close();
})().catch((error) => {
  console.error(error);
  window.close();
  process.exitCode = 1;
});
