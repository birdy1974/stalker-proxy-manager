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

/* ---------------------------------------------------------------- API */
async function api(path, { method = "GET", body, raw = false } = {}) {
  const opts = { method, headers: {} };
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
  const close = () => { modal.hide(); setTimeout(() => wrap.remove(), 250); if (onClose) onClose(); };
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
  el("button", { class: `btn btn-sm ${cls}`, type: "button", onclick: fn, html: (icon ? `<i class="bi ${icon} me-1"></i>` : "") + esc(label) });

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
    this.host.append(this.toolbar, this.spinner, this.tableHost, this.pager);
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
  async reload() {
    this.spinner.style.display = "block";
    try {
      const data = await this.opts.server(this.params());
      this.items = data.items || []; this.total = data.total || 0;
      this.lastData = data;
      this._render();
    } catch (e) { /* toast already shown */ }
    this.spinner.style.display = "none";
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
      if (o.dnd) { tr.draggable = true; this._bindDnd(tr, row); }
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
  _bindDnd(tr, row) {
    const g = el("td", {}, el("i", { class: "bi bi-grip-vertical row-drag" }));
    tr.insertBefore(g, tr.children[1] || null);
    tr.addEventListener("dragstart", (e) => { this._dragRow = row; tr.classList.add("dragging"); });
    tr.addEventListener("dragend", () => { tr.classList.remove("dragging"); $$("tr", this.tbody).forEach(x => x.classList.remove("drop-highlight")); });
    tr.addEventListener("dragover", (e) => { e.preventDefault(); tr.classList.add("drop-highlight"); });
    tr.addEventListener("dragleave", () => tr.classList.remove("drop-highlight"));
    tr.addEventListener("drop", async (e) => {
      e.preventDefault(); tr.classList.remove("drop-highlight");
      const from = this._dragRow; if (!from || from.id === row.id) return;
      const ids = this.items.map(x => x.id);
      ids.splice(ids.indexOf(from.id), 1);
      ids.splice(ids.indexOf(row.id) + (ids.indexOf(from.id) > ids.indexOf(row.id) ? 1 : 0), 0, from.id);
      await this.opts.dnd.onReorder(ids);
      this.reload();
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
  const v = pr.video, aud = (pr.audio || []).map(a =>
    [esc(a.codec), a.rate_hz ? `${a.rate_hz / 1000} kHz` : "", esc(a.channels || ""),
     a.kbps ? `@ ${a.kbps} kbps` : ""].filter(Boolean).join(" ")).join(" · ");
  return `<table class="table table-sm mb-0 small">
    ${v ? `<tr><td class="muted-label" style="width:120px">Video</td><td>${v.width}×${v.height}${v.ratio ? ` (${v.ratio})` : ""} · ${esc(v.codec)}${v.kbps ? ` @ ${v.kbps} kbps` : ""}${v.fps ? ` · ${v.fps} fps` : ""}</td></tr>` : ""}
    ${aud ? `<tr><td class="muted-label">Audio</td><td>${aud}</td></tr>` : ""}
    ${pr.duration_s ? `<tr><td class="muted-label">Duration</td><td>${fmtDur(pr.duration_s)}</td></tr>` : ""}
    ${pr.overall_kbps ? `<tr><td class="muted-label">Overall</td><td>${pr.overall_kbps} kbps</td></tr>` : ""}
  </table>`;
};
const tmdbHtml = (t) => !t
  ? `<span class="text-muted small">No TMDB hit (set the TMDB API key in Settings → TMDB for enrichment).</span>`
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
const MSE_OK_VIDEO = /^(avc1|h264|avc)$/i;
const MSE_OK_AUDIO = /^(mp4a|aac)$/i;
const browserPlayable = (codec) => !codec || MSE_OK_VIDEO.test(codec) || MSE_OK_AUDIO.test(codec);

function playInModal(url, title) {
  /* Muted + playsinline autoplay: Chromium (Chrome/Brave/Edge) and every
     iframe-embedded player block UNMUTED autoplay, which rendered this popup
     as a black box "that does not get any input" even while the stream was
     flowing. Sound comes back via the sound button (a user gesture, so the
     browser allows it). */
  const video = el("video", { controls: "", autoplay: "", muted: "", playsinline: "",
                              class: "w-100", style: "background:#000;max-height:65vh" });
  const status = el("div", { class: "small text-muted mt-1" }, `Source: ${url}`);
  const diag = el("div", { class: "small mt-2 p-2 bg-dark text-light mono",
                           style: "max-height:180px;overflow:auto;white-space:pre-wrap;border-radius:4px" });
  const body = el("div", {}, video, status, diag);
  let engine = null, settled = false, ticker = null, received = 0, lastStats = "";
  let soundBtn = null;

  const diagLines = [];
  const pushDiag = (line) => {
    const t = new Date().toLocaleTimeString();
    diagLines.push(`[${t}] ${line}`);
    if (diagLines.length > 40) diagLines.splice(0, diagLines.length - 40);
    diag.textContent = diagLines.join("\n");
    diag.scrollTop = diag.scrollHeight;
  };
  const say = (html, cls) => {
    diag.className = `small mt-2 alert ${cls || "alert-warning"} mb-0 py-2`;
    diag.innerHTML = html;
  };
  const backToLog = () => {
    diag.className = "small mt-2 p-2 bg-dark text-light mono";
    diag.style.cssText = "max-height:180px;overflow:auto;white-space:pre-wrap;border-radius:4px";
    diag.textContent = diagLines.join("\n");
  };
  const ok = (txt) => { if (!settled) { settled = true; status.textContent = txt; } };
  const fail = (why, hint) => {
    pushDiag("FAIL: " + why);
    say(`<div><b>Not playing:</b> ${esc(why)}</div>` +
        (hint ? `<div class="mt-1 text-muted">${hint}</div>` : "") +
        `<div class="mt-1 text-muted">Technical detail below - scroll the log.</div>`, "alert-warning");
  };

  const m = openModal({
    title: `▶ ${title}`, body, footer: el("div"), size: "xl", extraClass: "player-modal",
    closeButton: true,
    onClose: () => {
      clearInterval(ticker);
      try { engine?.destroy(); } catch {}
      try { video.pause(); video.removeAttribute("src"); video.load(); } catch {}
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
    return fail("hls.js is unavailable or this browser has no MediaSource support.",
                "Use Chrome/Edge/Firefox, or switch the output format to MPEG-TS in FFmpeg → template.");
  if (!isHls && !haveTs)
    return fail("mpegts.js is unavailable or this browser has no MediaSource support.",
                "The player library is served from /static/vendor/ - a blocked or stale " +
                "static mount leaves the popup with nothing to decode the transport stream.");

  // surface the player library's INTERNAL log (probe result, MSE init,
  // appendBuffer errors, loader errors) - this is what says exactly why a
  // given browser refuses a stream that the server is demonstrably sending.
  if (window.mpegts && mpegts.LoggingControl && mpegts.LoggingControl.addLogListener) {
    if (!window.__spmMpegtsLogHook) {
      window.__spmMpegtsLogHook = true;
      mpegts.LoggingControl.addLogListener((tag, type, msg) => {
        if (window.__spmActiveDiag) window.__spmActiveDiag(`${type}: ${msg}`);
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
      `buf=${bufEnd.toFixed(1)}s rx=${(received / 1024).toFixed(0)} KB ${lastStats} · ${url}`;
    if (settled) return;
    if (received === 0 && performance.now() - t0 > 10000) {
      settled = true;
      fail("no stream bytes reached the browser in 10 s.",
        "The popup DID open the stream (the server log shows it) - something between " +
        "the server and this tab dropped it: a reverse proxy buffering the response, " +
        "or a browser shield. Check Logs → stream; try outside Brave/iframes.");
    } else if (received > 0 && video.readyState <= 1 && performance.now() - t0 > 12000) {
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
      engine.on(Hls.Events.MANIFEST_PARSED, () => ok(`▶ playing · ${url}`));
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
        pushDiag(`media info: video=${mi.videoCodec} audio=${mi.audioCodec} ` +
                 `${mi.width}x${mi.height}`);
        const v = mi.videoCodec || "", a = mi.audioCodec || "";
        const bad = [v, a].filter(c => c && !browserPlayable(c));
        if (bad.length) say(`Codec ${bad.join(", ")} is not playable through MediaSource. ` +
          "Switch this item to a transcode template (H.264 + AAC) instead of copy.", "alert-danger");
      });
      engine.on(mpegts.Events.STATISTICS_INFO, (s) => {
        received = (s.receivedBytes ?? s.totalBytes ?? received);
        const kbps = s.speed ?? s.speedKBps ?? 0;
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
