/* Settings data tools. File selection and previews never commit a restore. */
(() => {
  let catalog = null, restoreData = null, restoreReviewed = false, deletePayload = null;
  let restoreRevision = 0, deleteRevision = 0;
  const running = new Set();
  const node = id => document.getElementById(id);
  const post = (path, body) => api(`/api/backup/${path}`, {method: "POST", body});
  const status = (id, message, error = false) => {
    node(id).textContent = message;
    node(id).classList.toggle("text-danger", error);
  };
  const option = (value, label) => `<option value="${esc(value)}">${esc(label)}</option>`;
  const label = name => name.replaceAll("_", " ");
  async function busy(id, fn, output) {
    const button = node(id), text = button.textContent;
    running.add(id);
    button.disabled = true;
    button.textContent = "Working…";
    try { await fn(); }
    catch (err) { status(output, err.message, true); }
    finally { running.delete(id); button.textContent = text; button.disabled = false; sync(); }
  }
  function sync() {
    node("backup-selected").disabled = !document.querySelector(".backup-table:checked");
    node("backup-setting-download").disabled = !node("backup-setting").value;
    node("restore-run").disabled = !restoreReviewed || !node("restore-confirm").checked;
    node("delete-run").disabled = !deletePayload || !node("delete-confirm").checked ||
      node("delete-phrase").value !== (deletePayload.all ? "DELETE EVERYTHING" : "DELETE SELECTED");
    running.forEach(id => node(id).disabled = true);
  }
  function resetRestore() {
    restoreRevision++;
    restoreReviewed = false;
    node("restore-confirm").checked = false;
    node("restore-confirm-panel").hidden = true;
    node("restore-review").textContent = "";
    status("import-result", "");
    sync();
  }
  function resetDelete() {
    deleteRevision++;
    deletePayload = null;
    node("delete-confirm").checked = false;
    node("delete-phrase").value = "";
    node("delete-confirm-panel").hidden = true;
    node("delete-review").textContent = "";
    status("delete-status", "");
    sync();
  }
  function updateDeleteTargets() {
    if (!catalog) return;
    resetDelete();
    const kind = node("delete-scope").value;
    node("delete-target").disabled = kind === "all";
    node("delete-target-label").textContent = kind === "setting" ? "Setting key" : "Table";
    node("delete-target").innerHTML = kind === "all" ? option("all", "All tables and settings") :
      kind === "setting" ? catalog.setting_keys.map(k => option(k, k)).join("") :
      catalog.tables.map(t => option(t.name, `${label(t.name)} (${t.rows.toLocaleString()} rows)`)).join("");
    node("delete-preview").disabled = !node("delete-target").value;
  }
  async function loadCatalog() {
    catalog = await api("/api/backup/catalog");
    node("backup-count").textContent = `· ${catalog.tables.length} tables`;
    node("backup-tables").innerHTML = catalog.tables.map((t, i) => `
      <div class="form-check border-bottom py-1">
        <input class="form-check-input backup-table" type="checkbox" id="backup-table-${i}" value="${esc(t.name)}">
        <label class="form-check-label small d-flex justify-content-between gap-2" for="backup-table-${i}">
          <span>${esc(label(t.name))}${t.runtime_only ? " (reference only)" : ""}</span><span class="text-muted">${t.rows.toLocaleString()}</span>
        </label>
      </div>`).join("");
    node("backup-setting").innerHTML = option("", "Choose a stored setting…") +
      catalog.setting_keys.map(k => option(k, k)).join("");
    updateDeleteTargets();
    sync();
  }
  async function download(payload, name) {
    const data = await post("export", payload);
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url;
    a.download = `spm-${name}-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status("backup-status", `Backup ready: ${Object.keys(data.tables).length} tables (including dependencies). Store it securely.`);
  }
  node("backup-all").onclick = () => busy("backup-all", () => download({}, "full"), "backup-status");
  node("backup-selected").onclick = () => busy("backup-selected", () => download({tables:
    [...document.querySelectorAll(".backup-table:checked")].map(n => n.value)}, "selected"), "backup-status");
  node("backup-setting-download").onclick = () => busy("backup-setting-download", () => download({tables: ["settings"],
    setting_keys: [node("backup-setting").value]}, "setting"), "backup-status");
  node("backup-select-all").onclick = () => { document.querySelectorAll(".backup-table").forEach(n => n.checked = true); sync(); };
  node("backup-select-none").onclick = () => { document.querySelectorAll(".backup-table").forEach(n => n.checked = false); sync(); };
  node("backup-tables").onchange = sync;
  node("backup-setting").onchange = sync;

  function restorePayload() {
    const scope = node("restore-scope").value;
    const payload = {data: restoreData};
    if (scope !== "all") payload.tables = [scope];
    if (scope === "settings" && node("restore-setting").value !== "all")
      payload.setting_keys = [node("restore-setting").value];
    return payload;
  }
  node("import-file").onchange = async event => {
    resetRestore();
    restoreData = null;
    node("restore-options").hidden = true;
    const file = event.target.files[0];
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (data.app !== "stalker-proxy-manager" || ![1, 2].includes(data.version))
        throw new Error("Choose a Stalker Proxy Manager backup (version 1 or 2).");
      if (data.version === 2 && (!data.tables || typeof data.tables !== "object" || Array.isArray(data.tables)))
        throw new Error("The backup has no valid tables.");
      restoreData = data;
      node("restore-scope").innerHTML = option("all", "Everything included in this backup") + (data.version === 2 ?
        Object.keys(data.tables).sort().map(n => option(n, `${label(n)} (${data.tables[n].length} rows)`)).join("") : "");
      node("restore-setting").innerHTML = option("all", "All settings in this backup") +
        (data.tables?.settings || []).map(r => option(r.key, r.key)).join("");
      node("restore-setting").hidden = node("restore-setting-label").hidden = true;
      node("restore-options").hidden = false;
      status("import-result", `${file.name} loaded. Review before restoring.`);
    } catch (err) { status("import-result", err.message, true); }
  };
  node("restore-scope").onchange = () => {
    resetRestore();
    node("restore-setting").hidden = node("restore-setting-label").hidden = node("restore-scope").value !== "settings";
  };
  node("restore-setting").onchange = resetRestore;
  node("restore-confirm").onchange = sync;
  node("restore-preview").onclick = () => busy("restore-preview", async () => {
    resetRestore();
    const revision = restoreRevision;
    if (restoreData.version === 1) {
      node("restore-review").textContent = "Legacy section backup. All included sections will be merged additively. Existing values are preserved. Legacy files may omit tables or contain non-portable references; only version 2 backups provide complete table coverage and a dry-run preview.";
    } else {
      const result = await post("preview", restorePayload());
      if (revision !== restoreRevision) return;
      node("restore-review").innerHTML = `<div class="fw-semibold mb-2">${result.added} to add · ${result.existing} existing · ${result.runtime_skipped} runtime records skipped</div>` +
        `<div class="table-responsive" style="max-height:240px"><table class="table table-sm mb-0"><thead><tr><th>Table</th><th>Add</th><th>Keep existing</th></tr></thead><tbody>` +
        Object.entries(result.tables).map(([name, c]) => `<tr><td>${esc(label(name))}</td><td>${c.added}</td><td>${c.existing}</td></tr>`).join("") + "</tbody></table></div>";
    }
    restoreReviewed = true;
    node("restore-confirm-panel").hidden = false;
    sync();
  }, "import-result");
  node("restore-run").onclick = () => busy("restore-run", async () => {
    if (!restoreReviewed || !node("restore-confirm").checked) return;
    // Disable all restore inputs while committing, so the reviewed selection cannot change.
    ["import-file", "restore-scope", "restore-setting", "restore-preview", "restore-confirm"].forEach(id => node(id).disabled = true);
    try {
      const result = restoreData.version === 1 ? await api("/api/import", {method: "POST", body: {mode: "add_only", data: restoreData}}) :
        await post("restore", {...restorePayload(), confirm_add_only: true});
      resetRestore();
      status("import-result", `Restore complete: ${result.added ?? result.imported} added; ${result.existing ?? result.skipped.length} existing/skipped. No existing values changed.`);
      if (result.skipped?.length) {
        const details = document.createElement("details"), summary = document.createElement("summary"), content = document.createElement("pre");
        summary.textContent = "Skipped records"; content.textContent = result.skipped.join("\n");
        details.append(summary, content); node("import-result").append(details);
      }
      await loadCatalog(); await loadSettings(); await loadEpg(); await loadFavicons();
    } finally {
      ["import-file", "restore-scope", "restore-setting", "restore-preview", "restore-confirm"].forEach(id => node(id).disabled = false);
    }
  }, "import-result");

  node("delete-scope").onchange = updateDeleteTargets;
  node("delete-target").onchange = resetDelete;
  node("delete-confirm").onchange = sync;
  node("delete-phrase").oninput = sync;
  node("delete-preview").onclick = () => busy("delete-preview", async () => {
    resetDelete();
    const revision = deleteRevision;
    const scope = node("delete-scope").value, target = node("delete-target").value;
    const payload = scope === "all" ? {all: true} : scope === "setting" ? {tables: ["settings"], setting_keys: [target]} : {tables: [target]};
    const result = await post("delete-preview", payload);
    if (revision !== deleteRevision) return;
    node("delete-review").innerHTML = `<div class="text-danger fw-semibold mb-2">${result.deleted} rows will be deleted. Related references may also be cleared.</div>` +
      `<div class="table-responsive" style="max-height:240px"><table class="table table-sm mb-0"><thead><tr><th>Table</th><th>Delete</th><th>Rows with references cleared</th></tr></thead><tbody>` +
      result.tables.map(t => `<tr><td>${esc(label(t.name))}${t.selected ? "" : " (related)"}</td><td>${t.deleted}</td><td>${t.references_cleared}</td></tr>`).join("") + "</tbody></table></div>";
    deletePayload = payload;
    node("delete-required").textContent = payload.all ? "DELETE EVERYTHING" : "DELETE SELECTED";
    node("delete-confirm-panel").hidden = false;
    sync();
  }, "delete-status");
  node("delete-run").onclick = () => busy("delete-run", async () => {
    if (!deletePayload || !node("delete-confirm").checked || node("delete-phrase").value !== node("delete-required").textContent) return;
    ["delete-scope", "delete-target", "delete-preview", "delete-phrase", "delete-confirm"].forEach(id => node(id).disabled = true);
    try {
      const result = await post("delete", {...deletePayload, confirm_delete: true, confirmation: node("delete-phrase").value});
      resetDelete(); resetRestore();
      await loadCatalog(); await loadSettings(); await loadEpg(); await loadFavicons();
      status("delete-status", `Deletion complete: ${result.deleted} rows removed. Defaults apply to deleted settings.`);
    } finally {
      ["delete-scope", "delete-target", "delete-preview", "delete-phrase", "delete-confirm"].forEach(id => node(id).disabled = false);
      node("delete-target").disabled = node("delete-scope").value === "all";
    }
  }, "delete-status");
  loadCatalog().catch(err => status("backup-status", err.message, true));
})();
