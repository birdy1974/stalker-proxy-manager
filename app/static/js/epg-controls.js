/* Guide policy editors. Channel changes stay staged until the channel is saved. */
window.EpgControls = (() => {
  const help = (title, text) => el('span', {'data-help':title}, text);
  const integer = (value, min, max) => Number.isInteger(Number(value)) && Number(value)>=min && Number(value)<=max;
  const label = (text, field) => el('label', {class:'d-block muted-label mt-2'}, text, field);
  const healthRevisions = new WeakMap();
  const names = {missing:'Missing guide', gap_now:'No programme now', stale:'Stale guide', never_fetched:'Never fetched', refresh_failed:'Refresh failed', timing_pending:'Timezone change awaiting guide reprocessing'};

  async function editChannel(id, staged, defaultId, onApply) {
    const [overview, saved] = await Promise.all([api('/api/epg'), staged ? Promise.resolve({policy:staged}) :
      (id ? api(`/api/epg/policy/${id}`) : Promise.resolve({policy:{gap_fill:true,offset_minutes:0,mappings:[]}}))]);
    const policy = JSON.parse(JSON.stringify(saved.policy));
    const body=el('div'), list=el('div', {class:'mt-2'});
    const fill=el('input', {type:'checkbox',class:'form-check-input me-2','aria-label':'Fill gaps from fallback guides'});
    fill.checked=policy.gap_fill;
    const offset=el('input', {type:'number',min:-1440,max:1440,step:1,class:'form-control form-control-sm',value:policy.offset_minutes,'aria-label':'Channel guide offset in minutes'});
    body.append(el('label',{class:'form-check-label'},fill,'Fill gaps from fallback guides'),
      label('Channel correction (minutes)',offset),
      help('Guide priority and time corrections','Sources are tried top to bottom. With gap filling enabled, fallback entries fill only uncovered intervals and may be clipped at the primary programme boundaries. Without it, only the first enabled source is used. Positive offsets move programmes later; negative offsets move them earlier. Source, channel and mapping offsets are added. No sources means automatic selection by source ID for the channel’s EPG ID. Custom policies use a stable per-channel output EPG ID so aliases can have different schedules.'),
      el('h6',{class:'mt-3'},'Guide source priority'),list);
    if (saved.explicit_sources && !policy.mappings.length) body.append(el('div',{class:'alert alert-warning small'},'The configured guide sources were removed. Choose replacements, or apply an empty list to return to automatic selection.'));
    let closed=false;
    const m=openModal({title:'Channel EPG priority & timing',body,footer:el('div'),size:'lg',onClose:()=>{closed=true;}});
    const render=()=>{
      list.replaceChildren();
      if (!policy.mappings.length) list.append(el('div',{class:'small text-muted'},'Automatic: use enabled feeds carrying the channel’s EPG ID, in source ID order.'));
      policy.mappings.forEach((mapping,index)=>{
        const card=el('div',{class:'border rounded p-2 mb-2'});
        const select=el('select',{class:'form-select form-select-sm','aria-label':`Guide source ${index+1}`});
        for(const source of overview.sources) select.append(el('option',{value:source.id},`${source.id}: ${source.name || source.url}${source.enabled?'':' [disabled]'}`));
        select.value=String(mapping.source_id);
        const datalistId='epg-candidates-'+Math.random().toString(36).slice(2);
        const guideId=el('input',{class:'form-control form-control-sm mono',value:mapping.tvg_id,maxlength:200,list:datalistId,'aria-label':`Guide ID ${index+1}`,placeholder:'Search channel name or type the exact guide ID'});
        const suggestions=el('datalist',{id:datalistId});
        let revision=0,timer;
        const loadChoices=async()=>{
          const current=++revision;
          try {
            const result=await api(`/api/epg/channels?source_id=${mapping.source_id}&per_page=200&q=${encodeURIComponent(guideId.value)}`);
            if (closed || current!==revision || !card.isConnected) return;
            suggestions.replaceChildren(...result.rows.map(r=>el('option',{value:r.tvg_id},r.name)));
          } catch (_) { /* manually entered IDs remain usable when a guide has no catalogue */ }
        };
        select.addEventListener('change',()=>{mapping.source_id=Number(select.value);loadChoices();});
        guideId.addEventListener('input',()=>{mapping.tvg_id=guideId.value;clearTimeout(timer);timer=setTimeout(loadChoices,200);});
        const correction=el('input',{type:'number',class:'form-control form-control-sm',min:-1440,max:1440,step:1,value:mapping.offset_minutes || 0,'aria-label':`Mapping offset ${index+1}`});
        correction.addEventListener('input',()=>{mapping.offset_minutes=Number(correction.value);});
        const actions=el('div',{class:'d-flex flex-wrap gap-2 align-items-center mb-1'},el('b',{class:'me-auto'},`${index+1}${index===0?' · Primary':' · Fallback'}`));
        const up=mBtn('Move up','btn-outline-secondary',()=>{[policy.mappings[index-1],policy.mappings[index]]=[policy.mappings[index],policy.mappings[index-1]];render();});up.disabled=index===0;
        const down=mBtn('Move down','btn-outline-secondary',()=>{[policy.mappings[index+1],policy.mappings[index]]=[policy.mappings[index],policy.mappings[index+1]];render();});down.disabled=index===policy.mappings.length-1;
        actions.append(up,down,mBtn('Remove','btn-outline-danger',()=>{policy.mappings.splice(index,1);render();}));
        card.append(actions,select,label('Guide channel ID',guideId),suggestions,label('This mapping’s extra correction (minutes)',correction));
        list.append(card);loadChoices();
      });
    };
    body.append(mBtn('Add guide source','btn-outline-primary',()=>{
      if (!overview.sources.length) return toast('Add or check an EPG source in Settings first','warn');
      if (policy.mappings.length>=32) return toast('A channel supports up to 32 guide mappings','warn');
      const next = overview.sources.find(s=>s.enabled && !policy.mappings.some(m=>m.source_id===s.id)) || overview.sources.find(s=>s.enabled) || overview.sources[0];
      policy.mappings.push({source_id:next.id,tvg_id:defaultId || '',offset_minutes:0});render();
    }));
    m.footer.append(mBtn('Cancel','btn-outline-secondary',m.close),mBtn('Apply to channel editor','btn-accent',()=>{
      if (!integer(offset.value,-1440,1440) || policy.mappings.some(r=>!r.source_id || !r.tvg_id.trim() || !integer(r.offset_minutes,-1440,1440))) return toast('Enter guide IDs and whole-minute corrections between −1440 and 1440','warn');
      const keys=policy.mappings.map(r=>`${r.source_id}|${r.tvg_id.trim()}`);
      if (new Set(keys).size!==keys.length) return toast('Each source and guide ID combination may appear only once','warn');
      onApply({gap_fill:fill.checked,offset_minutes:Number(offset.value),mappings:policy.mappings.map(r=>({...r,tvg_id:r.tvg_id.trim()}))});m.close();
    }));
    render();
  }

  function editSource(source,onSaved) {
    const mode=el('select',{class:'form-select form-select-sm','aria-label':'Source refresh schedule'},
      el('option',{value:'inherit'},'Inherit global interval'),el('option',{value:'manual'},'Manual refresh only'),el('option',{value:'custom'},'Custom interval'));
    mode.value=source.refresh_hours==null?'inherit':source.refresh_hours===0?'manual':'custom';
    const hours=el('input',{type:'number',min:1,max:168,step:1,value:source.refresh_hours || 24,class:'form-control form-control-sm','aria-label':'Source refresh interval in hours'});
    const stale=el('input',{type:'number',min:1,max:720,step:1,value:source.stale_hours ?? '',placeholder:'Automatic',class:'form-control form-control-sm','aria-label':'Stale guide threshold in hours'});
    const update=()=>{hours.disabled=mode.value!=='custom';};mode.addEventListener('change',update);update();
    const tzMode=el('select',{class:'form-select form-select-sm','aria-label':'Source timezone mode'},
      el('option',{value:'auto'},'Automatic — trust source timestamps'),
      el('option',{value:'missing'},'Use timezone when offset is missing'),
      el('option',{value:'override'},'Override supplied timezone'));
    tzMode.value=source.timezone_mode || 'auto';
    const zoneListId='epg-timezones-'+Math.random().toString(36).slice(2);
    const zone=el('input',{class:'form-control form-control-sm',list:zoneListId,maxlength:64,value:source.timezone_name || Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC','aria-label':'Source timezone',placeholder:'Europe/Amsterdam'});
    const zones=el('datalist',{id:zoneListId},['UTC','Europe/Amsterdam','Europe/London','America/New_York'].map(name=>el('option',{value:name})));
    const offset=el('input',{type:'number',min:-1440,max:1440,step:1,class:'form-control form-control-sm',value:source.offset_minutes || 0,'aria-label':'Source time correction in minutes'});
    const result=el('div',{class:'small mt-2',role:'status'});
    const exampleStart=el('input',{class:'form-control form-control-sm mono',maxlength:100,'aria-label':'Preview original start',placeholder:source.url?.startsWith('portal://')?'2026-09-19 20:00:00':'20260919200000 +0200'});
    const exampleStop=el('input',{class:'form-control form-control-sm mono',maxlength:100,'aria-label':'Preview original stop',placeholder:'Optional end timestamp'});
    let revision=0,closed=false;
    const changed=()=>{++revision;result.textContent='Settings changed — preview again to check the resulting time.';};
    const updateZone=()=>{zone.disabled=tzMode.value==='auto';};updateZone();
    tzMode.addEventListener('change',()=>{updateZone();changed();});
    [zone,offset,exampleStart,exampleStop].forEach(node=>node.addEventListener('input',changed));
    const timing=()=>({timezone_mode:tzMode.value,timezone_name:tzMode.value==='auto'?null:zone.value.trim(),offset_minutes:Number(offset.value)});
    const validTiming=()=>{
      if (!integer(offset.value,-1440,1440)) {toast('Source correction must be whole minutes from −1440 to 1440','warn');return false;}
      if (tzMode.value!=='auto' && !zone.value.trim()) {toast('Select or enter the source timezone','warn');return false;}
      return true;
    };
    const reset=mBtn('Reset timing to automatic / zero','btn-outline-secondary',()=>{tzMode.value='auto';offset.value='0';updateZone();changed();});
    const preview=mBtn('Preview timing','btn-outline-primary',async()=>{
      if (!validTiming()) return;
      const current=++revision;preview.disabled=true;result.textContent='Checking timing…';
      try {
        const d=await api(`/api/epg/sources/${source.id}/timing-preview`,{method:'POST',body:{...timing(),sample_start:exampleStart.value.trim() || null,sample_stop:exampleStop.value.trim() || null}});
        if (closed || current!==revision) return;
        if (!d.available) {result.textContent=d.message || 'No cached example; enter a timestamp or refresh the source.';return;}
        if (!exampleStart.value) exampleStart.value=d.original_start;
        if (!exampleStop.value) exampleStop.value=d.original_stop;
        const localZone=Intl.DateTimeFormat().resolvedOptions().timeZone || 'browser local time';
        result.replaceChildren(el('div',{},el('b',{},d.title || 'Example')),
          el('div',{},`Automatic (UTC): ${d.automatic_utc || 'not available'}`),
          el('div',{},`Selected interpretation (UTC): ${d.interpreted_utc}`),
          el('div',{},`With source correction (UTC): ${d.corrected_start}`),
          el('div',{},`Local (${localZone}): ${fmtTime(d.corrected_start)} → ${fmtTime(d.corrected_stop)}`),
          ...(d.warnings || []).map(text=>el('div',{class:'text-danger'},text)));
      } catch(e) {if(!closed && current===revision) result.textContent=e.message;}
      finally {if(!closed) preview.disabled=false;}
    });
    const body=el('div',{},el('div',{class:'small',style:'overflow-wrap:anywhere'},source.name || source.url),
      label('Refresh schedule',mode),
      el('div',{class:'row g-2'},el('div',{class:'col-sm-6'},label('Interval (hours)',hours)),el('div',{class:'col-sm-6'},label('Stale after (hours; blank = automatic)',stale))),
      help('Per-source guide schedule','Custom intervals are 1–168 hours. Manual mode disables scheduled downloads for this source, not its existing guide. Global interval 0 pauses all automatic refreshes. Automatic stale threshold is the greater of 48 hours or twice the effective interval. Failed imports retain the previous guide.'),
      el('h6',{class:'mt-3'},'Source timing'),
      help('Source timezone and correction','Timezone changes reprocess cached original guides; old portal caches need one fresh download. Until successful reprocessing, the last good guide remains available with a pending alert. Fixed corrections apply immediately and never compound across refreshes.'),
      label('Timezone mode',tzMode),
      el('div',{class:'row g-2'},el('div',{class:'col-sm-6'},label('Source timezone',zone),zones),el('div',{class:'col-sm-6'},label('Source correction (minutes)',offset))),
      el('div',{class:'mt-2'},reset),
      el('h6',{class:'mt-3'},'Timing preview'),
      help('Timing preview','This is an offline preview of unsaved settings. It loads an example from the cached original guide, or uses the timestamp you enter. XMLTV format is YYYYMMDDhhmmss followed by an optional +HHMM offset. Portal examples accept ISO timestamps or Unix seconds. Local time is your browser timezone, not the server timezone. Channel and mapping corrections are not included in this source-only preview.'),
      el('div',{class:'row g-2'},el('div',{class:'col-sm-6'},label('Original start',exampleStart)),el('div',{class:'col-sm-6'},label('Original stop',exampleStop))),
      el('div',{class:'mt-2'},preview),result,
      el('div',{class:'small text-muted mt-2'},`Last success: ${source.last_fetch ? fmtTime(source.last_fetch):'never'}`));
    // Explicit anchors keep each help icon beside its own control/section.
    const headings=body.querySelectorAll('h6');
    headings[0].id=zoneListId+'-timing';headings[1].id=zoneListId+'-preview';
    mode.id=zoneListId+'-schedule';tzMode.id=zoneListId+'-mode';zone.id=zoneListId+'-zone';offset.id=zoneListId+'-offset';
    const hints=body.querySelectorAll('[data-help]');
    [mode.id,headings[0].id,headings[1].id].forEach((target,i)=>hints[i].dataset.helpFor=target);
    for(const [target,title,text] of [
      [tzMode.id,'Timestamp interpretation','Automatic trusts explicit offsets. Without an offset, XMLTV defaults to UTC and portal times use the advertised portal timezone. Missing-offset mode changes only timestamps without offsets. Override replaces even an explicit offset with the selected named timezone. Unix timestamps always remain absolute.'],
      [zone.id,'Named source timezone','Choose an IANA timezone such as Europe/Amsterdam. Named zones follow winter/summer time automatically. Nonexistent spring-forward times are excluded; ambiguous autumn times use their first occurrence. Explicit source offsets avoid this ambiguity in Automatic or missing-offset mode.'],
      [offset.id,'Source correction','Whole minutes from −1440 to +1440, after timezone conversion. Positive moves programmes later; negative moves them earlier. Source + channel + mapping corrections are added. Stored UTC times are not shifted by fixed corrections.']
    ]) {const hint=help(title,text);hint.dataset.helpFor=target;body.append(hint);}
    if (source.timing_pending) body.prepend(el('div',{class:'alert alert-warning small'},'Timezone change awaiting guide reprocessing. The last successfully interpreted guide is still in use.'));
    const m=openModal({title:'EPG source schedule & timing',body,footer:el('div'),onClose:()=>{closed=true;++revision;}});
    api('/api/epg/timezones').then(d=>{if(!closed && d.timezones?.length) zones.replaceChildren(...d.timezones.map(name=>el('option',{value:name})));}).catch(()=>{});
    const save=mBtn('Save source settings','btn-accent',async()=>{
      if (mode.value==='custom' && !integer(hours.value,1,168)) return toast('Choose a whole-hour interval from 1 to 168','warn');
      if (stale.value!=='' && !integer(stale.value,1,720)) return toast('Stale threshold must be 1–720 hours or blank','warn');
      if (!validTiming()) return;
      save.disabled=true;
      try {
        const d=await api(`/api/epg/sources/${source.id}`,{method:'PATCH',body:{refresh_hours:mode.value==='inherit'?null:mode.value==='manual'?0:Number(hours.value),stale_hours:stale.value===''?null:Number(stale.value),...timing()}});
        m.close();await onSaved?.();toast(d.message || 'EPG source settings saved',d.timing_pending?'warn':'ok');
      } catch(e) {toast(e.message,'error');save.disabled=false;}
    });
    m.footer.append(mBtn('Cancel','btn-outline-secondary',m.close),save);
    preview.click();
  }

  async function health(host,compact=false) {
    const problemCard=host.closest('[data-health-problems-only]');
    const revision=(healthRevisions.get(host)||0)+1;
    healthRevisions.set(host,revision);
    try {
      const d=await api('/api/epg/health');
      if (!host.isConnected || healthRevisions.get(host)!==revision) return;
      const expanded=host.querySelector('details')?.open;
      const count=d.channels.length+d.sources.length;
      if(problemCard) problemCard.hidden=count===0;
      host.replaceChildren(el('div',{class:count?'small text-danger':'small text-success',role:'status'},
        `${d.channels_checked} channels checked · ${d.counts.missing} missing · ${d.counts.gap_now} without a programme now · ${d.counts.stale} stale · ${d.sources.length} source alerts`));
      if (compact) {if(count) host.append(el('a',{href:'/settings',class:'small'},'Review EPG alerts in Settings'));return;}
      const detail=el('details',{class:'mt-1'},el('summary',{class:'small'},'Guide alert details'));
      detail.open=Boolean(expanded);
      for (const c of d.channels) detail.append(el('div',{class:'small border-bottom py-1'},el('b',{},c.name),' — '+c.flags.map(f=>names[f]||f).join(', '), c.last_programme_end ? ' · last programme ends '+fmtTime(c.last_programme_end):''));
      for (const s of d.sources) detail.append(el('div',{class:'small border-bottom py-1',style:'overflow-wrap:anywhere'},el('b',{},s.name),' — '+s.flags.map(f=>names[f]||f).join(', '),s.flags.includes('refresh_failed') && s.error ? ' · '+s.error:''));
      if(count) host.append(detail);
    } catch(e) {if(host.isConnected && healthRevisions.get(host)===revision){
      if(problemCard) problemCard.hidden=false;
      host.textContent='Could not check EPG coverage: '+e.message;
    }}
  }
  return {editChannel,editSource,health};
})();
