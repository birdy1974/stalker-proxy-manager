/* Playlist diagnostics never open streams until an explicit source probe. */
window.PlaylistHealth = (() => {
  const labels={live:'Live',vod:'VOD',series:'Series',local:'Local'};
  const states={unavailable:'No usable source',warning:'Needs attention',unverified:'Unverified',healthy:'Available / verified',failed:'Recently failed',skipped:'Skipped by policy'};
  const colors={unavailable:'text-danger',failed:'text-danger',warning:'text-warning-emphasis',unverified:'text-secondary',healthy:'text-success',skipped:'text-secondary'};

  function details(item,refresh,onEdit) {
    const body=el('div'), footer=el('div'), pending=new Set();let closed=false,running=false;
    body.append(el('b',{},item.name),el('div',{class:`small ${colors[item.status]}`},states[item.status]),
      ...item.reasons.map(reason=>el('div',{class:'small mt-1'},reason)));
    if(item.episodes_checked!=null) body.append(el('div',{class:'small mt-1'},`${item.episodes_checked} episodes checked · ${item.episodes_unavailable} without a usable route`));
    body.append(el('span',{'data-help':'Source health and probing'},'These are read-only configuration and recent-result checks, not a guarantee of playback. Background refreshes never open media. Probe source explicitly opens one stored input for up to the probe timeout and respects busy MACs. A probe checks input media before FFmpeg, not your output template. Failed probes are retryable. Evidence expires after 15 minutes or a server restart.'));
    const buttons=[];
    for(const source of item.sources) {
      const card=el('div',{class:'border rounded p-2 mt-2'}), status=el('div',{class:'small',role:'status'});
      card.append(el('b',{},source.name),source.portal?el('span',{class:'small'},' · '+source.portal):null,
        el('div',{class:`small ${colors[source.status]}`},states[source.status]),
        ...source.reasons.map(reason=>el('div',{class:'small text-muted'},reason)));
      if(source.checked_at) card.append(el('div',{class:'small text-muted'},'Last evidence: '+fmtTime(source.checked_at)));
      const canProbe=source.id && !['unavailable','skipped'].includes(source.status);
      const probe=mBtn('Probe source','btn-outline-primary',async()=>{
        if(running || closed)return;
        running=true;buttons.forEach(b=>b.disabled=true);
        const controller=new AbortController();pending.add(controller);
        status.textContent='Probing input media…';
        try {
          const d=await api(`/api/playlist/probe?scope=source&kind=${encodeURIComponent(source.kind)}&id=${source.id}`,{signal:controller.signal});
          if(closed)return;
          const result=d.probe || {};
          status.className='small mt-1 '+(result.error?'text-danger':'text-success');
          status.textContent=result.error ? 'Probe failed: '+result.error : result.video || result.audio?.length ? 'Media detected. Health updated.' : 'No audio/video detected.';
          await refresh();
        } catch(e) {if(!closed && e.name!=='AbortError'){status.className='small mt-1 text-danger';status.textContent='Probe could not complete: '+e.message;}}
        finally {pending.delete(controller);running=false;if(!closed)buttons.forEach(b=>b.disabled=b.dataset.blocked==='1');}
      });
      probe.disabled=!canProbe;probe.dataset.blocked=canProbe?'0':'1';buttons.push(probe);
      card.append(el('div',{class:'mt-2'},probe),status);body.append(card);
    }
    if(!item.sources.length) body.append(el('div',{class:'small text-danger mt-2'},'No source candidates configured.'));
    if(item.source_count>item.sources.length) body.append(el('div',{class:'small text-muted mt-2'},`Showing ${item.sources.length} of ${item.source_count} source/episode candidates; summary covers all.`));
    const m=openModal({title:`${labels[item.kind]} source health`,body,footer,onClose:()=>{closed=true;pending.forEach(c=>c.abort());}});
    footer.append(mBtn('Close','btn-outline-secondary',m.close));
    if(onEdit) footer.append(mBtn('Edit playlist item','btn-accent',async()=>{await m.close();onEdit(item.kind,item.id);}));
  }

  function mount(host,{compact=false,onEdit}={}) {
    const problemCard=host.closest('[data-health-problems-only]');
    let page=1,revision=0,timer,debounce,closed=false,loading=false;
    const summary=el('div',{class:'row g-2'}),stamp=el('div',{class:'small text-muted mt-2',role:'status'});
    const kind=el('select',{class:'form-select form-select-sm',style:'max-width:160px','aria-label':'Playlist health type'},
      el('option',{value:''},'All types'),...Object.entries(labels).map(([value,label])=>el('option',{value},label)));
    kind.value=new URLSearchParams(location.search).get('health_kind') || '';
    const status=el('select',{class:'form-select form-select-sm',style:'max-width:190px','aria-label':'Playlist health status'},
      ...[['issues','Needs attention'],['all','All enabled items'],['unavailable','No usable source'],['warning','Warnings'],['unverified','Unverified'],['healthy','Available / verified']].map(([value,label])=>el('option',{value},label)));
    const search=el('input',{class:'form-control form-control-sm',style:'max-width:240px',placeholder:'Name or group…','aria-label':'Search playlist health'});
    const list=el('div',{style:'max-height:420px;overflow-y:auto'}),pager=el('div',{class:'d-flex flex-wrap gap-2 align-items-center mt-2'});
    const detail=el('details',{class:'mt-2'},el('summary',{class:'small'},'Playlist source health details'));
    detail.open=Boolean(new URLSearchParams(location.search).get('health_kind'));
    const reload=mBtn('Refresh health','btn-outline-secondary',()=>load());
    host.replaceChildren(summary,stamp);
    if(!compact) {detail.append(el('div',{class:'d-flex flex-wrap gap-2 mt-2 mb-2'},kind,status,search,reload),list,pager);host.append(detail);}
    async function load() {
      const current=++revision;loading=true;reload.disabled=true;
      try {
        const d=await api(`/api/playlist/health?kind=${encodeURIComponent(kind.value)}&status=${status.value}&q=${encodeURIComponent(search.value)}&page=${page}&per_page=${compact?1:25}`);
        if(closed || current!==revision || !host.isConnected)return;
        // Unverified inputs are not confirmed failures. Keep polling even
        // while the Dashboard card is hidden so new issues appear automatically.
        if(problemCard) problemCard.hidden=!Object.values(d.counts).some(c=>c.unavailable>0 || c.warning>0);
        summary.replaceChildren(...Object.entries(labels).map(([key,label])=>{
          const c=d.counts[key];
          return el('div',{class:'col-12 col-sm-6 col-xl-3'},el('div',{class:'border rounded p-2 h-100'},
            el('a',{href:`/playlist?tab=${key}&health_kind=${key}`,class:'fw-semibold'},label),
            el('div',{class:'small'},`${c.checked} enabled items checked`),
            el('div',{class:`small ${c.unavailable?'text-danger':'text-muted'}`},`${c.unavailable} without a usable source`),
            el('div',{class:'small text-muted'},`${c.warning} warnings · ${c.unverified} unverified · ${c.healthy} available / verified`)));
        }));
        stamp.textContent='Checked '+fmtTime(d.checked_at)+' · passive checks';
        if(compact)return;
        list.replaceChildren();
        for(const item of d.items) {
          const row=el('div',{class:'d-flex flex-wrap gap-2 align-items-center border-bottom py-2'});
          row.append(el('div',{class:'flex-grow-1',style:'min-width:0;overflow-wrap:anywhere'},el('b',{class:'small'},item.name),
            el('div',{class:`small ${colors[item.status]}`},`${labels[item.kind]} · ${states[item.status]}`),
            el('div',{class:'small text-muted'},item.reasons.join(' · ') || [...new Set(item.sources.flatMap(s=>s.reasons))].slice(0,3).join(' · '))),
            mBtn('Source details','btn-outline-secondary',()=>details(item,load,onEdit)));
          list.append(row);
        }
        if(!d.items.length)list.append(el('div',{class:'small text-muted py-2'},'No matching enabled playlist items.'));
        const pages=Math.max(1,Math.ceil(d.total/d.per_page));
        if(page>pages){page=pages;return load();}
        const previous=mBtn('Previous','btn-outline-secondary',()=>{page--;load();});previous.disabled=page<=1;
        const next=mBtn('Next','btn-outline-secondary',()=>{page++;load();});next.disabled=page>=pages;
        pager.replaceChildren(previous,el('span',{class:'small'},`${d.total} items · page ${page} / ${pages}`),next);
      } catch(e) {if(!closed && current===revision && host.isConnected){
        if(problemCard) problemCard.hidden=false;
        stamp.textContent='Could not check playlist health: '+e.message;
      }}
      finally {if(current===revision){loading=false;reload.disabled=false;}}
    }
    const filter=()=>{page=1;load();};kind.addEventListener('change',filter);status.addEventListener('change',filter);
    search.addEventListener('input',()=>{++revision;clearTimeout(debounce);debounce=setTimeout(filter,250);});
    load();timer=setInterval(()=>{if(!loading)load();},30000);
    const stop=()=>{closed=true;++revision;clearInterval(timer);clearTimeout(debounce);};
    window.addEventListener('pagehide',stop,{once:true});
    return {refresh:load,stop};
  }
  return {mount};
})();
