/* Shared EPG matching controls: dialog edits are staged until the user saves. */
window.EpgMatching = (() => {
  async function choose(name, onSelect) {
    const initial = await api('/api/epg/suggest?name=' + encodeURIComponent(name));
    if (initial.items.length === 1 && initial.items[0].score >= .86) {
      onSelect(initial.items[0].tvg_id);
      toast('EPG ID selected — save the channel to apply', 'ok');
      return;
    }
    const body = el('div');
    const search = el('input', {class:'form-control form-control-sm', value:name, 'aria-label':'EPG channel search'});
    const list = el('div', {class:'mt-2', 'aria-live':'polite'});
    body.append(el('label', {class:'muted-label'}, 'Search EPG channel name'), search, list);
    body.append(el('span', {'data-help':'EPG matching'}, 'Fuzzy matches use the custom channel name. Identical IDs from mirror feeds are one choice. Channel numbers and plus variants are kept distinct. Selecting here changes only the editor; Save applies it.'));
    let revision = 0, timer, closed = false;
    const m = openModal({title:'Select the correct EPG channel', body, footer:el('div'), size:'lg', onClose:()=>{closed=true;clearTimeout(timer);}});
    const render = items => {
      list.replaceChildren();
      if (!items.length) list.append(el('div', {class:'small text-muted'}, 'No matching EPG channel. Try another name or refresh the EPG sources.'));
      for (const c of items) {
        const row = el('div', {class:'d-flex gap-2 align-items-center border-bottom py-2'});
        row.append(el('div', {class:'flex-grow-1', style:'min-width:0;overflow-wrap:anywhere', html:`<b>${esc(c.name)}</b> · ${Math.round(c.score*100)}%<div class="mono small">${esc(c.tvg_id)}</div><div class="small text-muted">${c.sources.map(esc).join(' · ')}</div>`}),
          mBtn('Use this ID', 'btn-outline-primary flex-shrink-0', ()=>{onSelect(c.tvg_id);m.close();}));
        list.append(row);
      }
    };
    render(initial.items);
    search.addEventListener('input', ()=>{
      clearTimeout(timer);const current=++revision;
      timer=setTimeout(async()=>{
        if (!search.value.trim()) {render([]);return;}
        list.textContent='Searching…';
        try {
          const result=await api('/api/epg/suggest?name='+encodeURIComponent(search.value));
          if (!closed && current===revision) render(result.items);
        } catch(e) {if (!closed && current===revision) list.textContent=e.message;}
      },250);
    });
    m.footer.append(mBtn('Cancel', 'btn-outline-secondary', m.close));
  }

  function review(report, onSaved) {
    if (!report.ambiguous.length) return;
    const body=el('div');
    body.append(el('div', {class:'small mb-2'}, `${report.matched} automatically matched · ${report.ambiguous.length} require selection · ${report.unmatched} without candidates`));
    body.append(el('span', {'data-help':'Review EPG matches'}, 'No ambiguous assignment is made automatically. Choose the correct ID for each channel or leave it unchanged. Previously assigned IDs require explicit approval to replace. Cached guide data is reprocessed after saving.'));
    const selections=[];
    for (const row of report.ambiguous) {
      const select=el('select', {class:'form-select form-select-sm mt-1', 'aria-label':`EPG for ${row.name}`});
      select.append(el('option', {value:''}, 'Leave unchanged / decide later'));
      for(const c of row.candidates) select.append(el('option', {value:c.tvg_id}, `${c.name} — ${c.tvg_id} (${Math.round(c.score*100)}%)`));
      const card=el('div', {class:'border-bottom py-2', style:'min-width:0;overflow-wrap:anywhere'},
        el('b',{},row.name), el('div',{class:'small text-muted'}, `Current: ${row.epg_id || 'not assigned'}`),select);
      body.append(card);selections.push({row,select});
    }
    const m=openModal({title:'Review EPG channel matches',body,footer:el('div'),size:'lg'});
    const apply=mBtn('Save selected matches', 'btn-accent', async()=>{
      const items=selections.filter(x=>x.select.value).map(({row,select})=>({id:row.id,epg_id:select.value,previous:row.epg_id}));
      if (!items.length) return;
      apply.disabled=true;
      try {
        const result=await api('/api/epg/match/assign',{method:'POST',body:{items}});
        toast(`${result.matched} EPG assignments saved`, 'ok');m.close();onSaved?.();
      } catch(e) {toast(e.message,'error');apply.disabled=false;}
    });
    apply.disabled=true;
    selections.forEach(({select})=>select.addEventListener('change',()=>{apply.disabled=!selections.some(x=>x.select.value);}));
    m.footer.append(mBtn('Close', 'btn-outline-secondary', m.close),apply);
  }
  return {choose,review};
})();
