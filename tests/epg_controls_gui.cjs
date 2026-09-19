/* node tests/epg_controls_gui.cjs — jsdom tests; no server or guide downloads. */
const assert=require('node:assert/strict'),fs=require('node:fs');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<body></body>',{runScripts:'outside-only'}),w=dom.window;
const app=fs.readFileSync('app/static/js/app.js','utf8');
w.eval(app.slice(app.indexOf('const $ ='),app.indexOf('/* ----------------------------------------------------- time formatting'))+'\nwindow.el=el;');
const modals=[],calls=[],toasts=[];
const sources=[{id:1,url:'https://disabled.test',enabled:false},{id:2,url:'https://primary.test',enabled:true,refresh_hours:null},{id:3,url:'https://fallback.test',enabled:true}];
let policy={gap_fill:true,offset_minutes:0,mappings:[]},applied;
w.toast=(...args)=>toasts.push(args);w.fmtTime=x=>x;
w.mBtn=(text,cls,fn)=>w.el('button',{class:cls,onclick:fn},text);
w.openModal=({body,footer,onClose})=>{
  const root=w.el('div',{},body,footer);w.document.body.append(root);
  const m={root,footer,close:()=>{onClose?.();root.remove();}};modals.push(m);return m;
};
w.api=async(url,opts)=>{
  calls.push({url,opts});
  if(url==='/api/epg')return {sources};
  if(url.startsWith('/api/epg/policy/'))return {policy};
  if(url.startsWith('/api/epg/channels'))return {rows:[{tvg_id:'main',name:'Primary'}]};
  if(url==='/api/epg/health')return {channels_checked:1,counts:{missing:1,gap_now:0,stale:0},channels:[{name:'<img src=x onerror=alert(1)>',flags:['missing']}],sources:[]};
  return {ok:true};
};
w.eval(fs.readFileSync('app/static/js/epg-controls.js','utf8'));
const tick=()=>new Promise(r=>setImmediate(r));
const button=(m,text)=>[...m.root.querySelectorAll('button')].find(b=>b.textContent===text);
const field=(m,label)=>m.root.querySelector(`[aria-label="${label}"]`);
const change=(node,value,type='change')=>{node.value=value;node.dispatchEvent(new w.Event(type));};
const plain=x=>JSON.parse(JSON.stringify(x));
(async()=>{
  await w.EpgControls.editChannel(5,null,'main',v=>{applied=v;});
  let m=modals.at(-1);
  button(m,'Add guide source').click();assert.equal(field(m,'Guide source 1').value,'2','prefer an enabled source');
  change(field(m,'Mapping offset 1'),'-30','input');
  button(m,'Add guide source').click();assert.equal(field(m,'Guide source 2').value,'3');
  change(field(m,'Guide ID 2'),'alias','input');
  change(field(m,'Channel guide offset in minutes'),'60','input');
  const up=[...m.root.querySelectorAll('button')].filter(b=>b.textContent==='Move up');
  assert(up[0].disabled);up[1].click();assert.equal(field(m,'Guide source 1').value,'3');
  button(m,'Apply to channel editor').click();
  assert.deepEqual(plain(applied),{gap_fill:true,offset_minutes:60,mappings:[{source_id:3,tvg_id:'alias',offset_minutes:0},{source_id:2,tvg_id:'main',offset_minutes:-30}]});
  assert(!calls.some(c=>c.opts),'guide editor stages data without persisting');
  await w.EpgControls.editChannel(5,applied,'main',()=>{throw Error('cancel must not apply');});
  m=modals.at(-1);button(m,'Remove').click();button(m,'Cancel').click();assert.equal(applied.mappings.length,2,'cancel leaves staged input unchanged');
  await w.EpgControls.editChannel(null,null,'main',v=>{applied=v;});m=modals.at(-1);
  button(m,'Add guide source').click();change(field(m,'Guide ID 1'),' ','input');button(m,'Apply to channel editor').click();assert(m.root.isConnected);
  change(field(m,'Guide ID 1'),'main','input');change(field(m,'Channel guide offset in minutes'),'1441','input');button(m,'Apply to channel editor').click();assert(m.root.isConnected);
  change(field(m,'Channel guide offset in minutes'),'0','input');button(m,'Add guide source').click();change(field(m,'Guide source 2'),'2');button(m,'Apply to channel editor').click();assert(m.root.isConnected);assert(toasts.at(-1)[0].includes('only once'));m.close();
  w.EpgControls.editSource(sources[1],()=>{});m=modals.at(-1);
  assert(field(m,'Source refresh interval in hours').disabled);
  change(field(m,'Source refresh schedule'),'custom');assert(!field(m,'Source refresh interval in hours').disabled);
  change(field(m,'Source refresh interval in hours'),'169');button(m,'Save source settings').click();assert(m.root.isConnected);
  change(field(m,'Source refresh interval in hours'),'6');change(field(m,'Stale guide threshold in hours'),'12');
  button(m,'Save source settings').click();await tick();assert(!m.root.isConnected);
  assert.deepEqual(plain(calls.at(-1).opts.body),{refresh_hours:6,stale_hours:12,timezone_mode:'auto',timezone_name:null,offset_minutes:0});
  w.EpgControls.editSource(sources[1],()=>{});m=modals.at(-1);change(field(m,'Source refresh schedule'),'manual');
  assert(field(m,'Source refresh interval in hours').disabled);button(m,'Save source settings').click();await tick();
  assert.deepEqual(plain(calls.at(-1).opts.body),{refresh_hours:0,stale_hours:null,timezone_mode:'auto',timezone_name:null,offset_minutes:0});
  // Source timing uses disabled automatic-zone fields, a read-only preview,
  // escaped programme metadata, and a reset that leaves schedules untouched.
  const originalApi=w.api;
  w.api=async(url,opts)=>{
    if(url.endsWith('/timing-preview')) {
      calls.push({url,opts});
      return {available:true,title:'<img src=x onerror=alert(1)>',original_start:'20260719120000 +0000',original_stop:'20260719130000 +0000',automatic_utc:'2026-07-19T12:00:00+00:00',interpreted_utc:'2026-07-19T10:00:00+00:00',corrected_start:'2026-07-19T10:30:00+00:00',corrected_stop:'2026-07-19T11:30:00+00:00',warnings:[]};
    }
    return originalApi(url,opts);
  };
  w.EpgControls.editSource(sources[1],()=>{});m=modals.at(-1);await tick();
  assert(field(m,'Source timezone').disabled);
  assert.equal(field(m,'Preview original start').value,'20260719120000 +0000');
  assert(m.root.textContent.includes('<img'));assert(!m.root.querySelector('img'));
  change(field(m,'Source timezone mode'),'override');assert(!field(m,'Source timezone').disabled);
  change(field(m,'Source timezone'),'Europe/Amsterdam','input');
  change(field(m,'Source time correction in minutes'),'30','input');
  button(m,'Preview timing').click();await tick();
  assert.deepEqual(plain(calls.at(-1).opts.body),{timezone_mode:'override',timezone_name:'Europe/Amsterdam',offset_minutes:30,sample_start:'20260719120000 +0000',sample_stop:'20260719130000 +0000'});
  button(m,'Save source settings').click();await tick();
  assert.deepEqual(plain(calls.at(-1).opts.body),{refresh_hours:null,stale_hours:null,timezone_mode:'override',timezone_name:'Europe/Amsterdam',offset_minutes:30});
  w.EpgControls.editSource({...sources[1],timezone_mode:'missing',timezone_name:'Europe/London',offset_minutes:90,refresh_hours:6},()=>{});m=modals.at(-1);await tick();
  button(m,'Reset timing to automatic / zero').click();
  assert.equal(field(m,'Source timezone mode').value,'auto');assert(field(m,'Source timezone').disabled);
  assert.equal(field(m,'Source time correction in minutes').value,'0');
  assert.equal(field(m,'Source refresh interval in hours').value,'6');
  button(m,'Cancel').click();
  let completePreview;
  w.api=async(url,opts)=>url.endsWith('/timing-preview')?new Promise(resolve=>{completePreview=resolve;}):originalApi(url,opts);
  w.EpgControls.editSource(sources[1],()=>{});m=modals.at(-1);
  change(field(m,'Source time correction in minutes'),'60','input');
  completePreview({available:true,title:'Stale preview must not appear',warnings:[]});await tick();
  assert(!m.root.textContent.includes('Stale preview must not appear'));assert(m.root.textContent.includes('Settings changed'));
  button(m,'Cancel').click();w.api=originalApi;
  const host=w.el('div');w.document.body.append(host);await w.EpgControls.health(host);
  assert(host.textContent.includes('1 missing'));assert(host.textContent.includes('<img'));assert(!host.querySelector('img'),'alert text must be escaped');
  dom.window.close();console.log('EPG controls GUI passed: staged/cancelled policies, priority order, source/offset validation, custom/manual schedules, disabled irrelevant fields and safe coverage alerts.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
