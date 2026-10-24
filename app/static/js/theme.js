/*
 * Color theme for the SPM web GUI: light / dark / auto.
 *
 * Loaded synchronously from <head> (before the stylesheets) so the resolved
 * theme is on <html data-bs-theme="..."> before the first paint - nobody sees
 * a flash of the wrong theme while the page loads. Bootstrap 5.3 themes its
 * components off that attribute; SPM's own dark palette lives in app.css
 * under [data-bs-theme="dark"] (the web player carries its palette inline).
 *
 * The mode is a per-browser preference, not a server setting: it lives in
 * localStorage ("spm-theme"), so the admin console, the login screen and the
 * web player on the same device agree, while another device keeps its own.
 * "auto" (the default when nothing is stored) follows the OS/browser
 * prefers-color-scheme and tracks it live; an explicit light/dark choice
 * wins until changed. Open tabs stay in sync via the storage event.
 *
 * Markup contract:
 *   [data-spm-theme-value="light|dark|auto"]  mode menu items (navbar
 *     dropdown); the active one gets .active + aria-pressed and its
 *     [data-spm-theme-check] element is unhidden.
 *   [data-spm-theme-mode-icon]   <i> whose glyph shows the current MODE
 *     (sun / moon / circle-half for auto).
 *   [data-spm-theme-toggle]      simple flip button (login, web player):
 *     stores the explicit opposite of the resolved theme; its
 *     [data-spm-theme-flip-icon] shows what a click switches TO.
 */
(() => {
  "use strict";

  const KEY = "spm-theme";
  const MODES = ["auto", "light", "dark"];
  const ICONS = { auto: "bi-circle-half", light: "bi-sun-fill", dark: "bi-moon-stars-fill" };
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  let mode = (() => {
    try {
      const v = window.localStorage.getItem(KEY);
      return MODES.includes(v) ? v : "auto";
    } catch (e) {
      return "auto"; // storage blocked (private mode): auto for this page
    }
  })();

  const resolved = () => (mode === "auto" ? (mq && mq.matches ? "dark" : "light") : mode);

  function apply() {
    document.documentElement.setAttribute("data-bs-theme", resolved());
    document.documentElement.setAttribute("data-spm-theme", mode);
  }

  function syncUi() {
    const res = resolved();
    document.querySelectorAll("[data-spm-theme-mode-icon]").forEach((el) => {
      el.className = "bi " + ICONS[mode];
    });
    document.querySelectorAll("[data-spm-theme-flip-icon]").forEach((el) => {
      el.className = "bi " + (res === "dark" ? ICONS.light : ICONS.dark);
    });
    document.querySelectorAll("[data-spm-theme-toggle]").forEach((el) => {
      el.title = "Color theme: " + res + " — switch to " + (res === "dark" ? "light" : "dark");
      el.setAttribute("aria-label", el.title);
    });
    document.querySelectorAll("[data-spm-theme-value]").forEach((el) => {
      const active = el.getAttribute("data-spm-theme-value") === mode;
      el.classList.toggle("active", active);
      el.setAttribute("aria-pressed", active ? "true" : "false");
      el.querySelectorAll("[data-spm-theme-check]").forEach((check) => {
        check.hidden = !active;
      });
    });
    document.querySelectorAll("[data-spm-theme-button]").forEach((el) => {
      el.title = "Color theme: " + (mode === "auto" ? "auto (" + res + ")" : mode);
    });
  }

  function set(next) {
    if (!MODES.includes(next)) return;
    mode = next;
    try {
      window.localStorage.setItem(KEY, next);
    } catch (e) { /* blocked storage: the choice still applies to this page */ }
    apply();
    syncUi();
  }

  apply(); // before first paint: <head> is still parsing when this runs

  if (mq) {
    const onSystemChange = () => {
      if (mode === "auto") {
        apply();
        syncUi();
      }
    };
    if (mq.addEventListener) mq.addEventListener("change", onSystemChange);
    else if (mq.addListener) mq.addListener(onSystemChange);
  }

  window.addEventListener("storage", (e) => {
    if (e.key !== KEY) return;
    mode = MODES.includes(e.newValue) ? e.newValue : "auto";
    apply();
    syncUi();
  });

  function wire() {
    document.querySelectorAll("[data-spm-theme-value]").forEach((el) => {
      el.addEventListener("click", () => set(el.getAttribute("data-spm-theme-value")));
    });
    document.querySelectorAll("[data-spm-theme-toggle]").forEach((el) => {
      el.addEventListener("click", () => set(resolved() === "dark" ? "light" : "dark"));
    });
    syncUi();
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }

  window.SpmTheme = {
    get mode() { return mode; },
    resolved,
    set,
    toggle: () => set(resolved() === "dark" ? "light" : "dark"),
  };
})();
