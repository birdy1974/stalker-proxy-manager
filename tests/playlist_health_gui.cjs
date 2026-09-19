/* node tests/playlist_health_gui.cjs — no real media or network calls. */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<body><div id="health"></div></body>',{runScripts:'outside-only',url:'http://test/playlist'}),w=dom.window;
const app=fs.readFileSync('app/static/js/app.js','utf8');
w.eval(app.slice(app.indexOf('const $ ='),app.indexOf('/* ----------------------------------------------------- time formatting'))+'\nwindow.el=el;');
w.fmtTime=x=>x;w.mBtn=(text,cls,fn)=>w.el('button',{class:cls,onclick:fn},text);
const modals=[],calls=[],edited=[];let probeResolve,probeSignal,refreshCount=0,holdProbe=false;
w.openModal=({body,footer,onClose})=>{const root=w.el('div',{},body,footer);w.document.body.append(root);const m={root,close(){onClose?.();root.remove();}};modals.push(m);return m;};
const counts=Object.fromEntries(['live','vod','series','local'].map(k=>[k,{checked:10,unavailable:2,warning:1,unverified:7,healthy:0}]));
const item={id:501,kind:'live',name:'<img src=x onerror=alert(1)>',status:'warning',reasons:[],source_count:2,sources:[
  {kind:'live',id:100,name:'Primary',status:'failed',reasons:['Recent failure'],checked_at:'2026-09-19'},
  {kind:'live',id:101,name:'Fallback',status:'unverified',reasons:['Not tested']}
]};
w.api=async(url,opts={})=>{
  calls.push({url,opts});
  if(url.includes('/probe?')) {
    probeSignal=opts.signal;
    if(holdProbe)return new Promise(resolve=>{probeResolve=resolve;});
    return {probe:{video:{codec:'h264'}}};
  }
  refreshCount++;return {counts,checked_at:'2026-09-19',total:30,per_page:25,items:[item]};
};
w.eval(fs.readFileSync('app/static/js/playlist-health.js','utf8'));
const tick=()=>new Promise(r=>setImmediate(r));
const button=(root,text)=>[...root.querySelectorAll('button')].find(n=>n.textContent===text);
(async()=>{
  const host=w.document.querySelector('#health');const control=w.PlaylistHealth.mount(host,{onEdit:(...args)=>edited.push(args)});await tick();
  assert.equal(host.querySelectorAll('a').length,4);assert.equal(host.querySelectorAll('img').length,0);
  assert.equal(calls.filter(c=>c.url.includes('/probe?')).length,0,'refresh is passive');
  button(host,'Next').click();await tick();assert(calls.at(-1).url.includes('page=2'));
  const kind=host.querySelector('[aria-label="Playlist health type"]');kind.value='series';kind.dispatchEvent(new w.Event('change'));await tick();assert(calls.at(-1).url.includes('kind=series'));assert(calls.at(-1).url.includes('page=1'));
  button(host,'Source details').click();let m=modals.at(-1);assert.equal(m.root.querySelectorAll('img').length,0);
  assert(m.root.querySelector('[data-help]'),'help uses tooltips');
  const before=refreshCount;button(m.root,'Probe source').click();await tick();await tick();assert(refreshCount>before);assert(m.root.textContent.includes('Media detected'));
  assert(calls.some(c=>c.url.includes('scope=source&kind=live&id=100')));
  button(m.root,'Edit playlist item').click();await tick();assert.deepEqual(edited,[['live',501]]);
  button(host,'Source details').click();m=modals.at(-1);holdProbe=true;button(m.root,'Probe source').click();await tick();
  assert([...m.root.querySelectorAll('button')].filter(b=>b.textContent==='Probe source').every(b=>b.disabled),'serialize probes to avoid colliding account leases');
  button(m.root,'Close').click();assert(probeSignal.aborted,'closing cancels the probe');probeResolve({probe:{video:{}}});await tick();
  const status=host.querySelector('[aria-label="Playlist health status"]');status.value='unverified';status.dispatchEvent(new w.Event('change'));await tick();assert(calls.at(-1).url.includes('status=unverified'));
  control.stop();dom.window.close();console.log('Playlist health: passive summaries, filtering/paging, escaped content, explicit probes, cancellation and edit links passed.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
