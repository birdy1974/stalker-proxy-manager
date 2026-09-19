/* Preset/custom controls and inline help for every template parameter. */
const FFmpegEditor = (() => {
  let schema, manual = false, redirect = false, advancedBusy = false;
  const CUSTOM = '__custom_value__';
  const input = key => document.getElementById('f-' + key);
  const custom = key => document.getElementById('f-' + key + '-custom');
  const choices = (select, values) => {
    select.replaceChildren(...values.map(v => el('option', {value:v}, v === '' ? 'Automatic / source (omit flag)' : v)));
  };
  function value(key) {
    const n = input(key);
    return n.type === 'checkbox' ? n.checked : n.value === CUSTOM && custom(key) ? custom(key).value.trim() : n.value;
  }
  function setValue(key, raw) {
    const n = input(key);
    if (!n || raw === undefined) return;
    if (n.type === 'checkbox') { n.checked = !!raw; return; }
    const v = String(raw ?? '');
    if (n.tagName === 'SELECT') {
      if (![...n.options].some(o => o.value === v)) {
        if (custom(key)) {
          n.value = CUSTOM; custom(key).value = v; custom(key).hidden = false;
          return;
        }
        n.append(el('option', {value:v}, `${v} (stored value)`));
      }
      if (custom(key)) custom(key).hidden = true;
    }
    n.value = v;
  }
  function help(node, key, text) {
    node.title = text;
    const label = node.closest('[class*="col-"]')?.querySelector('label');
    if (!label) return;
    label.htmlFor = node.id;
    const title = label.textContent.trim();
    label.removeAttribute('title');
    HelpTooltips.add(label, () => [text, document.getElementById(node.id + '-inactive')?.textContent].filter(Boolean).join(' '),
      {'aria-label':`${title}: help`, 'data-ff-help':key});
  }
  function setup(config) {
    schema = config;
    for (const [key, def] of Object.entries(schema.fields)) {
      let n = input(key);
      if (!n) continue;
      const initial = n.type === 'checkbox' ? n.checked : n.value;
      if (def.choices && n.tagName !== 'SELECT') {
        const select = el('select', {id:n.id, class:'form-select form-select-sm ff-field'});
        n.replaceWith(select); n = select;
      }
      if (def.choices) choices(n, def.choices);
      if (key === 'fps') [...n.options].find(o => o.value === '').textContent = 'src (source FPS)';
      if (n.tagName === 'SELECT' && def.custom) {
        n.append(el('option', {value:CUSTOM}, 'Custom value…'));
        const field = el('input', {id:n.id + '-custom', class:'form-control form-control-sm ff-field mt-1',
          type:'text', 'aria-label':`${key.replaceAll('_', ' ')}: custom value`, placeholder:'Enter a custom value'});
        field.hidden = true;
        if (def.max_length) field.maxLength = def.max_length;
        if (['integer','number','positive'].includes(def.kind)) field.inputMode = def.kind === 'integer' ? 'numeric' : 'decimal';
        n.after(field);
        n.addEventListener('change', () => {
          field.hidden = n.value !== CUSTOM;
          if (!field.hidden) field.focus();
        });
      }
      if (def.max_length && n.tagName === 'INPUT') n.maxLength = def.max_length;
      help(n, key, def.help);
      const note = el('div', {id:n.id + '-inactive', 'data-help-detail':'', class:'small text-muted mt-1'});
      note.hidden = true;
      n.closest('[class*="col-"]')?.append(note);
      n.setAttribute('aria-describedby', note.id);
      if (custom(key)) custom(key).setAttribute('aria-describedby', note.id);
      setValue(key, initial);
    }
    setupAdvanced();
    refresh();
  }
  function snapshot() {
    return Object.fromEntries(Object.keys(schema.fields).filter(k => input(k)).map(k => [k, value(k)]));
  }
  function disabledRules(group, values = snapshot()) {
    values.rc_mode = String(values.rc_mode || 'AUTO').toUpperCase();
    const disabled = {};
    for (const rule of schema.applicability[group]) {
      if (Object.entries(rule.when).every(([key, c]) => {
        const v = String(values[key] ?? '');
        return (!c.in || c.in.includes(v)) && (!c.not_in || !c.not_in.includes(v)) && (!c.regex || new RegExp(c.regex).test(v));
      })) rule.targets.forEach(key => { disabled[key] ??= rule.reason; });
    }
    return disabled;
  }
  function refresh() {
    const values = snapshot(), disabled = disabledRules('fields', values), options = disabledRules('options', values);
    for (const [key, def] of Object.entries(schema.fields)) {
      const n = input(key);
      if (!n) continue;
      let reason = disabled[key] || '';
      if (redirect) reason = 'Redirect bypasses FFmpeg. Switch to FFmpeg fields to edit these settings.';
      else if (manual) reason = 'The manual command is authoritative. Switch to fields mode to edit this setting.';
      if (['name', 'enabled'].includes(key) || (key === 'command' && !redirect)) reason = '';
      n.disabled = !!reason;
      for (const control of [n, custom(key)].filter(Boolean)) {
        control.disabled = !!reason;
        control.title = reason ? reason + ' Stored value is retained.' : def.help;
        if (reason) { control.setCustomValidity(''); control.classList.remove('is-invalid'); }
      }
      if (n.tagName === 'SELECT') {
        for (const opt of n.options) {
          const why = options[key + ':' + opt.value] || '';
          opt.disabled = !!why;
          opt.title = why;
        }
      }
      const note = document.getElementById(n.id + '-inactive');
      const choiceReason = options[key + ':' + values[key]];
      note.textContent = reason ? reason + ' Value kept.' : choiceReason ? 'Selected option is inactive: ' + choiceReason : '';
      note.hidden = !note.textContent;
    }
    refreshAdvanced(values);
  }
  function refreshAdvanced(values = snapshot()) {
    const disabled = disabledRules('advanced', values);
    const select = document.getElementById('ff-advanced-option');
    for (const opt of select.options) {
      const def = schema.advanced[Number(opt.value)];
      const why = opt.value === 'custom' ? '' : disabled[def.flag] || '';
      opt.disabled = !!why;
      opt.title = why;
    }
    const def = select.value === 'custom' ? null : schema.advanced[Number(select.value)];
    // Custom flags remain an escape hatch, but cannot bypass a known rule by
    // typing the name of a disabled guided option.
    const flag = def?.flag || document.getElementById('ff-advanced-flag').value.trim();
    const reason = redirect ? 'Redirect bypasses FFmpeg.' : manual ? 'Switch to fields mode to use the option helper.' : disabled[flag] || '';
    document.querySelectorAll('[data-ff-advanced]').forEach(n => {
      n.disabled = manual || redirect || (['ff-advanced-value', 'ff-advanced-add'].includes(n.id) && !!reason);
    });
    document.getElementById('ff-advanced-add').disabled ||= advancedBusy;
    document.getElementById('ff-advanced-help').textContent = (def?.help || 'Enter an option and choose its input/output placement. Form-owned options require manual command mode.') +
      (reason ? ' Unavailable: ' + reason : '') + ' Existing raw flags are kept; edit Extra flags to remove overrides.';
  }
  function valid(report = false) {
    let first = null;
    for (const [key, def] of Object.entries(schema.fields)) {
      const n = input(key);
      if (!n || n.disabled || n.type === 'checkbox') continue;
      const v = String(value(key));
      const field = n.value === CUSTOM && custom(key) ? custom(key) : n;
      let error = '';
      if (def.max_length && v.length > def.max_length) error = `Use at most ${def.max_length} characters, or edit the full command.`;
      if (v && !(v === 'AUTO' && def.choices?.includes('AUTO'))) {
        if (def.kind === 'rate' && (!/^\d+(\.\d+)?[kKmMgG]?$/.test(v) || parseFloat(v) <= 0)) error = 'Use a positive rate, e.g. 2500k or 2.5M.';
        if (['integer','number','positive'].includes(def.kind)) {
          const num = Number(v);
          if (!Number.isFinite(num) || (def.kind === 'integer' && !/^-?\d+$/.test(v))) error = 'Enter a valid ' + (def.kind === 'integer' ? 'whole number.' : 'number.');
          else if ((def.min !== null && (def.kind === 'positive' ? num <= def.min : num < def.min)) || (def.max !== null && num > def.max))
            error = `Use ${def.kind === 'positive' ? 'more than' : 'at least'} ${def.min} and at most ${def.max}.`;
        }
      }
      if (['video_codec','audio_codec'].includes(key) && (!v || !/^[\w.:-]+$/.test(v)))
        error = 'Enter one encoder name, copy, or an allowed mode—not additional flags.';
      if (key === 'aspect' && !/^[1-9]\d{0,2}:[1-9]\d{0,2}$/.test(v))
        error = 'Enter a positive width:height ratio, such as 16:9.';
      if (key === 'resolution' && v !== 'source') {
        const dimensions = v.match(/^(\d+)x(\d+)$/), height = v.match(/^(\d+)p$/);
        const sizes = dimensions ? [+dimensions[1], +dimensions[2]] : height ? [+height[1]] : [];
        if (!sizes.length || sizes.some(n => n < 16 || n > 8192 || n % 2))
          error = 'Use even dimensions from 16 to 8192 (e.g. 1600x900), a height such as 900p, or source.';
      }
      field.setCustomValidity(error);
      field.classList.toggle('is-invalid', !!error);
      if (error && !first) first = field;
    }
    if (report && first) first.reportValidity();
    return !first;
  }
  function setupAdvanced() {
    const select = document.getElementById('ff-advanced-option');
    schema.advanced.forEach((d, i) => select.append(el('option', {value:String(i)}, `${d.side === 'input' ? 'Input' : 'Output'} · ${d.label} (${d.flag})`)));
    select.append(el('option', {value:'custom'}, 'Other FFmpeg option…'));
    const change = () => {
      const d = schema.advanced[Number(select.value)];
      const isCustom = select.value === 'custom';
      document.getElementById('ff-advanced-custom').hidden = !isCustom;
      document.getElementById('ff-advanced-help').textContent = isCustom
        ? 'Enter an option name and its value (leave value empty for a switch). Choose before/after -i. Build-specific options require the installed FFmpeg documentation. Form-owned flags such as -vf/-map require manual command mode.' : d.help;
      const v = document.getElementById('ff-advanced-value'); v.value = isCustom ? '' : (d.choices[0] || '');
      document.getElementById('ff-advanced-suggestions').replaceChildren(...(isCustom ? [] : d.choices).map(x => el('option', {value:x})));
      document.getElementById('ff-advanced-status').textContent = '';
    };
    select.addEventListener('change', () => { change(); refreshAdvanced(); }); change();
    document.getElementById('ff-advanced-flag').addEventListener('input', () => refreshAdvanced());
    document.getElementById('ff-advanced-add').addEventListener('click', async () => {
      const button = document.getElementById('ff-advanced-add');
      const note = document.getElementById('ff-advanced-status');
      const isCustom = select.value === 'custom';
      const d = isCustom ? {side:document.getElementById('ff-advanced-side').value, flag:document.getElementById('ff-advanced-flag').value.trim()}
        : schema.advanced[Number(select.value)];
      if (manual || redirect || disabledRules('advanced')[d.flag]) return;
      const context = snapshot();
      advancedBusy = true; button.disabled = true;
      try {
        const result = await api('/api/ffmpeg/extra-option', {method:'POST', body:{
          side:d.side, flag:d.flag, value:document.getElementById('ff-advanced-value').value,
          raw:value('extra_' + d.side), options:context,
        }});
        if (manual || redirect || JSON.stringify(snapshot()) !== JSON.stringify(context)) return;
        setValue('extra_' + d.side, result.extra);
        input('extra_' + d.side).dispatchEvent(new Event('change', {bubbles:true}));
        note.textContent = `${d.flag} added/replaced in Extra ${d.side} flags. Save template to persist it.`;
        note.classList.remove('text-danger');
      } catch (error) { note.textContent = error.message; note.classList.add('text-danger'); }
      finally { advancedBusy = false; refreshAdvanced(); }
    });
  }
  function mode(isManual, isRedirect = false) {
    manual = isManual; redirect = isRedirect;
    refresh();
  }
  return {setup, value, setValue, valid, mode, refresh, disabledRules};
})();
