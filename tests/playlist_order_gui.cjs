/* Browser reorder payload: never manufacture page-local positions. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('app/templates/playlist.html', 'utf8');
const start = html.indexOf('function orderDnd(');
const end = html.indexOf('\nfunction builderBar(', start);
assert.ok(start >= 0 && end > start);
const calls = [];
const context = vm.createContext({api: async (...args) => { calls.push(args); return {ok:true,items:[{id:53,order:1}]}; }, toast: () => {}});
vm.runInContext(html.slice(start, end), context);
(async () => {
  for (const kind of ['live', 'vod', 'series', 'local']) {
    const result = await context.orderDnd(kind).onReorder([53, 51, 52]);
    assert.equal(result.items[0].id, 53, 'return confirmed positions to the shared table');
    const [url, options] = calls.at(-1);
    assert.equal(url, `/api/playlist/${kind}/order`);
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(JSON.stringify(options.body)), {ids:[53, 51, 52]});
    assert.ok(html.includes(`orderDnd("${kind}")`));
  }
  assert.equal(calls.length, 4);
  assert.ok(html.includes('isLocked: (r) => r.lock_number'));
  console.log('Playlist reorder GUI passed: all four tabs submit row IDs, preserve sequence and retain Live lock controls.');
})().catch(error => { console.error(error); process.exitCode=1; });
