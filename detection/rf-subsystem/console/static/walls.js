"use strict";
/*
 * Walls on the floor plan. Each wall a straight node-to-point path crosses
 * costs the band's wall loss (dB per wall) in the path-loss model; live
 * calibration fits that loss. Walls are saved on the server (POST /api/walls,
 * journaled).
 *
 * Tools (Walls button): Wall = click start, click end (keeps chaining from the
 * last point; Esc ends the chain). Room = click two opposite corners (four
 * walls). Erase = click a wall. Draft walls (dashed) were sketched from a
 * description and should be corrected to the real floor plan.
 *
 * Each edit is one server operation (add / delete / accept / clear) and the
 * server's reply becomes the page's wall list. A new wall is drawn at once
 * (before the reply). The periodic refresh only replaces the walls when it is
 * at least as new as the page's last edit (calibration_version), so it can
 * never put back an older list.
 */

// Do segments a-b and c-d properly cross? (Touching at an end does not count.)
function segCross(ax, ay, bx, by, cx, cy, dx, dy) {
  const o = (px, py, qx, qy, rx, ry) => (qx - px) * (ry - py) - (qy - py) * (rx - px);
  const d1 = o(cx, cy, dx, dy, ax, ay), d2 = o(cx, cy, dx, dy, bx, by);
  const d3 = o(ax, ay, bx, by, cx, cy), d4 = o(ax, ay, bx, by, dx, dy);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}

function wallsCrossed(ax, ay, bx, by, walls = S.walls) {
  let n = 0;
  for (const w of walls) if (segCross(ax, ay, bx, by, w.x1, w.y1, w.x2, w.y2)) n++;
  return n;
}

// Walls are drawn by eye, not measured. Each wall has an accuracy (sd_ft, 1 sd,
// default 2 ft): its two ends may each be off by that much, sideways and along
// the wall. The expected number of walls a path crosses is averaged over
// WALL_SAMPLES plausible positions per wall, drawn from a generator seeded by
// the wall's coordinates, so every page computes the same numbers.
const WALL_SAMPLES = 16;
const WALL_SD_DEFAULT = 2;
const wallSampleCache = new Map();

function seededRandom(seed) {   // mulberry32
  return () => {
    seed |= 0; seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function wallSamples(w) {
  const sd = w.sd_ft ?? WALL_SD_DEFAULT;
  const key = `${w.x1},${w.y1},${w.x2},${w.y2},${sd}`;
  let out = wallSampleCache.get(key);
  if (out) return out;
  let h = 2166136261;
  for (const ch of key) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  const rnd = seededRandom(h);
  const gauss = () => Math.sqrt(-2 * Math.log(1 - rnd())) * Math.cos(2 * Math.PI * rnd());
  const L = Math.hypot(w.x2 - w.x1, w.y2 - w.y1), ux = (w.x2 - w.x1) / L, uy = (w.y2 - w.y1) / L;
  out = [];
  for (let k = 0; k < WALL_SAMPLES; k++) {
    const s1 = gauss() * sd, s2 = gauss() * sd, e1 = gauss() * sd, e2 = gauss() * sd;  // sideways and along, per end
    out.push([w.x1 - uy * s1 - ux * e1, w.y1 + ux * s1 - uy * e1, w.x2 - uy * s2 + ux * e2, w.y2 + ux * s2 + uy * e2]);
  }
  out.bbox = [Math.min(w.x1, w.x2) - 4 * sd, Math.min(w.y1, w.y2) - 4 * sd, Math.max(w.x1, w.x2) + 4 * sd, Math.max(w.y1, w.y2) + 4 * sd];
  wallSampleCache.set(key, out);
  return out;
}

// Expected (fractional) number of walls crossed by the straight path a-b.
function wallsExpected(ax, ay, bx, by, walls = S.walls) {
  let n = 0;
  const lx = Math.min(ax, bx), hx = Math.max(ax, bx), ly = Math.min(ay, by), hy = Math.max(ay, by);
  for (const w of walls) {
    const sd = w.sd_ft ?? WALL_SD_DEFAULT;
    if (sd <= 0) { if (segCross(ax, ay, bx, by, w.x1, w.y1, w.x2, w.y2)) n++; continue; }
    const smp = wallSamples(w), bb = smp.bbox;
    if (hx < bb[0] || lx > bb[2] || hy < bb[1] || ly > bb[3]) continue;
    let c = 0;
    for (const q of smp) if (segCross(ax, ay, bx, by, q[0], q[1], q[2], q[3])) c++;
    n += c / WALL_SAMPLES;
  }
  return n;
}

const snap = (v) => Math.round(v * 2) / 2;

function wallsSetTool(tool) {
  S.wallsMode.tool = tool;
  S.wallsMode.pending = null;
  document.querySelectorAll("[data-wtool]").forEach((b) => b.classList.toggle("on", b.dataset.wtool === tool));
  $("walls-hint").textContent = "";
  S.needsDraw = true;
}

function wallsToggle() {
  S.wallsMode.on = !S.wallsMode.on;
  S.wallsMode.pending = null;
  $("walls-btn").classList.toggle("active", S.wallsMode.on);
  $("walls-bar").hidden = !S.wallsMode.on;
  if (S.wallsMode.on) wallsSetTool(S.wallsMode.tool);
  S.needsDraw = true;
}

async function wallsOp(path, body, optimistic) {
  S.wallsBusy = (S.wallsBusy || 0) + 1;
  if (optimistic) { S.walls = optimistic; buildGrid(); refresh(); }  // show the edit immediately
  try {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    const out = await res.json();
    if (!res.ok) throw new Error(out.error || res.status);
    S.walls = out.walls;
    S.wallsVersion = out.version;
    $("walls-hint").textContent = "";
  } catch (err) {
    $("walls-hint").textContent = `Failed: ${err.message || err}`;
    try {  // fall back to the server's list
      const doc = await (await fetch("api/calibration", { cache: "no-store" })).json();
      S.walls = doc.walls || []; S.wallsVersion = doc.version;
    } catch { /* keep what we have */ }
  } finally {
    S.wallsBusy--;
  }
  buildGrid();   // recompute wall crossings for every node and cell
  refresh();
  calAutoApply();  // walls change the candidate models: re-select the best
}

const wallsAdd = (walls) => wallsOp("api/walls/add", { walls }, [...S.walls, ...walls]);

// Called by the page's refresh: adopt the server's walls only if not older than our last edit.
function wallsSync(serverWalls, serverVersion) {
  if (S.wallsBusy || S.wallsMode.pending) return false;
  if (S.wallsVersion != null && serverVersion != null && serverVersion < S.wallsVersion) return false;
  if (JSON.stringify(serverWalls || []) === JSON.stringify(S.walls)) return false;
  S.walls = serverWalls || [];
  S.wallsVersion = serverVersion;
  return true;
}

function wallNear(x, y) {
  let best = null, bd = 8 / S.view.sc;  // 8 px
  for (const w of S.walls) {
    const vx = w.x2 - w.x1, vy = w.y2 - w.y1, L2 = vx * vx + vy * vy;
    const t = Math.max(0, Math.min(1, ((x - w.x1) * vx + (y - w.y1) * vy) / L2));
    const d = Math.hypot(x - (w.x1 + t * vx), y - (w.y1 + t * vy));
    if (d < bd) { bd = d; best = w; }
  }
  return best;
}

function wallsClick(x, y) {
  const m = S.wallsMode, p = [snap(x), snap(y)];
  if (m.tool === "erase") {
    const w = wallNear(x, y);
    if (w) wallsOp("api/walls/delete", { ids: [w.id] }, S.walls.filter((q) => q !== w));
    return;
  }
  if (!m.pending) { m.pending = p; S.needsDraw = true; return; }
  const [ax, ay] = m.pending;
  if (ax === p[0] && ay === p[1]) return;
  if (m.tool === "wall") {
    m.pending = p;  // chain from here
    wallsAdd([{ x1: ax, y1: ay, x2: p[0], y2: p[1], sd_ft: m.sd }]);
  } else {
    m.pending = null;
    const [bx, by] = p;
    wallsAdd([
      { x1: ax, y1: ay, x2: bx, y2: ay }, { x1: bx, y1: ay, x2: bx, y2: by },
      { x1: bx, y1: by, x2: ax, y2: by }, { x1: ax, y1: by, x2: ax, y2: ay }].map((q) => ({ ...q, sd_ft: m.sd })));
  }
}

function wallsMove(x, y) {
  S.wallsMode.cursor = [snap(x), snap(y)];
  S.wallsMode.hover = S.wallsMode.tool === "erase" ? wallNear(x, y) : null;
  S.needsDraw = true;
}

function drawWalls(ctx, toS) {
  for (const w of S.walls) {
    const [ax, ay] = toS(w.x1, w.y1), [bx, by] = toS(w.x2, w.y2);
    const hover = S.wallsMode.on && S.wallsMode.hover === w;
    ctx.strokeStyle = hover ? "#ff5d73" : w.draft ? "rgba(207,216,230,.55)" : "#cfd8e6";
    ctx.lineWidth = hover ? 5 : 3.5; ctx.lineCap = "round";
    ctx.setLineDash(w.draft ? [8, 6] : []);
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
  }
  ctx.setLineDash([]); ctx.lineCap = "butt";
  const m = S.wallsMode;
  if (m.on && m.pending && m.cursor) {
    const [ax, ay] = toS(...m.pending), [bx, by] = toS(...m.cursor);
    ctx.strokeStyle = "#ffb347"; ctx.lineWidth = 2; ctx.setLineDash([4, 4]);
    ctx.beginPath();
    if (m.tool === "room") ctx.rect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
    else { ctx.moveTo(ax, ay); ctx.lineTo(bx, by); }
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#ffb347"; ctx.beginPath(); ctx.arc(ax, ay, 4, 0, Math.PI * 2); ctx.fill();
  }
}

// toolbar
(() => {
  const btn = document.createElement("button");
  btn.id = "walls-btn"; btn.className = "btn"; btn.textContent = "Walls";
  btn.onclick = wallsToggle;
  $("calibrate").before(btn);
  const bar = document.createElement("div");
  bar.id = "walls-bar"; bar.className = "walls-bar"; bar.hidden = true;
  bar.innerHTML = `<div class="seg"><button data-wtool="wall" class="on">Wall</button><button data-wtool="room">Room</button><button data-wtool="erase">Erase</button></div>
    <select id="walls-sd" class="walls-sd"><option value="0.5">±0.5 ft</option><option value="1">±1 ft</option><option value="2" selected>±2 ft</option><option value="4">±4 ft</option></select>
    <button class="btn small" id="walls-accept">Accept drafts</button>
    <button class="btn small" id="walls-clear">Clear all</button>
    <button class="btn small active" id="walls-done">Done</button>
    <div id="walls-hint" class="walls-hint"></div>`;
  document.querySelector(".map-wrap").appendChild(bar);
  bar.querySelectorAll("[data-wtool]").forEach((b) => { b.onclick = () => wallsSetTool(b.dataset.wtool); });
  S.wallsMode.sd = 2;
  $("walls-sd").onchange = () => { S.wallsMode.sd = parseFloat($("walls-sd").value); };
  $("walls-done").onclick = wallsToggle;
  $("walls-accept").onclick = () => wallsOp("api/walls/accept", {}, S.walls.map(({ draft, ...w }) => w));
  let armed = false;
  $("walls-clear").onclick = () => {
    if (!armed) { armed = true; $("walls-clear").textContent = "Confirm"; setTimeout(() => { armed = false; $("walls-clear").textContent = "Clear all"; }, 4000); return; }
    armed = false; $("walls-clear").textContent = "Clear all";
    wallsOp("api/walls/clear", {}, []);
  };
  window.addEventListener("keydown", (ev) => {
    if (S.wallsMode.on && ev.key === "Escape") { S.wallsMode.pending = null; S.needsDraw = true; ev.stopImmediatePropagation(); }
  }, true);
})();
