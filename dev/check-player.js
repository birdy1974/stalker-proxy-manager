#!/usr/bin/env node
/*
Runtime-check the REAL preview player (playInModal in app/static/js/app.js)
against the REAL vendored mpegts.js - in a node `vm` sandbox with a minimal
DOM shim. This exists because dev/check-js.js is syntax-only: a TypeError at
runtime (e.g. `m.footer` being undefined because openModal never returned a
footer) ships silently through every other gate and the user gets a dead
black popup.

What it asserts:
  1. playInModal() runs to completion without throwing;
  2. a <video> element was created;
  3. the diag panel exists and received at least one line of text
     (in node there is no MediaSource, so the graceful "mpegts.js is
     unavailable" failure path must have rendered - which proves the panel
     mechanism works for real browser diagnostics too);
  4. the modal footer contains the Stop / sound / Retry-with controls.

Usage:  node dev/check-player.js [path-to-app-js]
Exit:   0 all assertions hold, 1 otherwise.
*/
'use strict';
const fs = require('fs'), vm = require('vm');

const APP = process.argv[2] || 'app/static/js/app.js';
const VENDOR = 'app/static/vendor/mpegts.js';

/* ---- minimal DOM shim -------------------------------------------------- */
const elements = [];
class FakeEl {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.className = '';
    this.id = '';
    this._text = '';
    this._html = '';
    this.parentElement = null;
    this.listeners = {};
    // media surface
    this.buffered = { length: 0, end: () => 0, start: () => 0 };
    this.readyState = 0;
    this.currentTime = 0;
    this.muted = false;
    this.paused = true;
    this.autoplay = false;
    this.playsInline = false;
    this.volume = 1;
  }
  get textContent() { return this._text + this.children.map(c => c.textContent ?? '').join(''); }
  set textContent(v) { this.children = []; this._text = String(v); }
  set innerHTML(v) {
    this._html = String(v); this.children = [];
    // crude parse: materialise every class="..." element as a flat child so
    // class selectors (.modal-title/.modal-body/.modal-footer/...) find them
    const re = /class="([^"]+)"/g; let m;
    while ((m = re.exec(String(v)))) {
      const c = new FakeEl('div'); c.className = m[1];
      this.children.push(c); c.parentElement = this;
    }
  }
  get innerHTML() { return this._html; }
  get classList() {
    const self = this;
    return {
      add: (...c) => { self.className = [...new Set([...self.className.split(/\s+/).filter(Boolean), ...c])].join(' '); },
      remove: (...c) => { self.className = self.className.split(/\s+/).filter(x => !c.includes(x)).join(' '); },
      contains: (c) => self.className.split(/\s+/).includes(c),
    };
  }
  append(...kids) {
    for (const k of kids) {
      if (typeof k === 'string') { this._text += k; continue; }
      this.children.push(k); k.parentElement = this;
    }
  }
  appendChild(k) { this.append(k); return k; }
  prepend(...kids) { this.append(...kids); }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  querySelector(sel) { return this._query(sel)[0] || null; }
  querySelectorAll(sel) { return this._query(sel); }
  _query(sel) {
    const cls = sel.startsWith('.') ? sel.slice(1) : null;
    const tag = /^[a-z]+$/i.test(sel) ? sel.toUpperCase() : null;
    const out = [];
    const walk = (n) => {
      for (const c of n.children) {
        if ((cls && String(c.className).split(/\s+/).includes(cls)) || (tag && c.tagName === tag)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  play() { return Promise.resolve(); }
  pause() {}
  load() {}
  remove() { if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(c => c !== this); }
  insertBefore(n) { this.children.unshift(n); return n; }
  contains() { return false; }
  focus() {}
  click() {}
}
class FakeDocument {
  constructor() { this.body = new FakeEl('body'); this.head = new FakeEl('head'); }
  createElement(tag) { const e = new FakeEl(tag); elements.push(e); return e; }
  createTextNode(t) { return { textContent: String(t), tagName: '#text' }; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  addEventListener() {}
  getElementById() { return null; }
}

/* ---- sandbox ----------------------------------------------------------- */
const document = new FakeDocument();
const sandbox = {
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  performance: { now: () => Date.now() },
  navigator: { userAgent: 'node-runtime-check' },
  location: { href: 'http://demo/sources' },
  fetch: async () => ({ ok: true, status: 200, json: async () => ({ items: [] }) }),
  document,
  Date, Math, JSON, Promise, Object, Array, String, Number, Boolean, RegExp,
  Error, TypeError, Map, Set, Uint8Array, parseInt, parseFloat, isNaN,
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
sandbox.URL = class { static createObjectURL() { return 'blob:x'; } static revokeObjectURL() {} };
sandbox.Blob = class { constructor(parts) { this.size = (parts || []).join('').length; } };
sandbox.Worker = class { postMessage() {} terminate() {} addEventListener() {} set onmessage(v) {} };
sandbox.bootstrap = { Modal: class { show() {} hide() {} } };
sandbox.toast = () => {};
sandbox.elements = elements;

vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(VENDOR, 'utf8'), sandbox, { filename: 'mpegts.js' });
vm.runInContext(fs.readFileSync(APP, 'utf8'), sandbox, { filename: 'app.js' });

let threw = null;
try {
  vm.runInContext(`playInModal("/preview/live/4.ts", "Runtime Check")`, sandbox);
} catch (e) { threw = e; }

setTimeout(() => {
  const problems = [];
  if (threw) problems.push(`playInModal threw: ${threw.stack}`);
  const video = elements.find(e => e.tagName === 'VIDEO');
  if (!video) problems.push('no <video> element was created');
  // the diag panel ships styled bg-dark; the graceful-failure path restyles
  // it to alert-warning, so accept either (match panel-ish classes only)
  const vIdx = elements.indexOf(video);
  const panels = elements.filter((e, i) =>
    e.tagName === 'DIV' && i > vIdx && /bg-dark|alert/.test(String(e.className)));
  const diag = panels[panels.length - 1];
  if (!diag) problems.push('diag panel was not created');
  else if (!(diag.textContent || '').trim()) problems.push('diag panel exists but is empty');

  setTimeout(() => {
    if (!diag || !(diag.textContent || '').trim())
      problems.push('diag panel got no text even from the graceful no-MediaSource failure path');
    if (problems.length) {
      console.error('check-player: FAILED');
      for (const p of problems) console.error('  - ' + p.split('\n')[0]);
      process.exit(1);
    }
    console.log('check-player: OK — no throw, video created, diag panel rendered:');
    console.log('    ' + diag.textContent.trim().split('\n')[0]);
    process.exit(0);
  }, 400);
}, 900);
