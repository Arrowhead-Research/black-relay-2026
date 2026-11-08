"use strict";
/*
 * WBT Console map: draws each device where the server places it (Wi-Fi,
 * Bluetooth, TPMS). Site menu, modes, node placement and sign-in: console.js.
 *
 * Positions are computed on the server (console/locate.py) from per-node
 * RSSI with the site's calibrated model, walls and time smoothing, and arrive
 * with each device in /api/state (``position``: x, y in ft, node count,
 * transmit-power offset, residual, and lat/lon once the site has node GPS
 * points). The grid here (buildGrid) is still used by calibration (calib.js).
 *
 * No model-derived error radius is drawn: accuracy is calibration's measured
 * leave-one-out error at the spots.
 */

const FT_PER_M = 3.28084;
const TYPES = {
  wifi_ap: "Wi-Fi AP",
  wifi_client: "Wi-Fi client",
  ble: "BLE",
  bt_classic: "BT classic",
  tpms: "TPMS",
};
const BAND_OF = { wifi_ap: "wifi", wifi_client: "wifi", ble: "bt", bt_classic: "bt", tpms: "tpms" };
const LOCAL_MEAN_FT = 4;  // dot = weighted mean of the likelihood within this radius of the peak
const NODE_COLORS = ["#ffd166", "#06d6a0", "#ef476f", "#118ab2", "#f78c6b", "#c77dff"];
const css = getComputedStyle(document.documentElement);
const typeColor = (t) => css.getPropertyValue("--" + t).trim() || "#ccc";

const store = {
  get(k, d) { try { const v = localStorage.getItem("dfviz:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("dfviz:" + k, JSON.stringify(v)); } catch { /* no storage */ } },
};

const S = {
  data: null,
  paused: false,
  nodes: {},            // node id -> [x, y] ft (effective, may be dragged)
  nodeOrder: [],
  selfRadio: {},        // mac -> node id
  models: {               // per band: path-loss exponent, RSSI at 1 m, noise
    wifi: { n: 2.7, p0: -40, sigma: 6 },
    bt: { n: 2.5, p0: -59, sigma: 6 },
    tpms: { n: 2.2, p0: -45, sigma: 7 },
  },
  band: "wifi",          // band whose sliders are shown
  version: 0,            // bumps when the model or layout changes
  offsets: { wifi: {}, bt: {}, tpms: {} },  // per band, per node dB correction from live calibration
  walls: [],             // floor-plan walls [{x1, y1, x2, y2, draft?}] (ft)
  nodesMode: { on: false, placing: null },
  wallsMode: { on: false, tool: "wall", pending: null, cursor: null, hover: null },
  calib: { on: false, spot: null, recording: false, points: [], fits: {}, deleted: [], view: null },
  steps: new Map(),      // id -> recent estimate movements (ft per update)
  rings: true, trail: true, labels: false,
  types: new Set(store.get("types2", Object.keys(TYPES))),
  search: "",
  selected: store.get("selected", null),
  hover: null,
  est: new Map(),       // id -> estimate
  disp: new Map(),      // id -> animated [x, y]
  trails: new Map(),    // id -> [[x, y], ...]
  grid: null,
  allHeat: null,
  view: null,
  drag: null,
  fit: null,
  lastFetch: 0,
  needsDraw: true,
};

// ---------- grid & localization ----------
function buildGrid() {
  const pts = Object.values(S.nodes);
  const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
  const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 10);
  const m = Math.max(12, span * 0.5);
  const x0 = Math.min(...xs) - m, x1 = Math.max(...xs) + m;
  const y0 = Math.min(...ys) - m, y1 = Math.max(...ys) + m;
  const cell = Math.max(0.25, Math.max(x1 - x0, y1 - y0) / 180);
  const nx = Math.ceil((x1 - x0) / cell), ny = Math.ceil((y1 - y0) / cell);
  const logd = {};
  for (const [id, [px, py]] of Object.entries(S.nodes)) {
    const a = new Float32Array(nx * ny);
    for (let j = 0; j < ny; j++) {
      const y = y0 + (j + 0.5) * cell;
      for (let i = 0; i < nx; i++) {
        const x = x0 + (i + 0.5) * cell;
        const dm = Math.hypot(x - px, y - py) / FT_PER_M;
        a[j * nx + i] = Math.log10(Math.max(dm, 0.3));
      }
    }
    logd[id] = a;
  }
  // walls crossed on the straight path from each node to each cell
  const walls = {};
  if (S.walls.length) {
    for (const [id, [px, py]] of Object.entries(S.nodes)) {
      const a = new Float32Array(nx * ny);
      for (let j = 0; j < ny; j++) {
        const y = y0 + (j + 0.5) * cell;
        // expected crossings given each wall's drawing accuracy (exact count while dragging a node: faster)
        for (let i = 0; i < nx; i++) a[j * nx + i] = S.drag ? wallsCrossed(px, py, x0 + (i + 0.5) * cell, y) : wallsExpected(px, py, x0 + (i + 0.5) * cell, y);
      }
      walls[id] = a;
    }
  }
  S.grid = { x0, y0, x1, y1, cell, nx, ny, logd, walls };
  S.version++;  // geometry changed: restart smoothing
}


// sigma^2 / tau^2 for the transmit-power prior (tau = the band's fitted spread).
function txLambda(sigma, spread) {
  return (sigma * sigma) / (spread * spread);
}




function visible(dev) {
  if (!S.types.has(dev.type)) return false;
  if (!S.search) return true;
  const q = S.search.toLowerCase();
  return [dev.id, dev.name, dev.manufacturer, S.selfRadio[dev.id]].some((v) => v && String(v).toLowerCase().includes(q));
}

// Server positions -> the page's estimates (S.est).
function recompute() {
  if (!S.data) return;
  const prev = S.est;
  S.est = new Map();
  for (const dev of S.data.devices) {
    const p = dev.position;
    if (!p) continue;
    const e = { kind: "fix", x: p.x, y: p.y, K: p.nodes, ids: p.ids, offset: p.offset_db, res: p.res_db,
                t: p.t, lat: p.lat, lon: p.lon, errM: p.err_m };
    S.est.set(dev.id, e);
    const q = prev.get(dev.id);
    if (q && q.t !== e.t) {
      const st = S.steps.get(dev.id) || [];
      st.push(Math.hypot(e.x - q.x, e.y - q.y));
      if (st.length > 20) st.shift();
      S.steps.set(dev.id, st);
    }
    if (!S.disp.has(dev.id)) S.disp.set(dev.id, [e.x, e.y]);
  }
  S.needsDraw = true;
}

// ---------- drawing ----------
const map = document.getElementById("map");
const ctx = map.getContext("2d");

function layoutView() {
  const r = map.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  if (map.width !== Math.round(r.width * dpr) || map.height !== Math.round(r.height * dpr)) {
    map.width = Math.round(r.width * dpr);
    map.height = Math.round(r.height * dpr);
  }
  const g = S.grid, pad = 36;
  const sc = Math.min((r.width - 2 * pad) / (g.x1 - g.x0), (r.height - 2 * pad) / (g.y1 - g.y0));
  S.view = {
    w: r.width, h: r.height, dpr, sc,
    ox: (r.width - sc * (g.x1 - g.x0)) / 2 - g.x0 * sc,
    oy: (r.height - sc * (g.y1 - g.y0)) / 2 - g.y0 * sc,
  };
}
const toS = (x, y) => [S.view.ox + x * S.view.sc, S.view.oy + y * S.view.sc];
const toW = (sx, sy) => [(sx - S.view.ox) / S.view.sc, (sy - S.view.oy) / S.view.sc];

function hull() {
  const pts = S.nodeOrder.map((id) => [id, ...S.nodes[id]]);
  const cx = pts.reduce((a, p) => a + p[1], 0) / pts.length, cy = pts.reduce((a, p) => a + p[2], 0) / pts.length;
  return pts.sort((a, b) => Math.atan2(a[2] - cy, a[1] - cx) - Math.atan2(b[2] - cy, b[1] - cx));
}

function draw() {
  if (!S.grid) return;
  layoutView();
  const { dpr, w, h, sc } = S.view, g = S.grid;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const bg = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) / 1.2);
  bg.addColorStop(0, "#111a2a"); bg.addColorStop(1, "#090d15");
  ctx.fillStyle = bg; ctx.fillRect(0, 0, w, h);

  // grid lines every 5 ft
  ctx.lineWidth = 1;
  ctx.font = "10px system-ui"; ctx.fillStyle = "#4d5b75";
  for (let x = Math.ceil(g.x0 / 5) * 5; x <= g.x1; x += 5) {
    const [sx] = toS(x, 0); ctx.strokeStyle = x % 10 === 0 ? "#1d2840" : "#151e30";
    ctx.beginPath(); ctx.moveTo(sx + 0.5, toS(0, g.y0)[1]); ctx.lineTo(sx + 0.5, toS(0, g.y1)[1]); ctx.stroke();
    if (x % 10 === 0) ctx.fillText(`${x}′`, sx + 3, toS(0, g.y0)[1] + 11);
  }
  for (let y = Math.ceil(g.y0 / 5) * 5; y <= g.y1; y += 5) {
    const [, sy] = toS(0, y); ctx.strokeStyle = y % 10 === 0 ? "#1d2840" : "#151e30";
    ctx.beginPath(); ctx.moveTo(toS(g.x0, 0)[0], sy + 0.5); ctx.lineTo(toS(g.x1, 0)[0], sy + 0.5); ctx.stroke();
    if (y % 10 === 0) ctx.fillText(`${y}′`, toS(g.x0, 0)[0] + 3, sy - 3);
  }

  drawWalls(ctx, toS);

  // building outline (node hull) with edge lengths
  const H = hull();
  ctx.setLineDash([6, 5]); ctx.strokeStyle = "rgba(230,237,247,.45)"; ctx.lineWidth = 1.5;
  ctx.beginPath();
  H.forEach(([, x, y], i) => { const [sx, sy] = toS(x, y); i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy); });
  ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
  ctx.font = "11px system-ui"; ctx.textAlign = "center";
  H.forEach(([, x, y], i) => {
    const [, x2, y2] = H[(i + 1) % H.length];
    const [sx, sy] = toS((x + x2) / 2, (y + y2) / 2);
    const label = `${Math.hypot(x2 - x, y2 - y).toFixed(0)} ft`;
    const tw = ctx.measureText(label).width + 10;
    ctx.fillStyle = "rgba(11,15,23,.8)"; ctx.fillRect(sx - tw / 2, sy - 8, tw, 16);
    ctx.fillStyle = "#aab6cc"; ctx.fillText(label, sx, sy + 4);
  });
  ctx.textAlign = "left";

  const sel = S.data && S.data.devices.find((d) => d.id === S.selected);
  const selEst = S.est.get(S.selected);

  // range rings (single-node devices: the one ring is all we know)
  if (sel && selEst && selEst.kind === "proximity") {
    const [sx, sy] = toS(...S.nodes[selEst.node]);
    const col = typeColor(sel.type);
    ctx.strokeStyle = col; ctx.fillStyle = col + "18"; ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.arc(sx, sy, selEst.dFt * sc, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); ctx.setLineDash([]);
    ctx.font = "12px system-ui"; ctx.fillStyle = col;
    ctx.fillText(`~${selEst.dFt.toFixed(0)} ft`, sx + 14, sy + selEst.dFt * sc + 16);
  } else if (sel && selEst && S.rings) {
    const M = S.models[sel.band] || S.models.wifi;
    for (const id of selEst.ids) {
      const rssi = sel.nodes[id].rssi;
      const dFt = Math.pow(10, (M.p0 + selEst.offset - rssi) / (10 * M.n)) * FT_PER_M;
      const [sx, sy] = toS(...S.nodes[id]);
      ctx.strokeStyle = nodeColor(id) + "aa"; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.arc(sx, sy, dFt * sc, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    }
  }

  // trail
  if (sel && S.trail) {
    const t = S.trails.get(sel.id) || [];
    for (let i = 1; i < t.length; i++) {
      const [ax, ay] = toS(...t[i - 1]), [bx, by] = toS(...t[i]);
      ctx.strokeStyle = `rgba(255,179,71,${(i / t.length) * 0.8})`; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
    }
  }

  // devices
  let moving = false;
  const dots = [];
  for (const [id, e] of S.est) {
    const dev = S.devById.get(id);
    if (!dev || e.kind !== "fix" || (!visible(dev) && id !== S.selected)) continue;
    if (S.selfRadio[id]) continue;  // a node's own radio is at its node (drawn below), not at an estimate
    if (S.selected && id !== S.selected && S.est.has(S.selected)) continue;  // focus: hide the others
    const p = S.disp.get(id);
    const dx = e.x - p[0], dy = e.y - p[1];
    if (Math.abs(dx) + Math.abs(dy) > 0.02) { p[0] += dx * 0.08; p[1] += dy * 0.08; moving = true; } else { p[0] = e.x; p[1] = e.y; }
    dots.push([id, dev, p]);
  }
  dots.sort((a, b) => (a[0] === S.selected) - (b[0] === S.selected));
  // demo emitters: true position (hollow diamond) and the error to the estimate
  for (const [id, dev, p] of dots) {
    if (!dev.truth) continue;
    const [tx, ty] = toS(...dev.truth), [sx, sy] = toS(...p);
    ctx.strokeStyle = "rgba(155,231,255,.7)"; ctx.lineWidth = 1; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(sx, sy); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = "#9be7ff"; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.moveTo(tx, ty - 6); ctx.lineTo(tx + 6, ty); ctx.lineTo(tx, ty + 6); ctx.lineTo(tx - 6, ty); ctx.closePath(); ctx.stroke();
  }
  ctx.font = "11px system-ui";
  for (const [id, dev, p] of dots) {
    const [sx, sy] = toS(...p);
    const isSel = id === S.selected, isHover = id === S.hover;
    const col = typeColor(dev.type);
    const radius = isSel ? 7 : isHover ? 6 : 4;
    if (isSel) { ctx.shadowColor = col; ctx.shadowBlur = 18; }
    const e = S.est.get(id);
    // Two nodes only constrain a curve, not a point: draw those fainter.
    ctx.fillStyle = col; ctx.globalAlpha = !isSel && !isHover && e && e.K < 3 ? 0.35 : 0.95;
    ctx.beginPath();
    if (dev.band === "tpms") ctx.roundRect(sx - radius, sy - radius, radius * 2, radius * 2, 2);   // vehicle sensor
    else if (dev.band === "bt") { ctx.moveTo(sx, sy - radius * 1.2); ctx.lineTo(sx + radius * 1.1, sy + radius * 0.8); ctx.lineTo(sx - radius * 1.1, sy + radius * 0.8); ctx.closePath(); }
    else ctx.arc(sx, sy, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0; ctx.globalAlpha = 1;
    if (S.selfRadio[id]) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(sx, sy, radius + 3, 0, Math.PI * 2); ctx.stroke(); }
    if (isSel) { ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(sx, sy, radius, 0, Math.PI * 2); ctx.stroke(); }
    if (S.labels || isSel || isHover) {
      ctx.fillStyle = isSel ? "#fff" : "#c9d3e3";
      ctx.fillText(displayName(dev), sx + radius + 4, sy + 4);
    }
  }

  if (sel && S.selfRadio[sel.id]) {
    const node = S.selfRadio[sel.id], [nx, ny] = toS(...S.nodes[node]);
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(nx, ny, 16, 0, Math.PI * 2); ctx.stroke();
    if (selEst && selEst.kind === "fix") {
      const [ex, ey] = toS(selEst.x, selEst.y), off = Math.hypot(selEst.x - S.nodes[node][0], selEst.y - S.nodes[node][1]);
      ctx.strokeStyle = "rgba(255,255,255,.5)"; ctx.lineWidth = 1; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(nx, ny); ctx.lineTo(ex, ey); ctx.stroke(); ctx.setLineDash([]);
      ctx.beginPath(); ctx.arc(ex, ey, 5, 0, Math.PI * 2); ctx.stroke();
      ctx.font = "11px system-ui"; ctx.fillStyle = "#c9d3e3";
      ctx.fillText(`model: ${off.toFixed(0)} ft off`, ex + 9, ey + 4);
    }
  }

  if (S.calib.on) drawCal(ctx, toS, sc);

  // nodes
  for (const id of S.nodeOrder) {
    const [sx, sy] = toS(...S.nodes[id]);
    const col = nodeColor(id);
    ctx.fillStyle = "#0b0f17"; ctx.strokeStyle = col; ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.roundRect(sx - 9, sy - 9, 18, 18, 4); ctx.fill(); ctx.stroke();
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(sx, sy, 3, 0, Math.PI * 2); ctx.fill();
    ctx.font = "600 12px system-ui"; ctx.fillStyle = "#fff";
    const below = S.nodes[id][1] > (S.grid.y0 + S.grid.y1) / 2;
    ctx.fillText(id, sx + 13, sy + (below ? 18 : -10));
    if (sel && sel.nodes[id]) {
      ctx.font = "11px system-ui"; ctx.fillStyle = col;
      ctx.fillText(`${sel.nodes[id].rssi.toFixed(0)} dBm`, sx + 13, sy + (below ? 32 : -24));
    }
  }

  S.needsDraw = moving;
}

function nodeColor(id) { return NODE_COLORS[Math.max(0, S.nodeOrder.indexOf(id)) % NODE_COLORS.length]; }
function displayName(dev) {
  if (S.selfRadio[dev.id]) return `${S.selfRadio[dev.id]} radio`;
  return dev.name || dev.manufacturer || dev.id;
}

function frame() {
  if (S.needsDraw) draw();
  requestAnimationFrame(frame);
}

// ---------- sidebar ----------
const $ = (id) => document.getElementById(id);

function renderSelected() {
  const dev = S.data && S.data.devices.find((d) => d.id === S.selected);
  const e = S.est.get(S.selected);
  $("selected-card").hidden = !dev;
  if (!dev) return;
  $("sel-dot").style.background = typeColor(dev.type);
  $("sel-name").innerHTML = escapeHtml(displayName(dev)) + (S.selfRadio[dev.id] ? '<span class="badge">node</span>' : "");
  $("sel-id").textContent = `${dev.id} · ${TYPES[dev.type] || dev.type}${dev.manufacturer && dev.name ? " · " + dev.manufacturer : ""}`;
  const fix = e && e.kind === "fix";
  const radioOf = S.selfRadio[dev.id];
  $("sel-pos").textContent = radioOf ? radioOf : fix ? `${e.x.toFixed(1)}, ${e.y.toFixed(1)} ft` : e ? `~${e.dFt.toFixed(0)} ft from ${e.node}` : "—";
  $("sel-nodes").textContent = `${Object.keys(dev.nodes).length} / ${S.nodeOrder.length}`;
  $("sel-res").textContent = fix ? `${e.res.toFixed(1)} dB` : "—";
  const geo = fix && e.lat != null && !radioOf;
  $("sel-geo-box").hidden = !geo;
  if (geo) {
    $("sel-geo").textContent = `${e.lat.toFixed(6)}, ${e.lon.toFixed(6)}`;
    $("sel-geo-box").title = e.errM != null ? `Median error ±${e.errM} m (calibration and node GPS fit)` : "Not calibrated: no measured accuracy yet";
  }
  const truth = dev.truth || (radioOf ? S.nodes[radioOf] : null);  // demo truth, or a node radio's node
  $("sel-truth-box").hidden = !(truth && fix);
  if (truth && fix) $("sel-truth").textContent = `${Math.hypot(e.x - truth[0], e.y - truth[1]).toFixed(1)} ft`;
  const st = S.steps.get(dev.id) || [];
  $("sel-step").textContent = st.length ? `${(st.reduce((a, b) => a + b, 0) / st.length).toFixed(1)} ft` : "—";
  const bars = $("sel-bars");
  bars.innerHTML = "";
  for (const id of S.nodeOrder) {
    const n = dev.nodes[id];
    const pct = n ? Math.max(3, Math.min(100, ((n.rssi + 100) / 75) * 100)) : 0;
    let dist = "";
    const M = S.models[dev.band] || S.models.wifi;
    if (n && e) dist = ` · ≈${(Math.pow(10, (M.p0 + e.offset - n.rssi) / (10 * M.n)) * FT_PER_M).toFixed(0)} ft`;
    const row = document.createElement("div");
    row.className = "bar";
    row.innerHTML = `<span style="color:${nodeColor(id)}">${id}</span><div class="track"><div class="fill" style="width:${pct}%"></div></div>` +
      `<span class="v">${n ? n.rssi.toFixed(0) + " dBm" : "—"}</span>`;
    row.title = n ? `${n.n} samples, ±${n.spread} dB${dist}` : "";
    bars.appendChild(row);
  }
}

function renderList() {
  const ul = $("device-list");
  const top = ul.parentElement.scrollTop;
  ul.innerHTML = "";
  if (!S.data) return;
  let shown = 0;
  for (const dev of S.data.devices) {
    if (!visible(dev)) continue;
    if (++shown > 400) break;
    const li = document.createElement("li");
    const strongest = Math.max(...Object.values(dev.nodes).map((n) => n.rssi));
    const e = S.est.get(dev.id);
    li.className = (dev.id === S.selected ? "sel " : "") + (e && e.kind === "fix" ? "" : "dim");
    li.innerHTML = `<i class="type-dot" style="background:${typeColor(dev.type)}"></i>
      <div><div class="dname">${escapeHtml(displayName(dev))}${S.selfRadio[dev.id] ? '<span class="badge">node</span>' : ""}${dev.randomized ? '<span class="badge">random MAC</span>' : ""}</div>
      <div class="dmeta mono">${dev.id}${e && e.kind === "proximity" ? ` · ~${e.dFt.toFixed(0)} ft from ${e.node}` : ""}</div></div>
      <div class="dright">${strongest.toFixed(0)} dBm<br><span class="nodedots">${S.nodeOrder.map((n) => `<span class="${dev.nodes[n] ? "on" : ""}"></span>`).join("")}</span></div>`;
    li.onclick = () => (S.calib.on ? calSetRef(dev.id) : select(dev.id === S.selected ? null : dev.id));
    ul.appendChild(li);
  }
  if (!shown) ul.innerHTML = '<li class="muted">No devices match.</li>';
  ul.parentElement.scrollTop = top;
}

function renderChips() {
  const box = $("type-chips");
  box.innerHTML = "";
  const counts = {};
  for (const d of (S.data ? S.data.devices : [])) counts[d.type] = (counts[d.type] || 0) + 1;
  for (const [t, label] of Object.entries(TYPES)) {
    const c = document.createElement("span");
    c.className = "chip" + (S.types.has(t) ? " on" : "");
    c.innerHTML = `<i style="background:${typeColor(t)}"></i>${label} <b>${counts[t] || 0}</b>`;
    c.onclick = () => { S.types.has(t) ? S.types.delete(t) : S.types.add(t); store.set("types2", [...S.types]); refresh(); };
    box.appendChild(c);
  }
}

function renderStatus() {
  const pill = $("kismet-pill"), k = S.data && S.data.kismet;
  pill.classList.toggle("ok", !!(k && k.ok));
  pill.classList.toggle("bad", !!(k && !k.ok));
  const age = k && k.last_poll ? Math.max(0, Math.round(S.data.now - k.last_poll)) : null;
  pill.lastElementChild.textContent = !k ? "connecting…" : k.ok ? `Kismet live · ${age}s` : `Kismet error: ${k.error}`;
  if (S.paused) pill.lastElementChild.textContent = "paused";
  const total = S.data ? S.data.devices.length : 0;
  // Capture sources that run but deliver nothing (or are stopped) — Kismet itself shows no error for these.
  const bad = ((S.data && S.data.sources) || []).filter((x) => x.status !== "ok");
  $("src-pill").hidden = !bad.length;
  if (bad.length) {
    $("src-pill").textContent = `⚠ ${bad.length} ${bad.every((x) => x.status === "silent") ? "silent" : "sources down"}`;
    $("src-pill").title = bad.map((x) => `${x.name}: ${x.status}${x.status === "silent" ? ` ${Math.round(x.quiet_s / 60)} min` : x.error ? ` (${x.error})` : ""}`).join("\n");
  }
  const located = [...S.est.values()].filter((e) => e.kind === "fix").length;
  $("count-pill").textContent = `${total} devices · ${located} located`;
}

// ---------- controls ----------
function showBand(band) {
  S.band = band;
  renderModelCard();
}

// Read-only: the model in use per band, and where it came from.
function renderModelCard() {
  if (!S.data) return;
  const names = { wifi: "Wi-Fi", bt: "Bluetooth", tpms: "TPMS" };
  const rows = Object.keys(S.models).map((band) => {
    const m = S.models[band], f = S.calib.fits[band];
    return `<tr><td><b>${names[band]}</b></td><td>${m.n}</td><td>${m.sigma}</td><td>${S.walls.length && m.wall ? m.wall : "—"}</td>` +
      `<td>${f ? f.loo_median_ft.toFixed(1) + " ft" : '<span class="muted">default</span>'}</td></tr>`;
  }).join("");
  $("model-card").innerHTML = `<tr><th></th><th>n</th><th>σ dB</th><th>wall dB</th><th>error</th></tr>${rows}`;
}

function refresh() {
  recompute();
  renderSelected();
  renderList();
  renderChips();
  renderStatus();
  renderModelCard();
  renderLiveCal();
  renderGeoStatus();
  S.needsDraw = true;
}

function select(id) {
  S.selected = id;
  $("show-all").hidden = !id;
  const dev = id && S.devById && S.devById.get(id);
  if (dev && dev.band && dev.band !== S.band) showBand(dev.band);
  store.set("selected", id);
  if (id && !S.trails.has(id)) S.trails.set(id, []);
  refresh();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// ---------- map interaction ----------
function nodeAt(sx, sy) {
  for (const id of S.nodeOrder) { const [x, y] = toS(...S.nodes[id]); if (Math.hypot(sx - x, sy - y) < 13) return id; }
  return null;
}
function deviceAt(sx, sy) {
  let best = null, bd = 12;
  for (const [id] of S.est) {
    const dev = S.devById.get(id), p = S.disp.get(id);
    if (!dev || !p || (!visible(dev) && id !== S.selected)) continue;
    if (S.selected && id !== S.selected && S.est.has(S.selected)) continue;
    const [x, y] = toS(...p), d = Math.hypot(sx - x, sy - y);
    if (d < bd) { bd = d; best = id; }
  }
  return best;
}

function pointer(ev) { const r = map.getBoundingClientRect(); return [ev.clientX - r.left, ev.clientY - r.top]; }

map.addEventListener("mousedown", (ev) => {
  const [sx, sy] = pointer(ev);
  const n = nodeAt(sx, sy);
  if (n && S.nodesMode.on) { S.drag = n; ev.preventDefault(); }  // nodes move only in Setup > Nodes
});
window.addEventListener("mouseup", () => {
  if (S.drag) { const id = S.drag; S.drag = null; buildGrid(); refresh(); nodesSave(id, S.nodes[id]); }
});
map.addEventListener("mousemove", (ev) => {
  if (!S.view) return;
  const [sx, sy] = pointer(ev);
  const [wx, wy] = toW(sx, sy);
  if (S.wallsMode.on) wallsMove(wx, wy);
  if (S.drag) {
    S.nodes[S.drag] = [Math.round(wx * 2) / 2, Math.round(wy * 2) / 2];
    if (!S.dragPending) {
      S.dragPending = true;
      requestAnimationFrame(() => { S.dragPending = false; buildGrid(); recompute(); renderSelected(); S.needsDraw = true; });
    }
    return;
  }
  const n = nodeAt(sx, sy);
  const d = n ? null : deviceAt(sx, sy);
  map.style.cursor = n && S.nodesMode.on ? "grab" : d ? "pointer" : "crosshair";
  if (d !== S.hover) { S.hover = d; S.needsDraw = true; }
  const tip = $("tooltip");
  if (d) {
    const dev = S.devById.get(d), e = S.est.get(d);
    tip.innerHTML = `<b>${escapeHtml(displayName(dev))}</b><span class="mono muted">${dev.id}</span><br>${TYPES[dev.type]} · ${e.K} nodes<br>` +
      S.nodeOrder.filter((k) => dev.nodes[k]).map((k) => `<span style="color:${nodeColor(k)}">${k}</span> ${dev.nodes[k].rssi.toFixed(0)}`).join(" · ");
    tip.hidden = false;
    tip.style.left = Math.min(sx + 14, S.view.w - 270) + "px";
    tip.style.top = sy + 14 + "px";
  } else if (n) {
    tip.innerHTML = `<b>${n}</b>${S.nodes[n][0].toFixed(1)}, ${S.nodes[n][1].toFixed(1)} ft`;
    tip.hidden = false; tip.style.left = sx + 14 + "px"; tip.style.top = sy + 14 + "px";
  } else tip.hidden = true;
  $("cursor-readout").textContent = `x ${wx.toFixed(1)} ft · y ${wy.toFixed(1)} ft`;
});
map.addEventListener("mouseleave", () => { $("tooltip").hidden = true; S.hover = null; S.needsDraw = true; });
map.addEventListener("click", (ev) => {
  const [sx, sy] = pointer(ev);
  if (nodeAt(sx, sy)) return;
  if (S.nodesMode.on) { nodesClick(...toW(sx, sy)); return; }  // placing a node
  if (S.wallsMode.on) { wallsClick(...toW(sx, sy)); return; }  // drawing walls
  if (S.calib.on) { calSetSpot(...toW(sx, sy)); return; }  // calibration: click marks where you stand
  const d = deviceAt(sx, sy);
  select(d || null);  // empty space: back to all devices
});
window.addEventListener("keydown", (ev) => {
  if (ev.target.tagName === "INPUT") return;
  if (ev.key === "Escape") select(null);
  if (ev.key === " ") { ev.preventDefault(); togglePause(); }
});
window.addEventListener("resize", () => { S.needsDraw = true; });

for (const k of ["rings", "trail", "labels"]) {
  $(k).checked = S[k];
  $(k).onchange = () => { S[k] = $(k).checked; store.set(k, S[k]); S.needsDraw = true; };
}
$("search").oninput = () => { S.search = $("search").value.trim(); refresh(); };
$("pause").onclick = togglePause;
function togglePause() {
  S.paused = !S.paused;
  $("pause").textContent = S.paused ? "Resume" : "Pause";
  $("pause").classList.toggle("active", S.paused);
  renderStatus();
}

// "All devices" button (shown while focused on one device)
const back = document.createElement("button");
back.className = "btn small active"; back.id = "show-all"; back.textContent = "← All"; back.hidden = true;
back.onclick = () => select(null);
document.querySelector(".top-left").appendChild(back);

// ---------- data ----------
async function poll() {
  if (!S.paused || !S.data) {
    try {
      const res = await fetch("api/state", { cache: "no-store" });
      if (res.status === 401) { setTimeout(poll, 2500); return; }  // console.js shows sign-in
      const data = await res.json();
      const first = !S.data;
      if (!first && siteSync(data)) return;  // switched site: the page reloads
      S.data = data;
      S.devById = new Map(data.devices.map((d) => [d.id, d]));
      // walls changed by another page: adopt them unless this page has a newer edit
      if (!first && wallsSync(data.walls, data.calibration_version)) buildGrid();
      S.selfRadio = Object.fromEntries(Object.entries(data.layout.self_emitters || {}).map(([n, mac]) => [mac, n]));
      if (first) init(data);
      refresh();
      const e = S.est.get(S.selected);
      if (e) {
        const t = S.trails.get(S.selected) || [];
        const last = t[t.length - 1];
        if (!last || Math.hypot(last[0] - e.x, last[1] - e.y) > 0.2) t.push([e.x, e.y]);
        if (t.length > 40) t.shift();
        S.trails.set(S.selected, t);
      }
    } catch (err) {
      $("kismet-pill").classList.add("bad");
      $("kismet-pill").lastElementChild.textContent = "console server unreachable";
    }
  }
  setTimeout(poll, 2500);
}

function init(data) {
  const L = data.layout;
  S.nodeOrder = Object.keys(L.nodes);
  S.walls = data.walls || [];
  S.wallsVersion = data.calibration_version;
  S.nodes = structuredClone(L.nodes);   // node positions belong to the site (server)
  S.siteVersion = data.site && data.site.version;
  for (const [band, m] of Object.entries(L.models || {})) {
    S.models[band] = { n: m.path_loss_exp, p0: m.ref_rssi_1m, sigma: m.sigma_db, wall: m.wall_db || 0, txSpread: 8 };
  }
  S.calib.fits = { ...(data.calibration_fits || {}), ...S.calib.fits };
  for (const k of ["rings", "trail", "labels"]) { S[k] = store.get(k, S[k]); $(k).checked = S[k]; }
  buildGrid();
  calUseFits();        // saved calibration fits, else layout defaults
  calAutoApply();      // re-select the best model for the current spots and walls
}

requestAnimationFrame(frame);
poll();
