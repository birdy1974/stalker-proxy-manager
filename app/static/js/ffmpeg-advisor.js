/* Optimisation advisor for the FFmpeg template editor.
 *
 * Advice only: it never changes a field by itself. The operator ticks the
 * suggestions they want, reviews the exact field changes in a dialog
 * ("Review changes"), and only the Apply button there writes them into the form.
 * The goal and the dismissed suggestions are saved per template with the
 * normal "Save template" button (advice_goal / advice_ignored).
 *
 * The page gives it: readFields(), writeFields(obj), mode() -> {sentinel,manual},
 * name(), onApplied() (re-render the command). Every rule lives on the server
 * (app/services/ffmpeg_advisor.py); this file only draws the answer.
 */
const FFmpegAdvisor = (() => {
  const SEV = {
    critical: {label: 'Critical', badge: 'text-bg-danger', icon: 'bi-x-octagon-fill', dot: '#dc3545', rank: 0},
    warn: {label: 'Warning', badge: 'text-bg-warning', icon: 'bi-exclamation-triangle-fill', dot: '#ffc107', rank: 1},
    tip: {label: 'Tip', badge: 'text-bg-info', icon: 'bi-lightbulb-fill', dot: '#0dcaf0', rank: 2},
  };
  const AXIS = {start: 'start / latency', speed: 'speed', load: 'CPU / GPU load',
    quality: 'picture quality', compat: 'compatibility', host: 'this host'};
  const COUNT_NAME = {critical: ['critical', 'critical'], warn: ['warning', 'warnings'], tip: ['tip', 'tips']};
  let host = null, goal = 'balanced', ignored = new Set(), report = null;
  let touched = new Map();            // advice id -> checked (only what the user changed)
  let timer = null, seq = 0, readOnly = false, goals = [];
  const $id = id => document.getElementById(id);

  function init(h) {
    host = h;
    $id('ff-adv-goal')?.addEventListener('change', e => { goal = e.target.value; refresh(0); });
    $id('ff-adv-review')?.addEventListener('click', review);
    $id('ff-adv-list')?.addEventListener('click', onListClick);
    $id('ff-adv-list')?.addEventListener('change', onListChange);
    $id('ff-adv-ignored')?.addEventListener('click', e => {
      const b = e.target.closest('[data-adv-restore]');
      if (!b) return;
      e.preventDefault();
      if (b.dataset.advRestore === '*') ignored.clear(); else ignored.delete(b.dataset.advRestore);
      draw();
    });
  }

  /** A template was selected (t = row) or a new one started (t = null). */
  function load(t) {
    goal = (t && t.advice_goal) || 'balanced';
    ignored = new Set(String((t && t.advice_ignored) || '').split(',').filter(Boolean));
    touched = new Map();
    report = null;
    refresh(0);
  }
  /** What the save request carries. */
  function state() { return {advice_goal: goal, advice_ignored: [...ignored].join(',')}; }

  function refresh(delay = 250) {
    clearTimeout(timer);
    const card = $id('ff-advisor');
    if (!card || !host) return;
    const m = host.mode();
    if (m.sentinel) { card.hidden = true; return; }
    readOnly = !!m.manual;
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const r = await api('/api/ffmpeg/advice', {method: 'POST', body: {
          options: host.readFields(), goal, name: host.name(), ignored: [...ignored].join(',')}});
        if (mine !== seq) return;       // a newer answer is on its way
        report = r; goals = r.goals || goals;
        draw();
      } catch { /* the api helper already toasted it */ }
    }, delay);
  }

  function visible() { return (report?.findings || []).filter(f => !ignored.has(f.id)); }
  function checked(f) {
    if (!f.fix || readOnly) return false;
    return touched.has(f.id) ? touched.get(f.id) : f.severity !== 'tip';
  }

  function draw() {
    const card = $id('ff-advisor');
    if (!card || !report) return;
    card.hidden = false;
    const sel = $id('ff-adv-goal');
    if (sel.options.length !== goals.length) {
      sel.replaceChildren(...goals.map(g => el('option', {value: g.id}, g.label)));
    }
    sel.value = goal;
    $id('ff-adv-goal-hint').textContent = goals.find(g => g.id === goal)?.hint || '';
    drawScores(report.scores);
    drawFacts();
    const items = visible();
    const counts = {critical: 0, warn: 0, tip: 0};
    items.forEach(f => counts[f.severity]++);
    $id('ff-adv-counts').innerHTML = items.length
      ? Object.entries(counts).filter(([, n]) => n).map(([s, n]) =>
          `<span class="badge ${SEV[s].badge}">${n} ${COUNT_NAME[s][n > 1 ? 1 : 0]}</span>`).join('')
      : '<span class="badge text-bg-success"><i class="bi bi-check2"></i> nothing to improve</span>';
    const list = $id('ff-adv-list');
    list.replaceChildren(...items.map(item));
    drawIgnored();
    drawBadges(items);
    updateReview(items);
    $id('ff-adv-note').textContent = readOnly
      ? 'Manual command: advice is read-only here. Switch to fields mode to apply changes.'
      : 'Nothing is changed until you review and apply. The goal and dismissed tips are saved with the template.';
  }

  function drawScores(s) {
    const rows = [['quality', 'Picture quality'], ['start', 'Start / zap speed'],
      ['efficiency', 'Light on the host']];
    $id('ff-adv-scores').innerHTML = rows.map(([k, label]) => {
      const v = s?.[k] ?? 0;
      const cls = v >= 70 ? 'bg-success' : v >= 45 ? 'bg-warning' : 'bg-danger';
      return `<div class="col-12 col-sm-4"><div class="d-flex justify-content-between small">
        <span>${label}</span><span class="text-muted">${v}</span></div>
        <div class="progress" style="height:6px" role="progressbar" aria-label="${label}"
          aria-valuenow="${v}" aria-valuemin="0" aria-valuemax="100"
          title="Rough estimate from the settings, not a benchmark">
          <div class="progress-bar ${cls}" style="width:${v}%"></div></div></div>`;
    }).join('') + `<div class="col-12 small text-muted">Bars are a rough estimate from these settings
      (higher is better); ${s?.basis === 'copy' ? 'Copy keeps the source as it is.' : 'use the demo for a real speed measurement.'}</div>`;
  }

  function drawFacts() {
    const e = report.environment || {}, bits = [];
    if (e.cpus) bits.push(`${e.cpus} CPU thread${e.cpus > 1 ? 's' : ''}`);
    const dev = (e.devices || []).find(d => d.exists);
    if (dev) bits.push(`GPU ${esc(dev.path.split('/').pop())}${dev.accessible ? '' : ' (no access)'}`);
    else if (e.devices) bits.push('no GPU render node');
    if (e.vaapi && e.vaapi.h264_low_power != null) bits.push(`low-power H.264 ${e.vaapi.h264_low_power ? 'available' : 'not available'}`);
    if (e.active_transcodes != null) bits.push(`${e.active_transcodes} transcode${e.active_transcodes === 1 ? '' : 's'} running now`);
    let html = bits.length ? `Host: ${bits.join(' · ')}` : '';
    const live = report.live;
    if (live) {
      const cls = live.slow ? 'text-danger' : 'text-success';
      html += `${html ? '<br>' : ''}<span class="${cls}">Last live run of this template: ${Number(live.speed).toFixed(2)}× real time${live.slow ? ' (too slow)' : ''}</span>`;
    }
    $id('ff-adv-facts').innerHTML = html;
  }

  function item(f) {
    const s = SEV[f.severity] || SEV.tip;
    const id = `ff-adv-chk-${f.id}`;
    const box = el('div', {class: `ff-adv-item ff-adv-${f.severity} border-start border-3 ps-2 py-1 mb-2`,
      'data-adv-id': f.id, tabindex: '-1', style: `border-color:${s.dot} !important`});
    const check = f.fix && !readOnly
      ? `<input class="form-check-input mt-1" type="checkbox" id="${id}" data-adv-check="${esc(f.id)}"
           ${checked(f) ? 'checked' : ''} aria-label="Include this change">`
      : '<span style="width:1em"></span>';
    box.innerHTML = `<div class="d-flex gap-2 align-items-start">
      ${check}
      <div class="flex-grow-1">
        <label ${f.fix && !readOnly ? `for="${id}"` : ''} class="mb-0">
          <span class="badge ${s.badge}"><i class="bi ${s.icon}"></i> ${s.label}</span>
          <span class="badge text-bg-light border">${esc(AXIS[f.axis] || f.axis)}</span>
          ${esc(f.message)}</label>
        ${f.why ? `<details class="small text-muted"><summary>Why</summary>${esc(f.why)}</details>` : ''}
        ${f.fix ? `<div class="small mt-1">Suggested change: <code>${esc(f.fix.summary)}</code></div>`
                : '<div class="small text-muted mt-1">No automatic change: this needs a decision or a change outside the template.</div>'}
      </div>
      <button type="button" class="btn btn-sm btn-link text-muted p-0 text-nowrap" data-adv-ignore="${esc(f.id)}"
        title="Hide this tip for this template (saved with the template)">Dismiss</button></div>`;
    return box;
  }

  function onListClick(e) {
    const b = e.target.closest('[data-adv-ignore]');
    if (!b) return;
    ignored.add(b.dataset.advIgnore);
    draw();
  }
  function onListChange(e) {
    const c = e.target.closest('[data-adv-check]');
    if (!c) return;
    touched.set(c.dataset.advCheck, c.checked);
    updateReview(visible());
  }

  function drawIgnored() {
    const n = [...ignored].filter(id => (report.findings || []).some(f => f.id === id)).length;
    $id('ff-adv-ignored').innerHTML = n
      ? `${n} dismissed tip${n > 1 ? 's' : ''} hidden. <a href="#" data-adv-restore="*" class="link-secondary">Show them again</a>`
      : '';
  }

  /* Coloured dot beside the label of every field a visible finding is about. */
  function drawBadges(items) {
    document.querySelectorAll('[data-adv-badge]').forEach(n => n.remove());
    const worst = new Map();
    for (const f of items) for (const field of f.fields || []) {
      const cur = worst.get(field);
      if (!cur || SEV[f.severity].rank < SEV[cur.severity].rank) worst.set(field, f);
    }
    for (const [field, f] of worst) {
      const label = document.getElementById('f-' + field)?.closest('[class*="col-"]')?.querySelector('label');
      if (!label) continue;
      const dot = el('button', {type: 'button', 'data-adv-badge': field, class: 'btn btn-link p-0 ms-1 align-baseline border-0',
        title: f.message, 'aria-label': `${SEV[f.severity].label}: ${f.message}`});
      dot.innerHTML = `<i class="bi ${SEV[f.severity].icon}" style="color:${SEV[f.severity].dot};font-size:.8em"></i>`;
      dot.addEventListener('click', () => focusFinding(f.id));
      label.append(dot);
    }
  }
  function focusFinding(id) {
    const n = document.querySelector(`[data-adv-id="${CSS.escape(id)}"]`);
    if (!n) return;
    n.scrollIntoView({block: 'center', behavior: 'smooth'});
    n.focus({preventScroll: true});
    n.classList.add('ff-adv-flash');
    setTimeout(() => n.classList.remove('ff-adv-flash'), 1500);
  }

  function selectedIds(items = visible()) {
    return items.filter(f => f.fix && checked(f)).map(f => f.id);
  }
  function updateReview(items) {
    const n = selectedIds(items).length, b = $id('ff-adv-review');
    b.disabled = readOnly || n === 0;
    b.textContent = n ? `Review ${n} change${n > 1 ? 's' : ''}…` : 'Review changes…';
  }

  /* ---- preview, then apply ------------------------------------------------ */
  const prettyField = k => k.replaceAll('_', ' ');
  const show = v => v === true ? 'on' : v === false ? 'off' : (v === '' || v == null ? '(source / auto)' : String(v));

  async function review() {
    const ids = selectedIds();
    if (!ids.length) return;
    let res;
    try {
      res = await api('/api/ffmpeg/advice/preview', {method: 'POST', body: {
        options: host.readFields(), goal, name: host.name(), ids}});
    } catch { return; }
    const body = el('div');
    const rows = res.changes.map(c => `<tr>
        <th class="text-nowrap fw-normal">${esc(prettyField(c.field))}</th>
        <td class="mono small text-break">${esc(show(c.from))}</td>
        <td class="text-center">→</td>
        <td class="mono small text-break fw-semibold">${esc(show(c.to))}</td>
        <td class="small text-muted">${c.ids.map(esc).join(', ')}</td></tr>`).join('');
    body.innerHTML = `
      <p class="small text-muted mb-2">These are the exact changes. Nothing is applied until you press
        <b>Apply changes</b>; you can still review the result and save or discard it afterwards.</p>
      ${res.changes.length ? `<table class="table table-sm align-middle mb-2"><thead><tr>
          <th>Field</th><th>Now</th><th></th><th>After</th><th>From</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="alert alert-secondary py-2 small">The selected suggestions change nothing.</div>'}
      ${res.skipped.length ? `<div class="alert alert-warning py-2 small"><b>Not included:</b><ul class="mb-0">${
          res.skipped.map(s => `<li><code>${esc(s.id)}</code>: ${esc(s.reason)}</li>`).join('')}</ul></div>` : ''}
      ${(res.errors || []).length ? `<div class="alert alert-danger py-2 small"><b>The result would be invalid:</b> ${
          res.errors.map(esc).join('; ')}</div>` : ''}
      <div class="small muted-label">Resulting command</div>
      <pre class="small bg-dark text-light p-2 mono mb-0" style="white-space:pre-wrap;max-height:200px;overflow:auto">${esc(res.command)}</pre>`;
    const footer = el('div', {class: 'd-flex gap-2'});
    const m = openModal({title: 'Review suggested changes', body, footer, size: 'lg'});
    footer.append(mBtn('Cancel', 'btn-outline-secondary', m.close));
    const ok = mBtn('Apply changes', 'btn-accent', () => {
      const out = {};
      for (const c of res.changes) out[c.field] = res.options[c.field];
      host.writeFields(out);
      ids.forEach(id => touched.delete(id));
      m.close();
      host.onApplied();
      toast(`${res.changes.length} change${res.changes.length === 1 ? '' : 's'} applied - review the command, then save the template`, 'ok');
    });
    if (!res.changes.length || (res.errors || []).length) ok.disabled = true;
    footer.append(ok);
  }

  return {init, load, state, refresh};
})();
