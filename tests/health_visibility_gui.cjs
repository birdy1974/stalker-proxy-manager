/* node tests/health_visibility_gui.cjs — Dashboard/Playlist alerts vs always-visible Settings. */
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require('jsdom');
const dom=new JSDOM(`<body>
  <section id="dp" data-health-problems-only hidden><div id="dp-host"></div></section>
  <section id="de" data-health-problems-only hidden><div id="de-host"></div></section>
  <section id="sp"><div id="sp-host"></div></section>
  <section id="se"><div id="se-host"></div></section>
</body>`,{runScripts:'outside-only',url:'http://test/dashboard'}),w=dom.window;
const app=fs.readFileSync('app/static/js/app.js','utf8');
w.eval(app.slice(app.indexOf('const $ ='),app.indexOf('/* ----------------------------------------------------- time formatting'))+'\nwindow.el=el;');
w.fmtTime=x=>x;w.mBtn=(text,cls,fn)=>w.el('button',{class:cls,onclick:fn},text);
const timers=new Map();let nextTimer=0;
w.setInterval=(fn,ms)=>{assert.equal(ms,30000);timers.set(++nextTimer,fn);return nextTimer;};
w.clearInterval=id=>timers.delete(id);
const node=id=>w.document.getElementById(id),tick=()=>new Promise(r=>setImmediate(r));
const emptyPlaylist=()=>({counts:Object.fromEntries(['live','vod','series','local'].map(k=>[k,{checked:0,unavailable:0,warning:0,unverified:0,healthy:0}])),checked_at:'2026-09-19',total:0,per_page:25,items:[]});
const emptyEpg=()=>({channels_checked:0,counts:{missing:0,gap_now:0,stale:0},channels:[],sources:[]});
let playlist=emptyPlaylist(),epg=emptyEpg(),failPlaylist=false,failEpg=false;
w.api=async url=>{
  if(url.startsWith('/api/playlist/health')){if(failPlaylist)throw Error('Playlist offline');return structuredClone(playlist);}
  if(url==='/api/epg/health'){if(failEpg)throw Error('EPG offline');return structuredClone(epg);}
  throw Error(url);
};
w.eval(fs.readFileSync('app/static/js/playlist-health.js','utf8'));
w.eval(fs.readFileSync('app/static/js/epg-controls.js','utf8'));
(async()=>{
  for (const view of ['dashboard','playlist']) {
  const page=fs.readFileSync(`app/templates/${view}.html`,'utf8');
  const markup=new JSDOM(page.split('{% block content %}')[1].split('{% endblock %}')[0]);
  const playlistId=view==='dashboard'?'dashboard-playlist-health':'playlist-health';
  const epgId=view==='dashboard'?'dashboard-epg-health':'playlist-epg-health';
  for (const id of [playlistId,epgId]) {
    const card=markup.window.document.getElementById(id)?.closest('[data-health-problems-only]');
    assert(card?.hidden, `${view}: ${id} must be alert-only and hidden on first render`);
  }
  assert(page.includes(`EpgControls.health($("#${epgId}"), true)`));
  markup.window.close();
  playlist=emptyPlaylist();epg=emptyEpg();
  const dashboard=w.PlaylistHealth.mount(node('dp-host'),{compact:view==='dashboard'});
  const settings=w.PlaylistHealth.mount(node('sp-host'));
  await w.EpgControls.health(node('de-host'),true);await w.EpgControls.health(node('se-host'));await tick();
  assert(node('dp').hidden && node('de').hidden,'no loading/empty card on Dashboard');
  assert(!node('sp').hidden && !node('se').hidden,'empty health stays visible in Settings');
  playlist.counts.live={checked:3,unavailable:0,warning:0,unverified:2,healthy:1};
  await dashboard.refresh();await settings.refresh();assert(node('dp').hidden,'unverified alone is not an alert');
  for(const kind of ['live','vod','series','local']) {
    for(const state of ['unavailable','warning']) {
      playlist.counts[kind][state]=1;
      // Invoke the periodic refresh: hidden cards must still discover problems.
      for(const fn of timers.values())fn();await tick();await tick();
      assert(!node('dp').hidden,`${kind} ${state} must show Dashboard health`);
      assert(!node('sp').hidden);assert(node('de').hidden,'a playlist alert does not show a healthy EPG panel');
      playlist.counts[kind][state]=0;await dashboard.refresh();assert(node('dp').hidden,'resolved issue hides entire card');
    }
  }
  epg.channels=[{name:'Channel',flags:['missing']}];epg.counts.missing=1;
  await w.EpgControls.health(node('de-host'),true);assert(!node('de').hidden);
  epg=emptyEpg();epg.sources=[{name:'Guide',flags:['timing_pending']}];
  await w.EpgControls.health(node('de-host'),true);assert(!node('de').hidden,'source-only alerts count');assert(node('dp').hidden,'an EPG alert does not show a healthy playlist panel');
  epg=emptyEpg();await w.EpgControls.health(node('de-host'),true);assert(node('de').hidden);
  failPlaylist=true;failEpg=true;
  await dashboard.refresh();await settings.refresh();await w.EpgControls.health(node('de-host'),true);await w.EpgControls.health(node('se-host'));
  assert(!node('dp').hidden && !node('de').hidden,'health-check errors must not look like a healthy Dashboard');
  assert(node('dp-host').textContent.includes('Could not check'));assert(node('de-host').textContent.includes('Could not check'));
  assert(!node('sp').hidden && !node('se').hidden);
  failPlaylist=false;failEpg=false;
  await dashboard.refresh();await settings.refresh();await w.EpgControls.health(node('de-host'),true);await w.EpgControls.health(node('se-host'));
  assert(node('dp').hidden && node('de').hidden);assert(!node('sp').hidden && !node('se').hidden);
  // A slow stale error must not re-show a card after a newer clean result.
  const realApi=w.api;let reject;
  w.api=()=>new Promise((_,r)=>{reject=r;});const stale=w.EpgControls.health(node('de-host'),true);
  w.api=realApi;await w.EpgControls.health(node('de-host'),true);reject(Error('old'));await stale;assert(node('de').hidden);
  w.dispatchEvent(new w.Event('pagehide'));assert.equal(timers.size,0);
  }
  const template=fs.readFileSync('app/templates/settings.html','utf8');
  assert(template.includes('id="settings-playlist-health"'));assert(template.includes('id="epg-health"'));
  assert(!template.includes('data-health-problems-only'));
  console.log('Health visibility: empty/healthy/unverified hidden on Dashboard and Playlist; all four types, EPG source alerts, errors, recovery, polling, stale responses and always-visible Settings passed.');
  dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
