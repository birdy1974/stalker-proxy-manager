/* node tests/playlist_assignments_gui.cjs */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<body></body>',{runScripts:'outside-only',pretendToBeVisual:true,url:'http://test/playlist'}),w=dom.window;
const shared=fs.readFileSync('app/static/js/app.js','utf8');
w.eval(shared.slice(0,shared.indexOf('/* ----------------------------------------------- detail-popup helpers'))+'\nObject.assign(window,{DataTable,el,$,$$,esc,mBtn});');
const template=fs.readFileSync('app/templates/playlist.html','utf8');
w.eval(template.slice(template.indexOf('let TEMPLATES'),template.indexOf('/* ========================= details popup'))+'\nObject.assign(window,{PL_TABLES,groupCell,tplInlineCell,TEMPLATES,groupDialogHtml,tplOptions});');
w.eval(template.slice(template.indexOf('function bulkBar'),template.indexOf('/* toolbar shared'))+'\nwindow.bulkBar=bulkBar;');
w.TEMPLATES.push({id:7,name:'Copy'});
const tick=()=>new Promise(r=>setTimeout(r,0));
const defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const calls=[],modals=[];
w.api=(url,options)=>{const pending=defer();calls.push({url,options,...pending});return pending.promise;};
w.toast=()=>{};
w.openModal=({body,footer})=>{const root=w.el('div',{},body,footer);w.document.body.append(root);const m={root,close:()=>root.remove()};modals.push(m);return m;};
const button=(root,text)=>[...root.querySelectorAll('button')].find(b=>b.textContent.startsWith(text));
(async()=>{
 for(const kind of ['live','vod','series','local']) {
  let reads=0,reorders=0;
  const host=w.el('div');w.document.body.append(host);
  const table=new w.DataTable(host,{defaultSort:'order',selectable:true,
    server:async()=>{reads++;return {total:2,groups:['Old'],items:[{id:1,order:1,group_name:'Old',ffmpeg_template_id:null},{id:2,order:2,group_name:'Old',ffmpeg_template_id:null}]};},
    columns:[{key:'group_name',label:'Group',render:r=>w.groupCell(kind,r)},{key:'template',label:'Template',render:r=>w.tplInlineCell(kind,r)}],
    dnd:{onReorder:async()=>{reorders++;return {items:[]};}}
  });w.PL_TABLES[kind]=table;await tick();
  let input=host.querySelector('input[data-group-edit]');const tr=input.closest('tr');
  // First click/focus selects all; second click/drag remains native.
  input.dispatchEvent(new w.MouseEvent('pointerdown',{bubbles:true,clientX:10,clientY:10}));input.focus();
  input.setSelectionRange(2,2);input.dispatchEvent(new w.MouseEvent('click',{bubbles:true,clientX:10,clientY:10}));
  assert.equal(input.selectionStart,0);assert.equal(input.selectionEnd,3);assert.equal(tr.draggable,false);
  input.dispatchEvent(new w.MouseEvent('pointerdown',{bubbles:true,clientX:10,clientY:10}));input.setSelectionRange(1,2);
  input.dispatchEvent(new w.MouseEvent('click',{bubbles:true,clientX:40,clientY:10}));assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
  input.blur();input.setSelectionRange(1,1);
  input.dispatchEvent(new w.MouseEvent('pointerdown',{bubbles:true,clientX:10,clientY:10}));input.focus();
  assert.equal(input.selectionStart,1,'first pointer focus must not steal native drag selection');
  input.setSelectionRange(1,2);input.dispatchEvent(new w.MouseEvent('click',{bubbles:true,clientX:40,clientY:10}));
  assert.equal(input.selectionStart,1);assert.equal(input.selectionEnd,2);
  input.blur();input.focus();assert.equal(input.selectionStart,0);assert.equal(input.selectionEnd,3,'keyboard focus selects all');
  const drag=new w.Event('dragstart',{bubbles:true,cancelable:true});input.dispatchEvent(drag);assert(!table._dragRow);assert(!drag.defaultPrevented,'native text dragging is allowed');
  const textDrop=new w.Event('drop',{bubbles:true,cancelable:true});input.dispatchEvent(textDrop);assert(!textDrop.defaultPrevented);
  input.value=' New ';const saved=w.inlineGroup(kind,1,input.value,input);
  assert(input.readOnly);assert(input.getAttribute('aria-busy'));assert(input.closest('div').textContent.includes('Saving group'));
  await table._saveOrder(1,2);assert.equal(reorders,0,'no reorder while a field update is pending');
  assert.equal(calls.at(-1).options.body.group_name,'New');calls.at(-1).resolve({ok:true});await saved;
  assert.equal(reads,1,'no list reload after inline assignment');assert.equal(input.value,'New');assert.equal(table.items[0].group_name,'New');
  assert(!input.readOnly);assert.equal(host.querySelector('input[data-group-edit]'),input,'keep focus/DOM stable');
  assert([...input.list.options].some(o=>o.value==='New'));
  const select=host.querySelector('select');select.value='7';const pending=w.inlineTpl(kind,1,'7',select);
  assert(select.disabled);calls.at(-1).resolve({ok:true});await pending;assert.equal(table.items[0].ffmpeg_template_id,7);assert.equal(table.items[0].template,'Copy');assert.equal(reads,1);
  select.value='';const failed=w.inlineTpl(kind,1,'',select);calls.at(-1).reject(Error('offline'));await failed;
  assert.equal(select.value,'7');assert.equal(table.items[0].ffmpeg_template_id,7);assert(select.closest('div').textContent.includes('Save not confirmed'));
  // Queued list refresh waits until the pending assignment finishes.
  input.value='Next';const queued=w.inlineGroup(kind,1,'Next',input);await table.reload();assert.equal(reads,1);
  calls.at(-1).resolve({ok:true});await queued;await tick();assert.equal(reads,2);
  // Bulk dialog has immediate feedback, no double-submit, and remains retryable.
  table.selected=new Set([1,2]);const bar=w.bulkBar(kind,()=>table);host.append(bar);
  button(bar,'Assign group').click();let m=modals.at(-1);m.root.querySelector('#ba-val').value='Bulk';
  const apply=button(m.root,'Apply');apply.click();assert(apply.disabled);assert(button(m.root,'Cancel').disabled);assert(m.root.textContent.includes('Saving group for 2'));
  const callCount=calls.length;apply.click();assert.equal(calls.length,callCount);
  calls.at(-1).reject(Error('offline'));await tick();assert(!apply.disabled);assert(m.root.isConnected);assert(m.root.textContent.includes('Save not confirmed'));
  apply.click();calls.at(-1).resolve({ok:true,count:2});await tick();await tick();assert(!m.root.isConnected);
  assert(table.items.every(r=>r.group_name==='Bulk'));assert.equal(reads,2,'bulk metadata is applied without a list GET');
  button(bar,'Assign template').click();m=modals.at(-1);m.root.querySelector('#ba-val').value='7';
  button(m.root,'Apply').click();assert(m.root.textContent.includes('Saving template'));
  calls.at(-1).resolve({ok:true,count:2});await tick();await tick();
  assert(table.items.every(r=>r.ffmpeg_template_id===7 && r.template==='Copy'));assert.equal(reads,2);
  // Outside the editable field, row dragging is restored.
  const row=table.tbody.children[0];row.dispatchEvent(new w.MouseEvent('pointerdown',{bubbles:true}));assert(row.draggable);
  row.dispatchEvent(new w.Event('dragstart',{bubbles:true,cancelable:true}));assert.equal(table._dragRow.id,1);
  const field=table.tbody.children[1].querySelector('[data-group-edit]');const rowDrop=new w.Event('drop',{bubbles:true,cancelable:true});field.dispatchEvent(rowDrop);
  assert(rowDrop.defaultPrevented);assert.equal(reorders,0,'dropping on group controls must not reorder or insert a row ID');
  row.dispatchEvent(new w.Event('dragend',{bubbles:true}));host.remove();
 }
 console.log('Playlist assignments: all four kinds, immediate progress, no extra GET, cache/suggestions, failure rollback, retry/double-submit, deferred reload, select-on-focus and input-safe dragging passed.');
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
