/* Stalker Proxy Manager - shared UI toolkit.
 * Conventions (from the spec):
 *  - popups NEVER close on backdrop click / Esc - only explicit buttons
 *  - table headers are sticky, sortable, and host the filter inputs
 *  - all filters are case-insensitive (server does ilike; selects normalize)
 */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (tag, attrs = {}, ...kids) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "html") n.innerHTML = v;
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null) n.append(kid);
  return n;
};

/* ----------------------------------------------------- time formatting */
/* Every timestamp the API sends is UTC (ISO 8601, usually WITHOUT an offset
 * suffix - SQLite returns naive datetimes, so `isoformat()` carries no zone).
 * Browsers parse an offset-less ISO string as LOCAL time, which displays the
 * raw UTC wall clock (2h behind on a Berlin summer evening). fmtTime treats
 * offset-less datetimes as UTC and renders them in the browser's local zone.
 * `seconds: false` keeps the short `YYYY-MM-DD HH:MM` form some cards use. */
function fmtTime(iso, { seconds = true } = {}) {
  if (!iso) return "";
  const s = String(iso).trim().replace(" ", "T");
  const stamped = /[T]\d{2}:/.test(s) && !/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s + "Z" : s;
  const d = new Date(stamped);
  if (isNaN(d)) return String(iso);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}${seconds ? ":" + p(d.getSeconds()) : ""}`;
}

/* ---------------------------------------------------------------- API */
async function api(path, { method = "GET", body, raw = false, signal } = {}) {
  const opts = { method, headers: {}, signal };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  if (r.status === 401 && !path.startsWith("/api/login")) { location.href = "/login"; throw new Error("401"); }
  if (raw) return r;
  let data = null;
  try { data = await r.json(); } catch { data = {}; }
  if (!r.ok) {
    const msg = data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `${r.status} ${r.statusText}`;
    toast(msg, "error");
    throw new Error(msg);
  }
  return data;
}

/* -------------------------------------------------------------- toasts */
function toast(msg, kind = "info", ms = 4500) {
  const cls = { info: "text-bg-dark", ok: "text-bg-success", error: "text-bg-danger", warn: "text-bg-warning" }[kind] || "text-bg-dark";
  const t = el("div", { class: `toast align-items-center ${cls} border-0 show` });
  t.innerHTML = `<div class="d-flex"><div class="toast-body">${esc(msg)}</div>
    <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button></div>`;
  $("#toast-area").append(t);
  t.addEventListener("click", (e) => { if (e.target.closest("[data-bs-dismiss]")) t.remove(); });
  setTimeout(() => t.remove(), ms);
}

/* --------------------------------------------------------------- modal */
/* Static backdrop, no Esc: the ONLY ways out are the explicit buttons (spec).
 * `closeButton: true` additionally puts an explicit "×" in the header - still
 * a button, so it does not violate the spec - for popups that are a "window"
 * in the user's mind (the player/preview popup) rather than a form. */
function openModal({ title, body, footer, size = "lg", onClose, extraClass = "", closeButton = false }) {
  const wrap = el("div", { class: `modal fade ${extraClass}` });
  wrap.innerHTML = `<div class="modal-dialog modal-${size} modal-dialog-scrollable">
    <div class="modal-content">
      <div class="modal-header py-2"><h6 class="modal-title fw-semibold"></h6></div>
      <div class="modal-body"></div>
      <div class="modal-footer py-2"></div>
    </div></div>`;
  $(".modal-title", wrap).textContent = title;
  $(".modal-body", wrap).append(body);
  $(".modal-footer", wrap).append(footer || el("div"));
  document.body.append(wrap);
  const modal = new bootstrap.Modal(wrap, { backdrop: "static", keyboard: false });
  let shown = false, closing = false, resolveClosed;
  const closed = new Promise(resolve => { resolveClosed = resolve; });
  wrap.addEventListener("shown.bs.modal", () => { shown = true; if (closing) modal.hide(); }, { once:true });
  wrap.addEventListener("hidden.bs.modal", () => {
    modal.dispose(); wrap.remove(); resolveClosed();
  }, { once:true });
  // Bootstrap ignores hide() during its opening transition. Wait for shown,
  // then remove only after hidden so fast Close/Edit cannot orphan a backdrop.
  const close = () => {
    if (!closing) { closing = true; if (onClose) onClose(); if (shown) modal.hide(); }
    return closed;
  };
  if (closeButton) {
    $(".modal-header", wrap).append(
      el("button", { type: "button", class: "btn-close", "aria-label": "Close", onclick: close }));
  }
  modal.show();
  // `footer` is the LIVE .modal-footer node: callers (the player popup)
  // append their buttons after openModal returns. Returning the modal without
  // it made `m.footer.append(...)` throw a TypeError right after the popup
  // opened - the player was never attached, the footer stayed empty and the
  // popup was a dead black box (the original "preview gets no input" bug).
  return { close, root: wrap, modal, footer: $(".modal-footer", wrap) };
}
const mBtn = (label, cls, fn, icon = "") =>
  el("button", { class: `btn btn-sm ${cls}`, type: "button", onclick: fn, html: (icon ? `<i class="bi ${icon} me-1" aria-hidden="true"></i>` : "") + esc(label) });

function confirmDialog({ title, body, okText = "OK", okClass = "btn-accent", onOk, wide = false }) {
  const b = el("div"); if (typeof body === "string") b.innerHTML = body; else b.append(body);
  const footer = el("div", { class: "d-flex gap-2" });
  const m = openModal({ title, body: b, footer, size: wide ? "xl" : "md" });
  footer.append(mBtn("Cancel", "btn-outline-secondary", m.close), mBtn(okText, okClass, async () => { await onOk?.(); m.close(); }));
  return m;
}

/* ------------------------------------------------------------ data table
 * columns: [{ key, label, sort, width, render(row), filter:{type:'text'|'select',
 *            options:[{v,l}] | optionsLoader(fn), placeholder}}]
 * opts.server: fetch(params)->Promise<{total,page,per_page,items,...}>
 * opts.selectable, opts.onRowClick(row), opts.defaultSort, opts.perPageDefault,
 * opts.extraHeader(node) -> toolbar slot, opts.dnd({onReorder(idsOrdered)})
 */
class DataTable {
  constructor(host, opts) {
    this.host = host; this.opts = opts;
    this.state = { page: 1, per_page: opts.perPageDefault || 25, sort: opts.defaultSort || "", direction: "asc", filters: {} };
    this.selected = new Set(); this.items = []; this.total = 0;
    this._build();
    this.reload();
  }
  _build() {
    this.host.innerHTML = "";
    this._textFilters = [];
    this.toolbar = el("div", { class: "d-flex flex-wrap align-items-center gap-2 mb-2" });
    this.spinner = el("div", { class: "loading-bar mb-1", style: "display:none" });
    this.tableHost = el("div", { class: "table-host" });
    this.table = el("table", { class: "table table-sm table-hover table-spm" });
    this.tableHost.append(this.table);
    this.pager = el("div", { class: "d-flex flex-wrap align-items-center gap-2 mt-2 small" });
    this.host.append(this.toolbar, this.spinner);
    if (this.opts.dnd) {
      this.orderStatus = el("div", { class: "order-status small mb-2", role: "status", "aria-live": "polite", hidden: "" });
      this.host.append(this.orderStatus);
    }
    this.host.append(this.tableHost, this.pager);
    if (this.opts.extraHeader) this.toolbar.append(this.opts.extraHeader);
    this._buildHead();
  }
  params() {
    const p = new URLSearchParams({ page: this.state.page, per_page: this.state.per_page });
    if (this.state.sort) { p.set("sort", this.state.sort); p.set("direction", this.state.direction); }
    /* filters may carry a server-side param override (e.g. textbox -> "q"),
       otherwise the column key itself is sent */
    for (const c of this.opts.columns) {
      if (!c.filter) continue;
      const k = c.filter.param || c.key;
      const v = this.state.filters[k] ?? "";
      if (v !== "" && v != null) p.set(k, v);
    }
    return p.toString();
  }
  async reload({ duringReorder = false } = {}) {
    if (this._pendingEdits) { this._reloadAfterEdit = true; return false; }
    if (this._reordering && !duringReorder) { this._reloadAfterReorder = true; return false; }
    const revision = this._loadRevision = (this._loadRevision || 0) + 1;
    this._loading = true;
    this.spinner.style.display = "block";
    try {
      const data = await this.opts.server(this.params());
      if (revision !== this._loadRevision) return false;
      this.items = data.items || []; this.total = data.total || 0;
      this.lastData = data;
      this._loading = false;
      this._render();
      return true;
    } catch (e) { return false; /* toast already shown */ }
    finally {
      if (revision === this._loadRevision) {
        this._loading = false;
        this.spinner.style.display = "none";
      }
    }
  }
  _buildHead() {
    const o = this.opts;
    /* header + filter rows are built ONCE (never re-created on reload), so
       typing into a filter never loses focus */
    const htr = el("tr"), ftr = el("tr", { class: "filters" });
    if (o.selectable) {
      const cb = el("input", { type: "checkbox", class: "form-check-input" });
      this._selAll = cb;
      cb.addEventListener("change", () => {
        this.items.forEach(r => cb.checked ? this.selected.add(r.id) : this.selected.delete(r.id));
        this._renderBody(); o.onSelection?.(this.selected);
      });
      htr.append(el("th", { style: "width:28px" }, cb));
      ftr.append(el("th"));
    }
    if (o.dnd) { htr.append(el("th", { style: "width:26px" })); ftr.append(el("th")); }
    for (const c of o.columns) {
      const th = el("th", { class: c.sort ? "sortable" : "", style: c.width ? `width:${c.width}` : "" });
      th.append(el("span", { html: esc(c.label) }));
      if (c.sort) {
        c._ind = el("span", { class: "text-accent" });
        th.append(c._ind);
        th.addEventListener("click", () => {
          if (this.state.sort === c.sort) this.state.direction = this.state.direction === "asc" ? "desc" : "asc";
          else { this.state.sort = c.sort; this.state.direction = "asc"; }
          this.state.page = 1; this.reload();
        });
      }
      htr.append(th);
      const fth = el("th");
      if (c.filter) {
        if (c.filter.type === "select") {
          const fk = c.filter.param || c.key;
          const sel = el("select", { class: "form-select form-select-sm" }, el("option", { value: "" }, c.filter.placeholder || "All"));
          c._sel = sel;
          c._fill = (opts) => {
            const cur = this.state.filters[fk] ?? "";
            sel.innerHTML = ""; sel.append(el("option", { value: "" }, c.filter.placeholder || "All"));
            for (const op of opts) sel.append(el("option", { value: String(op.v ?? op) }, op.l ?? op));
            if (cur && ![...sel.options].some(x => x.value === cur)) sel.append(el("option", { value: cur }, cur));
            sel.value = cur;
          };
          if (c.filter.optionsLoader) { c.filter.optionsLoader(c._fill, this); if (c.filter.options) c._fill(c.filter.options); }
          else c._fill(c.filter.options || []);
          sel.value = this.state.filters[fk] ?? "";
          sel.addEventListener("change", () => { this.state.filters[fk] = sel.value; this.state.page = 1; this.reload(); });
          fth.append(sel);
        } else {
          /* text filters update the visible rows INSTANTLY while typing
             (client-side, case-insensitive), then the debounced server reload
             confirms with the full, paginated result */
          const fk = c.filter.param || c.key;
          const inp = el("input", { class: "form-control form-control-sm", placeholder: c.filter.placeholder || "filter…" });
          inp.value = this.state.filters[fk] ?? "";
          inp.dataset.filterKey = fk;
          inp.dataset.colIdx = o.columns.indexOf(c);
          this._textFilters.push({ inp, fk });
          let t; inp.addEventListener("input", () => {
            this.state.filters[fk] = inp.value;
            this._clientFilter();
            clearTimeout(t);
            t = setTimeout(() => { this.state.page = 1; this.reload(); }, 250);
          });
          inp.dataset.filterKey = fk;
          fth.append(inp);
        }
      }
      ftr.append(fth);
    }
    const thead = el("thead"); thead.append(htr, ftr); this.table.append(thead);
    this.tbody = el("tbody"); this.table.append(this.tbody);
  }
  /* instantaneous visual feedback for text filters: hide/show rows of the
     CURRENT page based on all active text filter values. Server reload follows. */
  _clientFilter() {
    if (!this.tbody) return;
    const offset = (this.opts.selectable ? 1 : 0) + (this.opts.dnd ? 1 : 0);
    const active = (this._textFilters || []).filter(x => x.inp.value.trim() !== "");
    for (const tr of this.tbody.children) {
      let show = true;
      for (const f of active) {
        const cell = tr.children[offset + (+f.inp.dataset.colIdx)];
        const txt = (cell?.textContent || "").toLowerCase();
        if (!txt.includes(f.inp.value.trim().toLowerCase())) { show = false; break; }
      }
      tr.style.display = show ? "" : "none";
    }
  }
  _updateSortInd() {
    for (const c of this.opts.columns) {
      if (c._ind) c._ind.innerHTML = this.state.sort === c.sort
        ? (this.state.direction === "asc" ? " ▲" : " ▼") : "";
    }
  }
  _render() {
    this._updateSortInd();
    /* select filters with dynamic loaders (e.g. group lists) refresh their
       options from the freshly loaded data, keeping the current value */
    for (const c of this.opts.columns)
      if (c.filter?.type === "select" && c.filter.optionsLoader && c._fill)
        c.filter.optionsLoader(c._fill, this);
    if (this._selAll) this._selAll.checked =
      this.items.length > 0 && this.items.every(r => this.selected.has(r.id));
    this._renderBody();
    this._renderPager();
    this._clientFilter();
    /* one hook after the body exists, for columns whose content is filled by a
       second request (the "Now" tooltip on the sources page): the component owns
       when a row is in the DOM, and a page that guessed by polling would refetch
       on a timer for a table that has not changed */
    if (this.opts.onRender) this.opts.onRender(this);
  }
  _renderBody() {
    const o = this.opts; this.tbody.innerHTML = "";
    if (!this.items.length) {
      this.tbody.append(el("tr", {}, el("td", { colspan: 12, class: "text-center text-muted py-4" },
        o.emptyText || "No data - fetch a portal first (Portals → Fetch).")));
    }
    for (const row of this.items) {
      const tr = el("tr", { "data-id": row.id });
      if (o.onRowClick) { tr.style.cursor = "pointer"; tr.addEventListener("click", (e) => { if (!e.target.closest("button,input,select,a")) o.onRowClick(row); }); }
      if (o.dnd) this._bindDnd(tr, row, !o.dnd.isLocked?.(row));
      if (o.selectable) {
        const cb = el("input", { type: "checkbox", class: "form-check-input" });
        cb.checked = this.selected.has(row.id);
        cb.addEventListener("change", () => { cb.checked ? this.selected.add(row.id) : this.selected.delete(row.id); o.onSelection?.(this.selected); });
        tr.append(el("td", {}, cb));
      }
      for (const c of o.columns) {
        const td = el("td", { class: c.class || "" });
        const v = c.render ? c.render(row) : row[c.key];
        if (v instanceof Node) td.append(v); else td.innerHTML = v ?? "";
        // Cells are ellipsised by CSS, so every one of them needs a tooltip or
        // long titles (VOD especially) become unreadable with no way to see the
        // rest. Columns with a render() used to get title="" - i.e. nothing.
        td.title = c.title ? String(c.title(row)) : (td.textContent || "").trim().replace(/\s+/g, " ");
        tr.append(td);
      }
      this.tbody.append(tr);
    }
  }
  beginEdit() {
    if (!this._pendingEdits) {
      // Ignore any pre-save list response; never repaint a field being edited.
      this._reloadAfterEdit = Boolean(this._loading);
      ++this._loadRevision;
      this._loading = false;
      this.spinner.style.display = "none";
      this._editControls = [...this.host.querySelectorAll("button"), this._selAll].filter(Boolean)
        .map(node => [node, node.disabled]);
      this._editControls.forEach(([node]) => { node.disabled = true; });
    }
    this._pendingEdits = (this._pendingEdits || 0) + 1;
  }
  endEdit() {
    this._pendingEdits = Math.max(0, (this._pendingEdits || 0) - 1);
    if (!this._pendingEdits) {
      this._editControls?.forEach(([node, disabled]) => { node.disabled = disabled; });
      if (this._reloadAfterEdit) { this._reloadAfterEdit = false; this.reload(); }
    }
  }
  _orderMessage(text, state) {
    this.orderStatus.hidden = false;
    this.orderStatus.className = `order-status small mb-2 text-${state === "error" ? "danger" : state === "saving" ? "primary" : "success"}`;
    this.orderStatus.replaceChildren();
    if (state === "saving") this.orderStatus.append(el("span", {
      class: "spinner-border spinner-border-sm me-2", "aria-hidden": "true" }));
    this.orderStatus.append(document.createTextNode(text));
  }
  _orderBusy(busy) {
    this._reordering = busy;
    this.tableHost.setAttribute("aria-busy", String(busy));
    this.tableHost.classList.toggle("order-saving", busy);
    // Prevent a second drag or a conflicting edit while the transaction runs.
    // The status remains outside these inert controls for assistive technology.
    for (const node of [this.toolbar, this.table, this.pager]) node.inert = busy;
  }
  async _saveOrder(fromId, targetId) {
    if (this._reordering || this._loading || this._pendingEdits || this.state.sort !== "order" || this.state.direction !== "asc") return;
    const before = this.items;
    const from = before.findIndex(r => r.id === fromId), target = before.findIndex(r => r.id === targetId);
    if (from < 0 || target < 0 || from === target || this.opts.dnd.isLocked?.(before[from])) return;
    const scrollTop = this.tableHost.scrollTop;
    const next = [...before];
    next.splice(target, 0, next.splice(from, 1)[0]);
    this._orderBusy(true);
    this._orderMessage("Saving order…", "saving");
    this.items = next;
    this._renderBody(); // immediate visual move; numbers are confirmed by the server
    this._clientFilter();
    this.tableHost.scrollTop = scrollTop;
    try {
      const result = await this.opts.dnd.onReorder(next.map(r => r.id));
      const positions = new Map((result?.items || []).map(r => [r.id, r]));
      const complete = next.every(r => positions.has(r.id));
      if (complete) {
        this.items = next.map(r => ({ ...r, ...positions.get(r.id) }));
        if (this.lastData) this.lastData.items = this.items;
      }
      // Compatibility with older servers, concurrent deletions, or a queued
      // filter refresh. The normal path needs only the one save request.
      let refreshed = true;
      if (!complete || this._reloadAfterReorder) {
        this._reloadAfterReorder = false;
        this._orderMessage("Order saved. Refreshing…", "saving");
        refreshed = await this.reload({ duringReorder: true });
      }
      this._orderMessage(refreshed ? "Order saved." : "Order saved, but refresh failed. Reload the table to verify positions.", refreshed ? "success" : "error");
    } catch (e) {
      this.items = before;
      this._orderMessage("Could not save order. Previous display restored. Please try again.", "error");
    } finally {
      this._orderBusy(false);
      this._renderBody();
      this._clientFilter();
      this.tableHost.scrollTop = scrollTop;
      if (this._reloadAfterReorder) { this._reloadAfterReorder = false; this.reload(); }
    }
  }
  _bindDnd(tr, row, canDrag = true) {
    const grip = el("i", { class: "bi bi-grip-vertical row-drag" });
    if (!canDrag) {
      // Locked rows accept drops, but cannot themselves be dragged.
      grip.className = "bi bi-lock-fill row-drag row-locked";
      grip.title = "channel number locked - drag another row to move it around this one";
    } else if (this.state.sort !== "order" || this.state.direction !== "asc") {
      grip.title = "Sort by Ord ascending to reorder";
    }
    const g = el("td", {}, grip);
    tr.insertBefore(g, tr.children[1] || null);
    const interactive = target => target.closest("input,textarea,select,button,a,[contenteditable],[data-no-row-drag]");
    const allowed = () => canDrag && !this._reordering && !this._pendingEdits && this.state.sort === "order" && this.state.direction === "asc";
    tr.draggable = allowed();
    // A draggable ancestor otherwise steals native text selection in inputs.
    const arm = e => { tr.draggable = allowed() && !interactive(e.target); };
    tr.addEventListener("pointerdown", arm);
    tr.addEventListener("mousedown", arm);
    tr.addEventListener("focusin", e => { if (interactive(e.target)) tr.draggable = false; });
    tr.addEventListener("dragstart", (e) => {
      if (interactive(e.target)) return; // native text drag, not a row reorder
      if (!tr.draggable || this._reordering || this._loading || this._pendingEdits) { e.preventDefault(); return; }
      this._dragRow = row; tr.classList.add("dragging");
      e.dataTransfer?.setData("text/plain", String(row.id));
    });
    tr.addEventListener("dragend", () => {
      this._dragRow = null;
      tr.classList.remove("dragging");
      $$("tr", this.tbody).forEach(x => x.classList.remove("drop-highlight"));
    });
    tr.addEventListener("dragover", (e) => {
      if (!this._dragRow || this._reordering || this._loading || this._pendingEdits || interactive(e.target)) return;
      e.preventDefault(); tr.classList.add("drop-highlight");
    });
    tr.addEventListener("dragleave", () => tr.classList.remove("drop-highlight"));
    tr.addEventListener("drop", (e) => {
      if (interactive(e.target)) {
        if (this._dragRow) e.preventDefault(); // don't insert a dragged row ID into a textbox
        return;
      }
      if (!this._dragRow) return; // leave external/native text drops alone
      e.preventDefault();
      $$("tr", this.tbody).forEach(x => x.classList.remove("drop-highlight"));
      const from = this._dragRow; this._dragRow = null;
      if (from) this._saveOrder(from.id, row.id);
    });
  }
  _renderPager() {
    const pages = Math.max(1, Math.ceil(this.total / this.state.per_page));
    const p = this.pager; p.innerHTML = "";
    const btn = (label, page, dis = false, act = false) =>
      el("button", { class: `btn btn-sm ${act ? "btn-accent" : "btn-outline-secondary"}`, disabled: dis ? "" : null, onclick: () => { this.state.page = page; this.reload(); } }, label);
    p.append(
      btn("«", 1, this.state.page <= 1), btn("‹", this.state.page - 1, this.state.page <= 1),
      el("span", { class: "mx-1" }, `Page ${this.state.page} / ${pages}`),
      btn("›", this.state.page + 1, this.state.page >= pages), btn("»", pages, this.state.page >= pages));
    const jump = el("input", { type: "number", class: "form-control form-control-sm", style: "width:70px", min: 1, max: pages, placeholder: "go" });
    jump.addEventListener("keydown", (e) => { if (e.key === "Enter") { this.state.page = Math.min(pages, Math.max(1, +jump.value || 1)); this.reload(); } });
    const per = el("select", { class: "form-select form-select-sm", style: "width:78px" },
      [10, 25, 50, 100, 250].map(n => el("option", { value: n, selected: n === this.state.per_page ? "" : null }, `${n} / page`)));
    per.addEventListener("change", () => { this.state.per_page = +per.value; this.state.page = 1; this.reload(); });
    p.append(el("span", { class: "ms-2 text-muted" }, `${this.total.toLocaleString()} items`), jump, per);
  }
}

/* ----------------------------------------------- detail-popup helpers
 * Shared by Input Sources and Playlist Builder popup enrichment:
 * - fmtDur(seconds) -> "h:mm:ss" / "m:ss"
 * - probeHtml(probe) -> table with codec/resolution/ratio/fps/bitrate/audio
 * - tmdbHtml(tmdb)  -> TMDB block, or a hint when no key is configured */
const fmtDur = (sec) => {
  if (sec == null) return "?";
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), x = Math.floor(sec % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
};
const probeHtml = (pr) => {
  if (!pr) return "";
  if (pr.error) return `<span class="text-danger small">${esc(pr.error)}</span>`;
  const value = x => x === null || x === undefined || x === "" ? "Not reported" : esc(x);
  const row = (label, data) => `<tr><th class="small fw-normal text-muted" style="width:145px">${esc(label)}</th><td class="small">${data}</td></tr>`;
  let html = '<table class="table table-sm mb-0">';
  html += row("Container", value(pr.container));
  html += row("Duration", pr.duration_s ? fmtDur(pr.duration_s) : "Not reported / live");
  html += row("Overall bitrate", pr.overall_kbps ? `${value(pr.overall_kbps)} kb/s` : "Not reported");
  for (const v of pr.videos || (pr.video ? [pr.video] : [])) {
    html += row(`Video${v.index != null ? " #" + v.index : ""}`, `${value(v.codec)} · profile ${value(v.profile)} · level ${value(v.level)}`);
    html += row("Resolution / frame rate", `${value(v.width)} × ${value(v.height)} · ${value(v.fps)} fps · DAR ${value(v.ratio)}`);
    html += row("Video bitrate / format", `${value(v.kbps)} kb/s · ${value(v.pixel_format)} · ${value(v.bits_per_raw_sample)} bits · ${value(v.field_order)}`);
    if (v.color_space || v.color_transfer || v.color_primaries) html += row("Color", [v.color_space, v.color_transfer, v.color_primaries].filter(Boolean).map(esc).join(" · "));
  }
  for (const a of pr.audio || []) html += row(`Audio${a.index != null ? " #" + a.index : ""}`,
    `${value(a.codec)} · ${value(a.profile)} · ${value(a.kbps)} kb/s · ${value(a.rate_hz)} Hz · ${value(a.channels)} channels${a.channel_layout ? " (" + esc(a.channel_layout) + ")" : ""}${a.language ? " · " + esc(a.language) : ""}${a.sample_format ? " · " + esc(a.sample_format) : ""}`);
  for (const sub of pr.subtitles || []) html += row(`Subtitle #${sub.index}`, `${value(sub.codec)}${sub.language ? " · " + esc(sub.language) : ""}`);
  html += '</table>';
  if (pr.notice) html += `<div class="small text-warning mt-1">${esc(pr.notice)}</div>`;
  if (pr.technical) html += `<details class="mt-2"><summary class="small">All technical metadata (JSON)</summary><pre class="small border rounded p-2 mt-1" style="max-height:320px;overflow:auto;white-space:pre-wrap">${esc(JSON.stringify(pr.technical, null, 2))}</pre></details>`;
  return html;
};
const tmdbHtml = (t) => !t
  ? `<span class="text-muted small">No TMDB hit.</span><span data-help="TMDB enrichment">Set the TMDB API key in Settings → TMDB for enrichment.</span>`
  : (t.error
    ? `<span class="text-warning small">TMDB: ${esc(t.error)}</span>`
    : `<div class="small">
      <div class="mb-1">${t.tagline ? `<i>${esc(t.tagline)}</i><br>` : ""}
      ${(t.genres || []).map(g => `<span class="badge text-bg-light me-1">${esc(g)}</span>`).join("")}
      ${t.vote_average ? `<span class="badge text-bg-warning me-1">★ ${Number(t.vote_average).toFixed(1)}</span>` : ""}
      ${t.status ? `<span class="badge text-bg-secondary">${esc(t.status)}</span>` : ""}</div>
      ${t.overview ? `<div class="mb-1">${esc(t.overview)}</div>` : ""}
      <table class="table table-sm mb-0">
        ${t.title ? `<tr><td class="muted-label" style="width:120px">TMDB</td><td>${esc(t.title)}${t.original_title && t.original_title !== t.title ? ` (${esc(t.original_title)})` : ""} ${t.tmdb_id ? `<a href="https://www.themoviedb.org/${t.type}/${t.tmdb_id}" target="_blank" rel="noopener">↗</a>` : ""}</td></tr>` : ""}
        ${t.release_date ? `<tr><td class="muted-label">Released</td><td>${esc(t.release_date)}</td></tr>` : ""}
        ${t.runtime_min ? `<tr><td class="muted-label">Runtime</td><td>${t.runtime_min} min</td></tr>` : ""}
        ${t.director ? `<tr><td class="muted-label">Director</td><td>${esc(t.director)}</td></tr>` : ""}
        ${(t.cast || []).length ? `<tr><td class="muted-label">Cast</td><td>${esc(t.cast.join(", "))}</td></tr>` : ""}
        ${t.seasons_count ? `<tr><td class="muted-label">Seasons/Eps.</td><td>${t.seasons_count} / ${t.episodes_count ?? "?"}</td></tr>` : ""}
      </table></div>`);

/* ------------------------------------------------------------- player */
/* HLS -> hls.js; MPEG-TS (what Stalker proxies output) -> mpegts.js.
 *
 * The old version attached an engine and showed nothing on failure, so every
 * problem rendered as "popup with a player, black screen": a missing library,
 * a 404/502 from the proxy, an unplayable codec (MSE only does H.264/AAC -
 * HEVC or AC3/E-AC3/DTS IPTV streams cannot be transmuxed in a browser), or an
 * empty response body. All four now say what happened.
 */
const MSE_OK_VIDEO = /^(avc1(?:\.[0-9a-f]+)?|h264|avc)$/i;
const MSE_OK_AUDIO = /^(mp4a(?:\.[0-9a-z.]+)?|aac)$/i;
const browserPlayable = (codec) => !codec || MSE_OK_VIDEO.test(codec) || MSE_OK_AUDIO.test(codec);

function playInModal(url, title) {
  /* Muted + playsinline autoplay: Chromium (Chrome/Brave/Edge) and every
     iframe-embedded player block UNMUTED autoplay, which rendered this popup
     as a black box "that does not get any input" even while the stream was
     flowing. Sound comes back via the sound button (a user gesture, so the
     browser allows it). */
  const video = el("video", { controls: "", autoplay: "", muted: "", playsinline: "",
                              class: "w-100", style: "background:#000;max-height:65vh" });
  // Dynamically setting the muted attribute only changes defaultMuted in
  // some browsers; the live property must be set before attaching MediaSource.
  video.muted = true;
  video.defaultMuted = true;
  const status = el("div", { class: "small text-muted mt-1" }, `Source: ${url}`);
  const diag = el("div", { class: "small mt-2 p-2 bg-dark text-light mono",
                           style: "max-height:180px;overflow:auto;white-space:pre-wrap;border-radius:4px" });
  const body = el("div", {}, video, status, diag);
  let engine = null, settled = false, ticker = null, received = 0, inputSeen = false, lastStats = "";
  let soundBtn = null, closed = false, stopped = false, probeController = null;
  const stopPlayback = () => {
    stopped = true;
    clearInterval(ticker); ticker = null;
    try { engine?.destroy(); } catch {}
    engine = null;
    try { video.pause(); video.removeAttribute("src"); video.load(); } catch {}
    if (window.__spmActiveDiag === pushDiag) window.__spmActiveDiag = null;
  };

  const diagLines = [];
  const pushDiag = (line) => {
    if (closed) return;
    const t = new Date().toLocaleTimeString();
    diagLines.push(`[${t}] ${line}`);
    if (diagLines.length > 40) diagLines.splice(0, diagLines.length - 40);
    diag.textContent = diagLines.join("\n");
    diag.scrollTop = diag.scrollHeight;
  };
  const say = (html, cls) => {
    if (closed || stopped) return;
    diag.className = `small mt-2 alert ${cls || "alert-warning"} mb-0 py-2`;
    diag.innerHTML = html;
  };
  const backToLog = () => {
    diag.className = "small mt-2 p-2 bg-dark text-light mono";
    diag.style.cssText = "max-height:180px;overflow:auto;white-space:pre-wrap;border-radius:4px";
    diag.textContent = diagLines.join("\n");
  };
  const ok = (txt) => { if (!closed && !stopped && !settled) { settled = true; status.textContent = txt; } };
  const fail = (why, hint) => {
    pushDiag("FAIL: " + why);
    say(`<div><b>Not playing:</b> ${esc(why)}</div>` +
        (hint ? `<div class="mt-1 text-muted">${hint}</div>` : "") +
        `<span data-help="Player diagnostics">Technical detail below - scroll the log.</span>`, "alert-warning");
  };

  const m = openModal({
    title: `▶ ${title}`, body, footer: el("div"), size: "xl", extraClass: "player-modal",
    closeButton: true,
    onClose: () => {
      closed = true;
      probeController?.abort();
      stopPlayback();
    },
  });
  m.footer.append(mBtn("Stop & Close", "btn-outline-secondary", m.close, "bi-stop-circle"));
  soundBtn = mBtn("Enable sound", "btn-outline-secondary", () => {
    video.muted = false;
    video.volume = 1;
    soundBtn.disabled = true;
    pushDiag("sound enabled by user gesture");
  }, "bi-volume-up-fill");
  m.footer.append(soundBtn);

  m.footer.append(mBtn("Replay", "btn-outline-primary", () => {
    m.close(); playInModal(url, title);
  }, "bi-arrow-clockwise"));
  const probeMatch = /^\/(preview|preview-play)\/(live|vod|series|episode|local)\/(\d+)\.ts(?:\?|$)/.exec(url);
  if (probeMatch) {
    const scope = probeMatch[1] === "preview" ? "source" : "playlist";
    const probeBox = el("div", { class: "mt-2 border-top pt-2", hidden: "", "aria-live": "polite" });
    body.append(probeBox);
    const probeButton = mBtn("Probe stream", "btn-outline-info", async () => {
      if (probeButton.disabled || closed) return;
      probeButton.disabled = true;
      stopPlayback();
      status.textContent = "Playback stopped for probing. Use Replay to resume.";
      probeBox.hidden = false;
      probeBox.textContent = "Probing source stream…";
      probeBox.setAttribute("aria-busy", "true");
      probeController = new AbortController();
      try {
        const d = await api(`/api/playlist/probe?scope=${scope}&kind=${probeMatch[2]}&id=${probeMatch[3]}`, {signal:probeController.signal});
        if (closed) return;
        probeBox.innerHTML = `<div class="muted-label">Source technical information (before FFmpeg)${scope === "playlist" ? " · primary source" : ""}</div>` +
          (d.source ? `<div class="small mb-1">${esc(d.source)}</div>` : "") + probeHtml(d.probe);
      } catch (e) {
        if (!closed) probeBox.textContent = "Probe failed: " + e.message + ". You can retry.";
      } finally {
        if (!closed) { probeButton.disabled = false; probeBox.setAttribute("aria-busy", "false"); }
      }
    }, "bi-info-circle");
    m.footer.append(probeButton);
    const hint = el("span", {"data-help":"Stream probe"},
      "Stops this preview to release its stream connection, then probes the original source before FFmpeg processing. Reports all available codec, resolution, frame rate, bitrate, audio, subtitle and container metadata. Playlist probes inspect the primary source, which may differ from a fallback used during playback. Live streams may not report every field. Use Replay to resume. A busy single-connection portal may require a moment before retrying.");
    m.footer.append(hint);
  }

  // The preview runs the source through an FFmpeg template, same as the real
  // output. If it stays black on copy (HEVC / AC3 / anything MediaSource cannot
  // take), retry through a transcode template instead of guessing.
  if (/^\/preview\//.test(url)) {
    const sel = el("select", { class: "form-select form-select-sm w-auto d-inline-block ms-2" });
    sel.append(el("option", { value: "" }, "default template"));
    const go = el("button", { class: "btn btn-sm btn-outline-primary ms-1" }, "Retry with");
    m.footer.append(el("span", { class: "small text-muted ms-2" }, "FFmpeg template:"), sel, go);
    api("/api/ffmpeg").then(r => (r.items || []).forEach(t =>
      sel.append(el("option", { value: t.id }, `${t.name}${t.is_default ? " (default)" : ""}`))
    )).catch(() => sel.remove());
    go.addEventListener("click", () => {
      const id = sel.value;
      const next = id ? `${url.split("?")[0]}?tpl=${id}` : url.split("?")[0];
      m.close();
      playInModal(next, title);
    });
  }

  const isHls = /\.m3u8(\?|$)/i.test(url);
  const haveHls = !!(window.Hls && window.Hls.isSupported && window.Hls.isSupported());
  const haveTs = !!(window.mpegts && window.mpegts.isSupported && window.mpegts.isSupported());

  if (isHls && !haveHls)
    { fail("hls.js is unavailable or this browser has no MediaSource support.",
                "Use Chrome/Edge/Firefox, or switch the output format to MPEG-TS in FFmpeg → template."); return m; }
  if (!isHls && !haveTs)
    { fail("mpegts.js is unavailable or this browser has no MediaSource support.",
                "The player library is served from /static/vendor/ - a blocked or stale " +
                "static mount leaves the popup with nothing to decode the transport stream."); return m; }

  // surface the player library's INTERNAL log (probe result, MSE init,
  // appendBuffer errors, loader errors) - this is what says exactly why a
  // given browser refuses a stream that the server is demonstrably sending.
  if (window.mpegts && mpegts.LoggingControl && mpegts.LoggingControl.addLogListener) {
    if (!window.__spmMpegtsLogHook) {
      window.__spmMpegtsLogHook = true;
      mpegts.LoggingControl.addLogListener((...parts) => {
        if (window.__spmActiveDiag) window.__spmActiveDiag(parts.filter(x => x != null).join(": "));
      });
    }
    window.__spmActiveDiag = pushDiag;
  }

  video.addEventListener("playing", () => { pushDiag("event: playing"); ok(`▶ playing · ${url}`); });
  video.addEventListener("error", () => {
    pushDiag(`event: video.error code=${video.error?.code} ${video.error?.message || ""}`);
    fail(`the <video> element rejected the stream (code ${video.error?.code ?? "?"}).`,
      "Usually an unsupported codec after transmux - see the stream probe for the " +
      "video/audio codec, or pick a transcode template instead of copy.");
  });
  video.addEventListener("stalled", () => pushDiag("event: stalled"));
  video.addEventListener("waiting", () => pushDiag("event: waiting (buffer empty)"));

  // 1s health ticker: proves (or refutes) data flow + decode in THIS browser
  ticker = setInterval(() => {
    let bufEnd = 0;
    try { bufEnd = video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0; } catch {}
    status.textContent =
      `${settled ? "▶" : "…"} rt${video.readyState} t=${video.currentTime.toFixed(1)}s ` +
      `buf=${bufEnd.toFixed(1)}s ${received ? `rx=${(received / 1024).toFixed(0)} KB ` : ""}${lastStats} · ${url}`;
    if (settled) return;
    if (!inputSeen && !bufEnd && performance.now() - t0 > 10000) {
      settled = true;
      fail("no stream bytes reached the browser in 10 s.",
        "The popup DID open the stream (the server log shows it) - something between " +
        "the server and this tab dropped it: a reverse proxy buffering the response, " +
        "or a browser shield. Check Logs → stream; try outside Brave/iframes.");
    } else if (inputSeen && video.readyState <= 1 && performance.now() - t0 > 12000) {
      settled = true;
      fail("stream data arrives but the browser never starts decoding it.",
        "Read the mpegts.js lines below: a codec MediaSource cannot take (HEVC/MPEG-2/AC3) " +
        "or an MSE error. Retry with a transcode template (H.264 + AAC).");
    }
  }, 1000);
  const t0 = performance.now();

  try {
    if (isHls) {
      engine = new Hls({ enableWorker: true, lowLatencyMode: true });
      engine.on(Hls.Events.ERROR, (_e, d) => {
        pushDiag(`hls ${d.type}/${d.details}${d.fatal ? " FATAL" : ""}`);
        if (d.fatal) fail(`HLS ${d.type}: ${d.details}`,
                          "A fatal HLS error means the variant playlist or segments are not " +
                          "reaching the player - check the proxy log for the stream.");
      });
      engine.on(Hls.Events.FRAG_LOADED, (_e, data) => { inputSeen = true; received += data?.stats?.total || data?.payload?.byteLength || 0; });
      engine.on(Hls.Events.MANIFEST_PARSED, () => {
        video.play().catch(() => say("Autoplay was blocked — press ▶ on the player.", "alert-secondary"));
      });
      engine.loadSource(url); engine.attachMedia(video);
    } else {
      engine = mpegts.createPlayer({ type: "mpegts", isLive: true, url },
                                   { enableStashBuffer: false, stashInitialSize: 384 });
      engine.on(mpegts.Events.ERROR, (type, detail, info) => {
        pushDiag(`mpegts ERROR ${type}/${detail || ""} ${info?.msg || ""}`);
        fail(`${type}${detail ? " / " + detail : ""}${info && info.msg ? ": " + info.msg : ""}`,
          "mpegts.js transmuxes MPEG-TS into fMP4 for MediaSource, which only accepts " +
          "H.264 video and AAC audio. A HEVC, MPEG-2 video or AC3/E-AC3/DTS audio stream " +
          "will stay black on copy - use a transcode template that converts it.");
      });
      engine.on(mpegts.Events.MEDIA_INFO, (mi) => {
        inputSeen = true;
        pushDiag(`media info: video=${mi.videoCodec} audio=${mi.audioCodec} ` +
                 `${mi.width}x${mi.height}`);
        const v = mi.videoCodec || "", a = mi.audioCodec || "";
        const bad = [v, a].filter(c => c && !browserPlayable(c));
        if (bad.length) say(`Codec ${bad.map(esc).join(", ")} is not playable through MediaSource. ` +
          "Switch this item to a transcode template (H.264 + AAC) instead of copy.", "alert-danger");
      });
      engine.on(mpegts.Events.STATISTICS_INFO, (s) => {
        received = (s.receivedBytes ?? s.totalBytes ?? received);
        const kbps = s.speed ?? s.speedKBps ?? 0;
        inputSeen ||= received > 0 || kbps > 0;
        lastStats = kbps ? `@${kbps.toFixed(0)} KB/s` : "";
      });
      engine.on(mpegts.Events.LOADING_COMPLETE, () => pushDiag("loader: server ended the stream"));
      engine.attachMediaElement(video); engine.load();
      engine.play().catch((e) => {
        pushDiag(`play() rejected: ${e}`);
        say("Autoplay was blocked - <b>press ▶ on the player</b> (the stream keeps loading).", "alert-secondary");
      });
    }
  } catch (e) {
    fail(`player setup threw ${e && e.message ? e.message : e}.`, "");
  }
  pushDiag(`player started: ${isHls ? "hls.js" : "mpegts.js"} · ${navigator.userAgent.slice(0, 90)}`);
  return m;
}
