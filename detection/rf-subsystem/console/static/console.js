"use strict";
/*
 * WBT Console shell: sign-in, the site menu (switch, new, rename, retention,
 * export, clear, wipe), Live/Setup modes, and the Nodes setup tool.
 *
 * Site data lives on the server, one workspace per building; this page only
 * asks for one change at a time. Switching site reloads the page so nothing
 * from the previous building lingers in memory.
 */

const C = { role: null, authOn: false, mode: store.get("mode", "live"), sites: [], menuOpen: false, wipeArm: null, clearArm: false };

// ---------- sign-in ----------
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const res = await _fetch(...args);
  const url = String(args[0] && args[0].url || args[0]);
  if (res.status === 401 && url.includes("api/") && !url.includes("api/login")) showLogin();
  return res;
};

function showLogin() {
  if (!$("login").hidden) return;
  $("login").hidden = false;
  $("login-err").hidden = true;
  $("login-pw").value = "";
  $("login-pw").focus();
}

$("login-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const res = await _fetch("api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("login-pw").value }) });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) { $("login-err").textContent = out.error || "Sign-in failed"; $("login-err").hidden = false; return; }
  location.reload();
};

$("logout").onclick = async () => {
  await api("api/logout", {});
  location.reload();
};

async function loadSession() {
  try {
    const s = await (await _fetch("api/session", { cache: "no-store" })).json();
    C.authOn = s.enabled; C.role = s.role;
    if (s.enabled && !s.role) showLogin();
  } catch { /* server unreachable: the status pill says so */ }
  $("auth-off").hidden = C.authOn;
  $("user-box").hidden = !(C.authOn && C.role);
  $("user-role").textContent = C.role || "";
}
const isAdmin = () => C.role === "admin";

async function api(path, body) {
  const res = await fetch(path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out.error || res.status);
  return out;
}

// Called by app.js on every poll. Returns true when the page is reloading.
function siteSync(data) {
  const site = data.site;
  if (!site) return false;
  $("site-name").textContent = site.name;
  document.title = `${site.name} · WBT Console`;
  if (S.siteId && site.id !== S.siteId) { location.reload(); return true; }
  S.siteId = site.id;
  if (site.version !== S.siteVersion && !S.drag && !S.nodesBusy) {
    S.siteVersion = site.version;
    const nodes = data.layout.nodes;
    if (JSON.stringify(nodes) !== JSON.stringify(S.nodes)) {   // nodes moved (here or on another page)
      S.nodes = structuredClone(nodes);
      S.nodeOrder = Object.keys(nodes);
      buildGrid();
    }
  }
  if (S.nodesMode.on) renderNodesBar();
  return false;
}

// ---------- modes ----------
function setMode(mode) {
  C.mode = mode;
  store.set("mode", mode);
  document.querySelectorAll("#mode [data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  $("setup-bar").hidden = mode !== "setup";
  if (mode !== "setup") setTool(null);
}
document.querySelectorAll("#mode [data-mode]").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

// One setup tool at a time: turn the others off before a tool's own handler runs.
function setTool(tool) {
  if (tool !== "walls" && S.wallsMode.on) wallsToggle();
  if (tool !== "calibrate" && S.calib.on) { S.calib.on = false; renderLiveCal(); refresh(); }
  if (tool !== "nodes" && S.nodesMode.on) nodesToggle(false);
}
$("setup-bar").addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  setTool({ "walls-btn": "walls", calibrate: "calibrate", "nodes-btn": "nodes" }[b.id] || null);
}, true);

// ---------- nodes tool ----------
function nodesToggle(on) {
  S.nodesMode.on = on === undefined ? !S.nodesMode.on : on;
  S.nodesMode.placing = null;
  $("nodes-btn").classList.toggle("active", S.nodesMode.on);
  $("nodes-bar").hidden = !S.nodesMode.on;
  if (S.nodesMode.on) renderNodesBar();
  S.needsDraw = true;
}
$("nodes-btn").onclick = () => nodesToggle();
$("nodes-done").onclick = () => nodesToggle(false);

function renderNodesBar() {
  const placed = S.nodeOrder;
  const seen = new Set((S.data && S.data.source_nodes) || []);
  const unplaced = [...seen].filter((n) => !S.nodes[n]);
  const chips = placed.map((n) => `<span class="nchip" title="${seen.size && !seen.has(n) ? "Kismet has no capture source for this node" : ""}">` +
    `<i style="background:${nodeColor(n)}"></i>${escapeHtml(n)}${seen.size && !seen.has(n) ? " ⚠" : ""}` +
    `<button data-rm="${escapeHtml(n)}" title="Remove from this site" ${placed.length <= 2 ? "disabled" : ""}>×</button></span>`)
    .concat(unplaced.map((n) => `<button class="btn small ${S.nodesMode.placing === n ? "active" : ""}" data-place="${escapeHtml(n)}">+ ${escapeHtml(n)}</button>`));
  $("nodes-chips").innerHTML = chips.join("");
  $("nodes-chips").querySelectorAll("[data-rm]").forEach((b) => { b.onclick = () => nodesSave(b.dataset.rm, null); });
  $("nodes-chips").querySelectorAll("[data-place]").forEach((b) => {
    b.onclick = () => { S.nodesMode.placing = S.nodesMode.placing === b.dataset.place ? null : b.dataset.place; renderNodesBar(); };
  });
  $("nodes-hint").textContent = S.nodesMode.placing ? `Click the map where ${S.nodesMode.placing} is.` : "";
}

function nodesClick(x, y) {
  const id = S.nodesMode.placing;
  if (!id) return;
  S.nodesMode.placing = null;
  nodesSave(id, [Math.round(x * 2) / 2, Math.round(y * 2) / 2]);
}

async function nodesSave(id, xy) {
  S.nodesBusy = (S.nodesBusy || 0) + 1;
  try {
    const out = await api("api/site/nodes", xy ? { node: id, x: xy[0], y: xy[1] } : { node: id, remove: true });
    S.nodes = out.nodes;
    S.nodeOrder = Object.keys(out.nodes);
    S.siteVersion = out.version;
    $("nodes-hint").textContent = "";
  } catch (err) {
    $("nodes-hint").textContent = `Failed: ${err.message}`;
    S.siteVersion = null;   // re-adopt the server's layout on the next poll
  } finally {
    S.nodesBusy--;
  }
  buildGrid();
  refresh();
  if (S.nodesMode.on) renderNodesBar();
  calAutoApply();   // node positions change every model candidate
}

// ---------- site menu ----------
$("site-btn").onclick = (ev) => { ev.stopPropagation(); toggleMenu(); };
// Clicks inside the menu never close it (its buttons re-render it, detaching the clicked element).
$("site-menu").addEventListener("click", (ev) => ev.stopPropagation());
document.addEventListener("click", () => { if (C.menuOpen) toggleMenu(false); });
window.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && C.menuOpen) toggleMenu(false); });

async function toggleMenu(open) {
  C.menuOpen = open === undefined ? !C.menuOpen : open;
  $("site-menu").hidden = !C.menuOpen;
  $("site-btn").setAttribute("aria-expanded", C.menuOpen);
  C.wipeArm = null; C.clearArm = false;
  if (C.menuOpen) await loadSites();
}

async function loadSites() {
  try { C.sites = (await api("api/sites")).sites; } catch (err) { C.sites = []; }
  renderMenu();
}

function fmtBytes(n) { return n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n > 1e3 ? `${(n / 1e3).toFixed(0)} kB` : `${n} B`; }

function renderMenu() {
  const active = C.sites.find((s) => s.active);
  const others = C.sites.filter((s) => !s.active);
  const rows = others.map((s) => `<div class="mrow">
      <button class="mlink" data-go="${s.id}"><b>${escapeHtml(s.name)}</b><span class="muted">${s.spots} spots · ${s.walls} walls</span></button>
      ${isAdmin() ? `<button class="btn small danger" data-wipe="${s.id}">Wipe</button>` : ""}
    </div>${C.wipeArm === s.id ? `<div class="mrow"><input id="wipe-confirm" placeholder="Type “${escapeHtml(s.name)}” to wipe"><button class="btn small danger" id="wipe-go">Wipe</button></div>` : ""}`).join("");
  $("site-menu").innerHTML = `
    ${active ? `<div class="msec">
      <div class="mhead"><input id="site-rename" value="${escapeHtml(active.name)}" aria-label="Site name"></div>
      <div class="muted msub">${active.nodes} nodes · ${active.spots} spots · ${active.walls} walls · history ${fmtBytes(active.history_bytes)}</div>
      ${isAdmin() ? `<div class="mrow"><span>Keep history</span><select id="site-ret">${[1, 2, 3, 7, 14, 30].map((d) => `<option value="${d}" ${d === active.retention_days ? "selected" : ""}>${d} day${d > 1 ? "s" : ""}</option>`).join("")}</select></div>
      <div class="mrow"><a class="btn small" href="api/sites/${active.id}/export">Export</a><button class="btn small" id="site-clear">${C.clearArm ? "Confirm clear" : "Clear history"}</button></div>` : ""}
    </div>` : ""}
    ${others.length ? `<div class="msec"><div class="mlabel">Other sites</div>${rows}</div>` : ""}
    <div class="msec"><div class="mrow"><input id="site-new" placeholder="New site name" maxlength="60"><button class="btn small" id="site-create">Create</button></div></div>
    <div id="site-err" class="cal-warn" hidden></div>`;
  const err = (e) => { $("site-err").textContent = e.message || String(e); $("site-err").hidden = false; };
  $("site-menu").querySelectorAll("[data-go]").forEach((b) => {
    b.onclick = () => api(`api/sites/${b.dataset.go}/activate`, {}).then(() => location.reload()).catch(err);
  });
  $("site-menu").querySelectorAll("[data-wipe]").forEach((b) => {
    b.onclick = () => { C.wipeArm = C.wipeArm === b.dataset.wipe ? null : b.dataset.wipe; renderMenu(); if (C.wipeArm) $("wipe-confirm").focus(); };
  });
  if ($("wipe-go")) {
    $("wipe-go").onclick = () => api(`api/sites/${C.wipeArm}/wipe`, { confirm: $("wipe-confirm").value })
      .then((out) => { C.sites = out.sites; C.wipeArm = null; renderMenu(); }).catch(err);
  }
  if ($("site-rename")) {
    $("site-rename").onchange = () => api(`api/sites/${active.id}/rename`, { name: $("site-rename").value }).then(loadSites).catch(err);
  }
  if ($("site-ret")) $("site-ret").onchange = () => api(`api/sites/${active.id}/retention`, { days: +$("site-ret").value }).then(loadSites).catch(err);
  if ($("site-clear")) {
    $("site-clear").onclick = () => {
      if (!C.clearArm) { C.clearArm = true; renderMenu(); setTimeout(() => { if (C.clearArm) { C.clearArm = false; if (C.menuOpen) renderMenu(); } }, 4000); return; }
      C.clearArm = false;
      api(`api/sites/${active.id}/clear`, {}).then(loadSites).catch(err);
    };
  }
  const create = () => {
    const name = $("site-new").value.trim();
    if (name) api("api/sites", { name }).then(() => location.reload()).catch(err);
  };
  $("site-create").onclick = create;
  $("site-new").onkeydown = (ev) => { if (ev.key === "Enter") create(); };
}

loadSession();
setMode(C.mode);
