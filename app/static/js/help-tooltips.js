/* Shared, opt-in help for pages and dynamically-created dialogs.
 * Mark explanatory text with data-help="Subject" (optionally data-help-for="id").
 * Status, errors, results and destructive-action warnings are never inferred
 * from CSS classes or hidden by this component. Source nodes remain in place
 * so existing code can update them and aria-describedby references still work.
 */
window.HelpTooltips = (() => {
  const records = new Map();
  let active = null, queued = false;
  const clean = text => String(text || '').replace(/\s+/g, ' ').trim();
  const labelText = node => {
    const copy = node.cloneNode(true);
    copy.querySelectorAll('[data-help], .help-tip-button').forEach(n => n.remove());
    return clean(copy.textContent);
  };
  function dismiss(record = active) {
    if (!record) return;
    clearTimeout(record.timer);
    record.pinned = false;
    record.hovered = false;
    record.instance?.hide();
    if (active === record) active = null;
  }
  function add(anchor, content, attrs = {}, owner = anchor) {
    if (records.has(owner)) return records.get(owner).button;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'help-tip-button btn btn-link btn-sm p-0 ms-1 align-baseline';
    const label = attrs['aria-label'] || `${labelText(anchor) || 'Information'}: help`;
    button.setAttribute('aria-label', label);
    button.innerHTML = '<i class="bi bi-question-circle" aria-hidden="true"></i>';
    for (const [key, value] of Object.entries(attrs)) button.setAttribute(key, value);
    if (anchor.matches('.card-header,h1,h2,h3,h4,h5,h6,summary,div.muted-label')) anchor.append(button);
    else anchor.after(button);
    const record = {button, owner, content, instance:null, pinned:false, hovered:false, visible:false, timer:null, text:''};
    records.set(owner, record);
    const text = () => typeof content === 'function' ? content() : content;
    const show = () => {
      clearTimeout(record.timer);
      if (button.hidden || !button.isConnected || !clean(text())) return;
      if (active && active !== record) dismiss(active);
      const next = clean(text());
      // Bootstrap's manual show is not idempotent: re-showing an already
      // visible tip can schedule its own hide. Focus/hover/click share one tip.
      if (record.visible && record.text === next) { active = record; return; }
      if (record.visible) record.instance.hide();
      record.text = next;
      if (!record.instance) record.instance = new bootstrap.Tooltip(button, {
        title:() => clean(text()), trigger:'manual', html:false, animation:false,
        placement:'auto', container:button.closest('.modal') || document.body,
        customClass:'help-tooltip', boundary:'viewport',
      });
      record.instance.setContent({'.tooltip-inner':record.text});
      record.instance.show();
      active = record;
    };
    const laterHide = () => {
      clearTimeout(record.timer);
      record.timer = setTimeout(() => {
        if (!record.pinned && !record.hovered && document.activeElement !== button) dismiss(record);
      }, 180);
    };
    button.addEventListener('shown.bs.tooltip', () => { record.visible = true; });
    button.addEventListener('hidden.bs.tooltip', () => { record.visible = false; });
    button.addEventListener('mouseenter', show);
    button.addEventListener('mouseleave', laterHide);
    button.addEventListener('focus', show);
    button.addEventListener('blur', laterHide);
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation(); // help beside a checkbox/summary never toggles it
      record.pinned = !record.pinned;
      if (record.pinned) show(); else dismiss(record);
    });
    button.addEventListener('inserted.bs.tooltip', () => {
      const tip = document.getElementById(button.getAttribute('aria-describedby'));
      if (!tip) return;
      tip.addEventListener('mouseenter', () => { record.hovered = true; clearTimeout(record.timer); });
      tip.addEventListener('mouseleave', () => { record.hovered = false; laterHide(); });
    });
    return button;
  }
  function anchorFor(source) {
    const target = document.getElementById(source.dataset.helpFor || '');
    if (target) {
      if (target.matches('input,select,textarea')) {
        return target.labels?.[0] || target.closest('[class*="col-"], .mb-2')?.querySelector('label') || target;
      }
      return target;
    }
    const label = source.closest('label') || source.parentElement?.querySelector(':scope > label');
    if (label) return label;
    const previous = source.previousElementSibling;
    if (previous?.matches('h1,h2,h3,h4,h5,h6,label,.muted-label')) return previous;
    if (source.parentElement?.matches('.d-flex')) return source;
    const section = source.closest('.card, .modal');
    return section?.querySelector('.card-header, .modal-title') || source;
  }
  function sourceText(source) {
    let text = clean(source.textContent);
    // Keep documentation URLs discoverable without enabling HTML injection.
    source.querySelectorAll('a[href]').forEach(a => { text += ` (${a.getAttribute('href')})`; });
    return text;
  }
  function scan() {
    queued = false;
    for (const [owner, record] of records) {
      if (!owner.isConnected || !record.button.isConnected) {
        dismiss(record); record.instance?.dispose(); record.button.remove(); records.delete(owner);
      }
    }
    document.querySelectorAll('[data-help]').forEach(source => {
      if (!records.has(source)) add(anchorFor(source), () => sourceText(source),
        {'aria-label':`${source.dataset.help || 'Information'}: help`}, source);
      const record = records.get(source);
      const hidden = source.hidden || source.classList.contains('d-none') ||
        !!source.parentElement?.closest('[hidden], .d-none') || !sourceText(source);
      if (record.button.hidden !== hidden) record.button.hidden = hidden;
      if (hidden && active === record) dismiss(record);
    });
    // Existing field-label titles become the same accessible help icons.
    // Table-cell/action titles are deliberately not treated as help paragraphs.
    document.querySelectorAll('label[title], .muted-label[title]').forEach(label => {
      if (!label.title.trim()) return;
      const text = label.title;
      label.removeAttribute('title');
      add(label, text);
    });
    if (active) {
      const next = clean(typeof active.content === 'function' ? active.content() : active.content);
      if (next !== active.text) {
        active.text = next;
        if (next) {
          active.instance?.hide();
          active.instance?.setContent({'.tooltip-inner':next});
          active.instance?.show();
        } else dismiss(active);
      }
    }
  }
  function schedule() {
    if (!queued) { queued = true; requestAnimationFrame(scan); }
  }
  document.addEventListener('pointerdown', event => {
    if (active && !active.button.contains(event.target) && !event.target.closest('.help-tooltip')) dismiss(active);
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') dismiss(); });
  document.addEventListener('hide.bs.modal', dismissAll);
  function dismissAll() { dismiss(); }
  const observer = new MutationObserver(schedule);
  observer.observe(document.body, {subtree:true, childList:true, characterData:true,
    attributes:true, attributeFilter:['title','hidden','class','data-help','data-help-for']});
  schedule();
  return {add, scan, dismiss: dismissAll};
})();
