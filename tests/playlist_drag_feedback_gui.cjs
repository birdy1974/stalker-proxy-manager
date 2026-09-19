/* Run with node tests/playlist_drag_feedback_gui.cjs (requires jsdom). */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<body></body>', {runScripts:'outside-only', pretendToBeVisual:true, url:'http://localhost/'});
const w = dom.window;
const shared = fs.readFileSync('app/static/js/app.js', 'utf8');
w.eval(shared.slice(0, shared.indexOf('/* ----------------------------------------------- detail-popup helpers')) + '\nwindow.DataTable = DataTable;');
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => {resolve=a; reject=b;}); return {promise,resolve,reject}; };
const ids = table => [...table.tbody.children].map(tr => Number(tr.dataset.id));
let requests = [], reads = 0, serverFails = false;
const rows = [{id:1,order:11,number:21,name:'One'}, {id:2,order:12,number:22,name:'Two'}, {id:3,order:13,number:90,name:'Locked',lock_number:true}];
const host = w.document.createElement('div'); w.document.body.append(host);
const table = new w.DataTable(host, {
  defaultSort:'order', selectable:true,
  server:async () => { reads++; if (serverFails) throw new Error('Refresh failed'); return {items:rows.map(r=>({...r})),total:3}; },
  columns:[{key:'name',label:'Name'}, {key:'order',label:'Ord'}, {key:'number',label:'Number'}],
  dnd:{isLocked:r=>r.lock_number, onReorder:order => {const d=deferred(); requests.push({order,...d}); return d.promise;}},
});
(async () => {
  await tick();
  table.selected.add(2);
  table.tableHost.scrollTop = 70;
  // Real drag events: moving down used to insert BEFORE the target, often
  // making adjacent downward drags appear to do nothing.
  table.tbody.children[0].dispatchEvent(new w.Event('dragstart', {bubbles:true,cancelable:true}));
  table.tbody.children[1].dispatchEvent(new w.Event('drop', {bubbles:true,cancelable:true}));
  assert.deepEqual(ids(table), [2,1,3], 'row moves immediately, before the response');
  assert.match(table.orderStatus.textContent, /Saving order/);
  assert.equal(table.orderStatus.getAttribute('role'), 'status');
  assert.equal(table.orderStatus.hidden, false);
  assert.ok(table.orderStatus.querySelector('.spinner-border'));
  assert.equal(table.tableHost.getAttribute('aria-busy'), 'true');
  assert.equal(table.table.inert, true);
  assert.equal(table.toolbar.inert, true);
  assert.equal(table.pager.inert, true);
  await table._saveOrder(2,1); // repeated drop cannot race the active save
  assert.equal(requests.length, 1);
  requests[0].resolve({ok:true,items:[{id:2,order:11,number:21},{id:1,order:12,number:22},{id:3,order:13,number:90}]});
  await tick();
  assert.equal(reads, 1, 'successful reorder must not refetch the enriched page');
  assert.equal(table.orderStatus.textContent, 'Order saved.');
  assert.equal(table.tableHost.getAttribute('aria-busy'), 'false');
  assert.equal(table.table.inert, false);
  assert.equal(table.items[0].order, 11);
  assert.equal(table.items[0].number, 21);
  assert.equal(table.items[2].number, 90, 'server-confirmed locked number retained');
  assert.equal(table.tableHost.scrollTop, 70);
  assert.equal(table.selected.has(2), true);
  assert.equal(table.tbody.children[2].draggable, false);
  assert.equal(table.tbody.children[0].draggable, true);

  const pending = table._saveOrder(1,2); // upward drag
  assert.deepEqual(ids(table), [1,2,3]);
  requests[1].reject(new Error('Save failed'));
  await pending;
  assert.deepEqual(ids(table), [2,1,3], 'failure restores previous display');
  assert.match(table.orderStatus.textContent, /Could not save order.*Previous display restored/);
  assert.equal(table.table.inert, false);
  await table._saveOrder(3,1); // locked row
  table.state.direction = 'desc'; await table._saveOrder(1,2);
  table.state.direction = 'asc'; table.state.sort = 'name'; await table._saveOrder(1,2);
  assert.equal(requests.length, 2);
  table.state.sort = 'order';

  // A queued filter/page refresh is not allowed to overwrite the pending move.
  const queued = table._saveOrder(1,2);
  await table.reload();
  assert.equal(reads, 1);
  requests[2].resolve({items:[{id:1,order:11},{id:2,order:12},{id:3,order:13}]});
  await queued;
  assert.equal(reads, 2);
  assert.equal(table.orderStatus.textContent, 'Order saved.');

  // Older servers get one compatibility refresh, not a false save failure.
  serverFails = true;
  const fallback = table._saveOrder(1,2);
  requests[3].resolve({ok:true}); await fallback;
  assert.equal(reads, 3);
  assert.match(table.orderStatus.textContent, /Order saved, but refresh failed/);
  assert.equal(table.table.inert, false);

  // Slow, obsolete GET responses must not replace newer table data.
  serverFails = false;
  const loads = [];
  table.opts.server = () => { const d=deferred(); loads.push(d); return d.promise; };
  const first = table.reload(), second = table.reload();
  loads[1].resolve({items:[{id:9,name:'Latest',order:1}],total:1}); await second;
  loads[0].resolve({items:rows,total:3}); await first;
  assert.deepEqual(ids(table), [9]);
  console.log('Drag feedback passed: immediate move, pending/success/error status, double-drop guard, authoritative numbers, no extra GET, selection/scroll, sort/lock guards and stale reloads.');
  dom.window.close();
})().catch(error => { console.error(error); dom.window.close(); process.exitCode=1; });
