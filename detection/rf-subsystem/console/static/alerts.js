"use strict";
/*
 * Marks, alert areas and alerts (server: console/alerting.py).
 *
 * - Device card: mark the selected device Friendly or Priority, for this site
 *   or the whole team (all sites). Friendly devices are dimmed on the map (or
 *   hidden) and never alerted on; priority devices get a red ring and alert
 *   wherever they are heard.
 * - Setup -> Areas: click the corners of an area, click the first corner to
 *   close it. Devices placed by 3+ nodes entering an area (or lingering in it)
 *   raise alerts.
 * - Alerts card: newest first; click one to select its device; acknowledge.
 */

const AL = {
  mode: { on: false, pts: [], cursor: null },
  hideFriendly: store.get("hideFriendly", false),
  markScope: store.get("markScope", "site"),
  seen: null,              // alert ids already shown (new ones flash the pill)
  acking: new Set(),       // alert ids acknowledged here, hidden before the server confirms
};
const markOf = (id) => (S.data && S.data.marks && S.data.marks[id]) || null;
const isFriendly = (id) => !!S.selfRadio[id] || (markOf(id) || {}).kind === "friendly";
const RULE_TEXT = {
  area_entry: (a) => `entered ${a.area ? a.area.name : "an area"}`,
  linger: (a) => `${a.area ? `in ${a.area.name}` : "here"} for ${Math.round(((a.context || {}).dwell_s || 0) / 60)} min`,
  priority_heard: () => "priority device heard",
  node_offline: (a) => `${(a.context || {}).node_id} offline`,
};

async function alertsApi(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}

function alertWho(a) {
  if (!a.emitter) return "";
  const dev = S.devById && S.devById.get(a.emitter.id);
  return a.emitter.label || (dev ? displayName(dev) : a.emitter.name || a.emitter.id);
}

function renderAlerts() {
  const v = S.data && S.data.alerts;
  if (!v) return;
  const pill = $("alert-pill");
  const ids = new Set(v.recent.map((a) => a.id));
  if (AL.seen && v.recent.some((a) => !AL.seen.has(a.id) && !a.acked)) {
    pill.classList.remove("flash"); void pill.offsetWidth; pill.classList.add("flash");
  }
  AL.seen = ids;
  const open = v.recent.filter((a) => !a.acked && !AL.acking.has(a.id));   // acknowledged alerts leave the list
  $("alerts-card").hidden = !open.length;
  pill.hidden = !open.length;
  pill.textContent = `${open.length} alert${open.length === 1 ? "" : "s"}`;
  const key = JSON.stringify(open.slice(0, 40).map((a) => [a.id, alertWho(a)]));
  if (key === AL.listKey) return;      // unchanged: keep the rows (re-rendering would eat clicks)
  AL.listKey = key;
  $("alert-list").innerHTML = open.slice(0, 40).map((a) => {
    const t = new Date(a.t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const what = (RULE_TEXT[a.rule_id] || (() => a.rule_id))(a);
    const who = alertWho(a);
    return `<li class="al ${a.severity}${a.acked ? " acked" : ""}" data-dev="${a.emitter ? escapeHtml(a.emitter.id) : ""}">` +
      `<span class="sev"></span><span class="txt">${who ? `<b>${escapeHtml(who)}</b> ` : ""}${escapeHtml(what)}</span><span class="muted">${t}</span></li>`;
  }).join("");
  $("alert-list").querySelectorAll("li[data-dev]").forEach((li) => {
    li.onclick = () => { if (li.dataset.dev) select(li.dataset.dev); };
  });
}

$("alerts-ack").onclick = async () => {
  // hide them at once; a poll answered before the server recorded the ack must not bring them back
  const ids = ((S.data && S.data.alerts && S.data.alerts.recent) || []).filter((a) => !a.acked).map((a) => a.id);
  ids.forEach((i) => AL.acking.add(i));
  renderAlerts();
  try { const out = await alertsApi("api/alerts/ack", { ids }); if (S.data) S.data.alerts = out.alerts; }
  catch { ids.forEach((i) => AL.acking.delete(i)); }
  renderAlerts();
};
$("alert-pill").onclick = () => $("alerts-card").scrollIntoView({ behavior: "smooth" });

// ---------- marks (device card) ----------
function renderMarkButtons(dev) {
  const box = $("sel-mark");
  if (!dev || S.selfRadio[dev.id]) { box.innerHTML = ""; AL.markKey = null; return; }
  const m = markOf(dev.id);
  const key = JSON.stringify([dev.id, m]);
  if (key === AL.markKey && box.innerHTML) return;   // unchanged: keep the buttons (re-rendering would eat clicks)
  AL.markKey = key;
  const btn = (kind, label) => `<button class="btn small ${m && m.kind === kind ? "active" : ""}" data-mark="${kind}">${label}</button>`;
  const lingerOpts = [[0, "no linger alarm"], [300, "linger 5 min"], [900, "linger 15 min"], [1800, "linger 30 min"], [3600, "linger 1 h"]];
  if (m && m.linger_s && !lingerOpts.some(([v]) => v === m.linger_s)) lingerOpts.push([m.linger_s, `linger ${Math.round(m.linger_s / 60)} min`]);
  box.innerHTML = btn("friendly", "Friendly") + btn("priority", "Priority") +
    `<select id="mark-linger" title="Alert when this device stays this long">${lingerOpts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>` +
    `<select id="mark-scope" title="Team marks apply at every site"><option value="site">this site</option><option value="team">team</option></select>` +
    (m ? `<button class="btn small" data-mark="">Clear</button>` : "");
  $("mark-scope").value = m ? m.scope : AL.markScope;
  $("mark-linger").value = String((m && m.linger_s) || 0);
  $("mark-linger").onchange = async () => {
    const v = +$("mark-linger").value, scope = m ? m.scope : $("mark-scope").value;
    try { const out = await alertsApi("api/marks", { device: dev.id, scope, linger_s: v || null }); S.data.marks = out.marks; refresh(); }
    catch (err) { box.insertAdjacentHTML("beforeend", `<span class="warn">${escapeHtml(err.message)}</span>`); }
  };
  $("mark-scope").onchange = () => { AL.markScope = $("mark-scope").value; store.set("markScope", AL.markScope); };
  box.querySelectorAll("[data-mark]").forEach((b) => {
    b.onclick = async () => {
      const kind = b.dataset.mark, scope = m && !kind ? m.scope : $("mark-scope").value;
      try {
        // a mark button toggles: clicking the active one clears the mark (a linger alarm stays)
        const body = !kind ? { device: dev.id, scope, remove: true }
          : { device: dev.id, scope, kind: m && m.kind === kind ? null : kind };
        const out = await alertsApi("api/marks", body);
        S.data.marks = out.marks;
        refresh();
      } catch (err) { box.insertAdjacentHTML("beforeend", `<span class="warn">${escapeHtml(err.message)}</span>`); }
    };
  });
}

// ---------- areas ----------
function drawAreas(ctx, toS) {
  const areas = (S.data && S.data.areas) || [];
  ctx.save();
  for (const a of areas) {
    ctx.beginPath();
    a.points.forEach(([x, y], i) => { const [sx, sy] = toS(x, y); i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
    ctx.closePath();
    ctx.fillStyle = "rgba(255,93,115,.07)"; ctx.fill();
    ctx.strokeStyle = "rgba(255,93,115,.55)"; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]); ctx.stroke(); ctx.setLineDash([]);
    const cx = a.points.reduce((s, p) => s + p[0], 0) / a.points.length, cy = a.points.reduce((s, p) => s + p[1], 0) / a.points.length;
    const [sx, sy] = toS(cx, cy);
    ctx.font = "11px system-ui"; ctx.fillStyle = "rgba(255,160,170,.9)"; ctx.textAlign = "center"; ctx.fillText(a.name, sx, sy); ctx.textAlign = "left";
  }
  if (AL.mode.on && AL.mode.pts.length) {
    ctx.strokeStyle = "#ff5d73"; ctx.lineWidth = 2; ctx.beginPath();
    AL.mode.pts.forEach(([x, y], i) => { const [sx, sy] = toS(x, y); i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
    if (AL.mode.cursor) ctx.lineTo(...toS(...AL.mode.cursor));
    ctx.stroke();
    for (const [x, y] of AL.mode.pts) { const [sx, sy] = toS(x, y); ctx.fillStyle = "#ff5d73"; ctx.fillRect(sx - 3, sy - 3, 6, 6); }
  }
  ctx.restore();
}

function areasToggle(on) {
  AL.mode.on = on === undefined ? !AL.mode.on : on;
  AL.mode.pts = []; AL.mode.cursor = null;
  $("areas-btn").classList.toggle("active", AL.mode.on);
  $("areas-bar").hidden = !AL.mode.on;
  if (AL.mode.on) renderAreasBar();
  S.needsDraw = true;
}

function renderAreasBar() {
  const areas = (S.data && S.data.areas) || [];
  $("area-chips").innerHTML = areas.map((a) => `<span class="nchip">${escapeHtml(a.name)} · ${Math.round(a.linger_s / 60)} min` +
    `<button data-area-rm="${a.id}" title="Delete">×</button></span>`).join("");
  $("area-chips").querySelectorAll("[data-area-rm]").forEach((b) => {
    b.onclick = async () => {
      try { const out = await alertsApi("api/areas/delete", { ids: [b.dataset.areaRm] }); S.data.areas = out.areas; renderAreasBar(); S.needsDraw = true; }
      catch (err) { $("areas-hint").textContent = `Failed: ${err.message}`; }
    };
  });
  $("areas-hint").textContent = AL.mode.pts.length ? "Click the first corner to finish" : "";
}

async function areasClick(x, y) {
  const p = [Math.round(x * 2) / 2, Math.round(y * 2) / 2], pts = AL.mode.pts;
  const first = pts[0];
  if (pts.length >= 3 && Math.hypot(p[0] - first[0], p[1] - first[1]) * S.view.sc < 12) {
    const name = $("area-name").value.trim() || `Area ${((S.data.areas || []).length) + 1}`;
    try {
      const out = await alertsApi("api/areas/add", { name, points: pts, linger_s: +$("area-linger").value });
      S.data.areas = out.areas;
      $("area-name").value = "";
    } catch (err) { $("areas-hint").textContent = `Failed: ${err.message}`; return; }
    AL.mode.pts = [];
  } else {
    pts.push(p);
  }
  renderAreasBar();
  S.needsDraw = true;
}

$("areas-btn").onclick = () => areasToggle();
$("areas-done").onclick = () => areasToggle(false);
window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && AL.mode.on && AL.mode.pts.length) { AL.mode.pts = []; renderAreasBar(); S.needsDraw = true; }
});
$("hide-friendly").checked = AL.hideFriendly;
$("hide-friendly").onchange = () => { AL.hideFriendly = $("hide-friendly").checked; store.set("hideFriendly", AL.hideFriendly); refresh(); };

