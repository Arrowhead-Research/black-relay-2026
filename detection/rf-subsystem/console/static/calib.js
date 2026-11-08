"use strict";
/*
 * Live calibration with known devices.
 *
 * 1. Select the device you carry in the list (any band; several devices or
 *    SSIDs can be used, each spot remembers its own). The Record button names
 *    the device it will record: what is selected is what is recorded.
 * 2. Stand still, click your spot on the map, press Record: the server reads
 *    every packet the nodes logged from the device in those 15 s (Kismet's
 *    packet log) and stores the median per node as one spot.
 * 3. With spots spread over the area, fit per band, by least squares:
 *        rssi(spot, node k) = P0[device] + c_k - 10 n log10(d_k) - W * walls_k
 *    (walls_k = walls the straight spot-to-node path crosses; W = wall loss,
 *    fitted only when some paths cross walls)
 *    Each device has its own P0 (transmit power differs between devices and
 *    SSIDs); c_k = optional per-node offset (average 0); sigma = residual sd.
 * 4. Measured accuracy: leave-one-out. Each spot is located with a model
 *    fitted on the other spots; its distance to the marked position is the
 *    error. The median is an empirical CEP50 for these spots.
 * 5. Automatic model selection (no manual parameters): every candidate
 *    (per-node offsets on/off x wall loss on/off x transmit-power spread x
 *    node links on/off) is fitted and scored by its leave-one-out median
 *    error at the walked spots. The best one is used if it beats the default
 *    model at the same spots. The server does the selection and saves the fit
 *    (console/calfit.py, same maths); this page shows the analysis.
 * 6. Node links (Wi-Fi): the server records each node's own radio as heard by
 *    the other nodes (known positions and walls, no walking). They are extra
 *    training spots only: accuracy is always measured at walked spots.
 *
 * The server owns the spots: each add/delete/reassign is one request and is
 * journaled, so two open pages cannot overwrite each other, and deleted spots
 * can be restored. A spot recorded for the wrong device can be rebuilt for
 * the right one from Kismet's packet log (↻).
 */

// Bluetooth capture reports each device about once per scan (every 7-10 s per
// node), so a Bluetooth spot records longer to get enough samples per node.
const CAL_RECORD_S = { wifi: 15, bt: 45, tpms: 15 };
const calRecordS = (band) => CAL_RECORD_S[band] || 15;
const CAL_MIN_SAMPLES = 3;   // per node, for a node to count in a spot
const CAL_MIN_RATE = { wifi: 0.4, bt: 0.08, tpms: 0.4 };   // per node/s below which the device is too quiet to calibrate well
const CAL_MOVE_FT = 10;      // spots at least this far apart...
const CAL_MOVE_DB = 3;       // ...should differ by more than this at some node
// A candidate is rejected when implausible (the measurements, not the model, are wrong).
const CAL_N_RANGE = [1.5, 5];
const CAL_MAX_SIGMA = 12;
const CAL_MIN_LOO = 5;               // spots needed in the error estimate before a fit replaces the defaults
const CAL_TX_SPREADS = [4, 8, 16];   // transmit-power uncertainty candidates, dB

const calDev = () => (S.selected && S.devById && S.devById.get(S.selected)) || null;
const calBand = () => (calDev() ? calDev().band : S.band);
const calName = (p) => p.device_name || (p.device || "").toUpperCase();

function calSetRef(id) {   // list click while calibrating
  select(id);
  const dev = S.devById.get(id);
  if (dev) S.band = dev.band;
  renderLiveCal();
}

function calSetSpot(x, y) {
  const t = (S.calib.suggest && S.calib.suggest.targets || []).find((q) => Math.hypot(q.x - x, q.y - y) < 2.5);
  S.calib.spot = t ? [t.x, t.y] : [Math.round(x * 2) / 2, Math.round(y * 2) / 2];
  renderLiveCal();
  S.needsDraw = true;
}

async function calApi(path, body) {
  const res = await fetch(path, body === undefined ? { cache: "no-store" } : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || res.status);
  return data;
}

async function calLoad() {
  try {
    const doc = await calApi("api/calibration");
    S.calib.points = doc.points || [];
    S.calib.fits = doc.fits || {};
    S.calib.version = doc.version;
    for (const pt of S.calib.points) if (!pt.warn && pt.n) pt.warn = calQualityWarnings(pt);
    S.calib.deleted = (await calApi("api/calibration/deleted")).points || [];
    S.calib.loaded = true;   // auto-selection may run only once the spots are known
  } catch { /* server without calibration support */ }
  if (S.data && S.grid) {
    calUseFits();
    await calAutoApply();
  }
  renderLiveCal();
  S.needsDraw = true;
}

async function calRecord() {
  const dev = calDev();
  // A node's own radio is at its node: use the node's position, not the click.
  const radioOf = dev && S.selfRadio[dev.id];
  const spot = radioOf ? S.nodes[radioOf] : S.calib.spot;
  if (!dev || !spot || S.calib.recording) return;
  S.calib.recording = true;
  const t0 = S.data.now;
  const secs = calRecordS(dev.band);
  const started = Date.now();
  const tick = setInterval(() => {
    const el = (Date.now() - started) / 1000;
    $("cal-progress").innerHTML = el < secs
      ? `<span class="progress"><i style="width:${(el / secs) * 100}%"></i></span> ${Math.ceil(secs - el)} s`
      : "Processing…";
  }, 250);
  renderLiveCal();
  try {
    await new Promise((r) => setTimeout(r, secs * 1000));
    const out = await calApi("api/calibration/record", {
      band: dev.band, device: dev.id, device_name: displayName(dev), device_type: dev.type,
      x: spot[0], y: spot[1], t0, seconds: secs,
      pos_sd_ft: radioOf ? 0.5 : parseFloat($("cal-acc").value),   // node radios sit at their (known) node
    });
    const point = out.point;
    const heard = Object.keys(point.rssi).length;
    point.warn = calQualityWarnings(point);
    $("cal-progress").textContent = `Recorded (${heard} nodes)`;
    $("cal-warn").hidden = !point.warn.length;
    $("cal-warn").innerHTML = point.warn.map(escapeHtml).join("<br>");
    await calLoad();
  } catch (err) {
    $("cal-progress").textContent = `Failed: ${err.message || err}`;
  } finally {
    clearInterval(tick);
    S.calib.recording = false;
    renderLiveCal();
  }
}

// Does this spot look like a moving, chatty device?
function calQualityWarnings(pt) {
  const warn = [];
  const rate = Math.max(...Object.values(pt.n || { x: 0 })) / (pt.seconds || calRecordS(pt.band));
  if (rate < (CAL_MIN_RATE[pt.band] || 0.4)) {
    warn.push(`Low rate: ${rate.toFixed(rate < 1 ? 2 : 1)} per s`);
  }
  const far = S.calib.points.filter((q) => q !== pt && q.device === pt.device && q.band === pt.band && Math.hypot(q.x - pt.x, q.y - pt.y) >= CAL_MOVE_FT);
  if (far.length) {
    const changed = far.some((q) => Object.keys(pt.rssi).some((k) => q.rssi[k] != null && Math.abs(q.rssi[k] - pt.rssi[k]) > CAL_MOVE_DB));
    if (!changed) {
      warn.push("RSSI unchanged from distant spots: device not moving?");
    }
  }
  return warn;
}

function median(v) {
  const s = [...v].sort((a, b) => a - b), m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

// Spots for a band (recorded ones; node radios can also be recorded like any device).
function calSpots(band) {
  return S.calib.points.filter((p) => p.band === band).map((p) => ({ ...p, kind: "recorded" }));
}

// Node links as training spots (Wi-Fi): each node radio at its node, heard by the
// other nodes (server: medians over 5 min, readings credited to the radio itself).
// They supersede hand-recorded spots of the same radios (calBest drops those).
function calLinks(band) {
  if (band !== "wifi" || !S.data || !S.data.node_links) return [];
  return (S.data.node_links.links || []).filter((l) => S.nodes[l.node]).map((l) => ({
    id: `link:${l.node}`, kind: "link", band, device: l.device, device_name: `${l.node} radio`,
    x: S.nodes[l.node][0], y: S.nodes[l.node][1], pos_sd_ft: calNodeSd(), rssi: l.rssi, n: l.n,
  }));
}

// Position accuracy (1 sd, ft) of a spot and of the nodes.
const calSpotSd = (p) => (p.pos_sd_ft != null ? p.pos_sd_ft : 2);
const calNodeSd = () => (S.data && S.data.layout.node_sd_ft != null ? S.data.layout.node_sd_ft : 2);
const LN10 = Math.log(10);

// RSSI variance a reading picks up from position uncertainty: the slope of the
// path-loss curve, 10 n / (ln10 d) dB per metre, times the position sd.
function calPosVar(n, dM, spotSdFt, nodeSdFt) {
  const g = (10 * Math.max(n, 0.5)) / (LN10 * Math.max(dM, 0.3));
  return g * g * ((spotSdFt * spotSdFt + nodeSdFt * nodeSdFt) / (FT_PER_M * FT_PER_M));
}

// One design row per (spot, node): features for
// rssi = P0[device] + c_k - 10 n log10(d_k) - W * E[walls_k].
function calRows(spots, cols) {
  const rows = [];
  for (const p of spots) {
    for (const [k, v] of Object.entries(p.rssi)) {
      if (!S.nodes[k]) continue;
      const dM = Math.max(Math.hypot(p.x - S.nodes[k][0], p.y - S.nodes[k][1]) / FT_PER_M, 0.3);
      const x = new Array(cols.P).fill(0);
      x[0] = -10 * Math.log10(dM);
      if (cols.dev[p.device] != null) x[cols.dev[p.device]] = 1;
      if (cols.off[k] != null) x[cols.off[k]] = 1;
      if (cols.wall != null) x[cols.wall] = -(S.walls.length ? wallsExpected(p.x, p.y, S.nodes[k][0], S.nodes[k][1]) : 0);
      rows.push({ x, y: v, dM, sd: calSpotSd(p), crossings: cols.wall != null ? -x[cols.wall] : 0 });
    }
  }
  return rows;
}

function calNormal(rows, cols, weights) {
  const P = cols.P, A = Array.from({ length: P }, () => new Array(P).fill(0)), b = new Array(P).fill(0);
  rows.forEach((r, i) => {
    const w = weights ? weights[i] : 1;
    for (let a = 0; a < P; a++) { if (!r.x[a]) continue; b[a] += w * r.x[a] * r.y; for (let c = 0; c < P; c++) A[a][c] += w * r.x[a] * r.x[c]; }
  });
  const offIdx = Object.values(cols.off);
  for (const a of offIdx) for (const c of offIdx) A[a][c] += 100;  // offsets average zero
  return [A, b];
}

// Weighted least squares. Weights = 1 / (sigma0^2 + position variance), with
// sigma0 (RSSI noise not explained by position uncertainty) estimated from the
// residuals; iterated because the position variance depends on n.
function calFit(spots, opts) {
  const withOffsets = !!opts.offsets;
  const nodes = [...new Set(spots.flatMap((p) => Object.keys(p.rssi)))].filter((k) => S.nodes[k]);
  const devices = [...new Set(spots.map((p) => p.device))];
  const cols = { dev: {}, off: {}, wall: null, P: 1 };
  devices.forEach((d) => { cols.dev[d] = cols.P++; });
  if (withOffsets) nodes.forEach((k) => { cols.off[k] = cols.P++; });
  if (opts.walls) cols.wall = cols.P++;
  let rows = calRows(spots, cols);
  if (opts.walls) {  // W is only identifiable when paths cross different (expected) numbers of walls
    const c = rows.map((r) => r.crossings), m = c.reduce((a, b) => a + b, 0) / c.length;
    if (!c.some((v) => v > 0.1) || c.reduce((a, v) => a + (v - m) ** 2, 0) / c.length < 0.05) return null;
  }
  const P = cols.P;
  if (rows.length < P + 2) return null;
  const xs = rows.map((r) => r.x[0]), mx = xs.reduce((a, b) => a + b, 0) / xs.length;
  if (xs.reduce((a, b) => a + (b - mx) ** 2, 0) / xs.length < 0.5) return null;
  const nodeSd = calNodeSd();
  let [A, b] = calNormal(rows, cols, null);
  let theta = solve(A, b);
  if (!theta) return null;
  let weights = null, sigma0sq = null;
  for (let it = 0; it < 3; it++) {
    const pv = rows.map((r) => calPosVar(theta[0], r.dM, r.sd, nodeSd));
    const res = rows.map((r) => r.y - r.x.reduce((acc, v, j) => acc + v * theta[j], 0));
    sigma0sq = Math.max(1, res.reduce((acc, e, i) => acc + e * e - pv[i], 0) / Math.max(1, rows.length - P));
    weights = pv.map((v) => 1 / (sigma0sq + v));
    [A, b] = calNormal(rows, cols, weights);
    const t = solve(A, b);
    if (!t) break;
    theta = t;
  }
  let sse = 0, chi2 = 0;
  rows.forEach((r, i) => { const e = r.y - r.x.reduce((acc, v, j) => acc + v * theta[j], 0); sse += e * e; chi2 += (weights ? weights[i] : 1) * e * e; });
  const dof = Math.max(1, rows.length - P), scale = Math.max(1, chi2 / dof);
  const se = (idx) => { const e = new Array(P).fill(0); e[idx] = 1; const col = solve(A, e); return col ? Math.sqrt(Math.max(0, col[idx] * scale)) : null; };
  const nSe = se(0), wallSe = cols.wall != null ? se(cols.wall) : null;
  const p0ByDevice = Object.fromEntries(devices.map((d) => [d, +theta[cols.dev[d]].toFixed(1)]));
  const offsets = {}, offsetSe = {};
  for (const [k, i] of Object.entries(cols.off)) { offsets[k] = +theta[i].toFixed(2); const v = se(i); offsetSe[k] = v == null ? null : +v.toFixed(2); }
  return {
    n: +theta[0].toFixed(2), nSe: nSe == null ? null : +nSe.toFixed(2),
    p0: +median(Object.values(p0ByDevice)).toFixed(1), p0ByDevice,
    sigma: +Math.sqrt(sse / dof).toFixed(2),                         // total scatter (used when locating)
    sigma0: +Math.sqrt(sigma0sq || sse / dof).toFixed(2),            // scatter not explained by position uncertainty
    wall: cols.wall != null ? +theta[cols.wall].toFixed(2) : 0, wallSe: wallSe == null ? null : +wallSe.toFixed(2),
    offsetsFitted: withOffsets, wallsFitted: cols.wall != null, txSpread: opts.txSpread || 8,
    offsets, offsetSe, measurements: rows.length, devices: devices.length,
  };
}

function solve(A, b) {
  const n = b.length, M = A.map((r, i) => [...r, b[i]]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (Math.abs(M[p][c]) < 1e-9) return null;
    [M[c], M[p]] = [M[p], M[c]];
    for (let r = 0; r < n; r++) {
      if (r === c) continue;
      const f = M[r][c] / M[c][c];
      for (let k = c; k <= n; k++) M[r][k] -= f * M[c][k];
    }
  }
  return M.map((r, i) => r[n] / r[i]);
}

// Locate one spot's RSSI vector with given model parameters (single shot, no smoothing).
function calLocate(rssi, fit, device) {
  const g = S.grid, ids = Object.keys(rssi).filter((k) => g.logd[k]);
  if (ids.length < 2) return null;
  const p0 = (fit.p0ByDevice && fit.p0ByDevice[device] != null) ? fit.p0ByDevice[device] : fit.p0;
  const K = ids.length, N = g.nx * g.ny, tenN = 10 * fit.n, div = K + txLambda(fit.sigma, fit.txSpread || 8);
  const r = ids.map((k) => rssi[k] - p0 - ((fit.offsets || {})[k] || 0)), L = ids.map((k) => g.logd[k]);
  const W = fit.wall || 0, Wk = ids.map((k) => (W && g.walls[k]) || null);
  let minC = Infinity, best = 0;
  const cost = new Float32Array(N);
  for (let c = 0; c < N; c++) {
    let s = 0, s2 = 0;
    for (let k = 0; k < K; k++) { const e = r[k] + tenN * L[k][c] + (Wk[k] ? W * Wk[k][c] : 0); s += e; s2 += e * e; }
    const v = s2 - (s * s) / div;
    cost[c] = v;
    if (v < minC) { minC = v; best = c; }
  }
  const inv = 1 / (2 * Math.max(fit.sigma, 1) ** 2), bi = best % g.nx, bj = Math.floor(best / g.nx);
  const ri = Math.ceil(LOCAL_MEAN_FT / g.cell);
  let ls = 0, lx = 0, ly = 0;
  for (let j = Math.max(0, bj - ri); j <= Math.min(g.ny - 1, bj + ri); j++) {
    for (let i = Math.max(0, bi - ri); i <= Math.min(g.nx - 1, bi + ri); i++) {
      const v = Math.exp(-(cost[j * g.nx + i] - minC) * inv);
      ls += v; lx += v * (g.x0 + (i + 0.5) * g.cell); ly += v * (g.y0 + (j + 0.5) * g.cell);
    }
  }
  return [lx / ls, ly / ls];
}

// Leave-one-out: fit without spot i (plus any extra training spots), locate spot i, measure the error.
function calLeaveOneOut(spots, opts, extra = []) {
  const out = [];
  spots.forEach((p, i) => {
    const fit = calFit(spots.filter((_, j) => j !== i).concat(extra), opts);
    if (!fit) return;
    const est = calLocate(p.rssi, fit, p.device);
    if (est) out.push({ id: p.id, x: est[0], y: est[1], err: Math.hypot(est[0] - p.x, est[1] - p.y) });
  });
  return out;
}

// All candidate models for a band, scored by leave-one-out error; cached per data.
function calBest(band) {
  const links = calLinks(band), linked = new Set(links.map((l) => l.device));
  const spots = calSpots(band).filter((p) => !linked.has(p.device));   // a node radio's link replaces its recorded spot
  const sig = JSON.stringify([band, spots.map((p) => [p.id, p.x, p.y, p.rssi]), links.map((p) => [p.id, p.rssi]), S.walls, S.nodes]);
  if (S.calib.best && S.calib.best[band] && S.calib.best[band].sig === sig) return S.calib.best[band];
  const dm = S.data.layout.models[band];
  const def = { n: dm.path_loss_exp, p0: dm.ref_rssi_1m, sigma: dm.sigma_db, offsets: {}, txSpread: 8,
    wall: S.walls.length ? (dm.wall_db || 0) : 0 };
  const scoreErrs = (locate) => spots.map((p) => { const e = locate(p); return e ? Math.hypot(e[0] - p.x, e[1] - p.y) : null; }).filter((v) => v != null);
  const defErrs = scoreErrs((p) => calLocate(p.rssi, def, p.device)).sort((a, b) => a - b);
  const candidates = [];
  for (const useLinks of links.length ? [false, true] : [false])
  for (const offsets of [false, true]) for (const walls of S.walls.length ? [false, true] : [false]) for (const txSpread of CAL_TX_SPREADS) {
    const opts = { offsets, walls, txSpread, links: useLinks };
    const extra = useLinks ? links : [];
    const fit = calFit(spots.concat(extra), opts);
    if (!fit) continue;
    fit.txSpread = txSpread;
    fit.nodeLinks = useLinks ? extra.length : 0;
    const loo = calLeaveOneOut(spots, opts, extra).map((l) => ({ ...l }));
    const errs = loo.map((l) => l.err).sort((a, b) => a - b);
    const problems = [];
    if (fit.n < CAL_N_RANGE[0] || fit.n > CAL_N_RANGE[1]) problems.push(`n = ${fit.n} outside ${CAL_N_RANGE[0]}–${CAL_N_RANGE[1]}`);
    if (fit.sigma > CAL_MAX_SIGMA) problems.push(`σ = ${fit.sigma} dB`);
    if (walls && fit.wall < 0) problems.push(`negative wall loss (${fit.wall} dB)`);
    candidates.push({ opts, fit, loo, errs, problems, median: errs.length ? median(errs) : null,
      params: 1 + fit.devices + (offsets ? S.nodeOrder.length : 0) + (walls ? 1 : 0) });
  }
  const ok = candidates.filter((c) => !c.problems.length && c.median != null && c.errs.length >= CAL_MIN_LOO);
  ok.sort((a, b) => a.median - b.median || a.params - b.params);
  const defMedian = defErrs.length ? median(defErrs) : null;
  const best = ok[0] || null;
  const adopted = !!(best && defMedian != null && best.median < defMedian);
  const reason = !spots.length ? "no spots"
    : !candidates.length ? "too few readings"
    : !ok.length ? "too few spots"
    : !adopted ? "fit not better"
    : null;
  // What the node links change: best error at the walked spots without and with them.
  const bestOf = (use) => ok.find((c) => !!c.opts.links === use);
  const linksEffect = links.length ? { links: links.length, without: bestOf(false)?.median ?? null, with: bestOf(true)?.median ?? null } : null;
  const res = { sig, band, spots, links, candidates, best, adopted, reason, defMedian, def, linksEffect };
  S.calib.best = { ...(S.calib.best || {}), [band]: res };
  return res;
}

// ---------- where to record next (experimental design) ----------
// For the full model (n, every node offset, wall loss if walls are drawn), the
// information matrix from the existing spots tells how well each parameter is
// known. A candidate place is scored by how much one reading there (all nodes
// hearing the selected device) would shrink the joint uncertainty of those
// parameters (log-determinant of their covariance; "Ds-optimal" design).
// Three targets are chosen greedily, each assuming the previous ones are done.
const CAL_GOALS = { n: 0.2, wall: 1.5, offset: 2 };   // ± targets for "well calibrated"

function logdet(M) {
  const n = M.length, A = M.map((r) => [...r]);
  let ld = 0;
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(A[r][c]) > Math.abs(A[p][c])) p = r;
    if (Math.abs(A[p][c]) < 1e-12) return -Infinity;
    [A[c], A[p]] = [A[p], A[c]];
    ld += Math.log(Math.abs(A[c][c]));
    for (let r = c + 1; r < n; r++) { const f = A[r][c] / A[c][c]; for (let k = c; k < n; k++) A[r][k] -= f * A[c][k]; }
  }
  return ld;
}

function invert(A) {
  const n = A.length, M = A.map((r, i) => [...r, ...r.map((_, j) => (i === j ? 1 : 0))]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (Math.abs(M[p][c]) < 1e-12) return null;
    [M[c], M[p]] = [M[p], M[c]];
    const d = M[c][c];
    for (let k = 0; k < 2 * n; k++) M[c][k] /= d;
    for (let r = 0; r < n; r++) { if (r === c) continue; const f = M[r][c]; if (f) for (let k = 0; k < 2 * n; k++) M[r][k] -= f * M[c][k]; }
  }
  return M.map((r) => r.slice(n));
}

function calSuggest(band) {
  const r = calBest(band), dev = calDev();
  const ref = dev && dev.band === band ? dev.id : "__next__";
  const sig = r.sig + "|" + ref;
  if (S.calib.suggest && S.calib.suggest.sig === sig) return S.calib.suggest;
  const fit = (r.best && r.best.fit) || null;
  const n = fit ? fit.n : r.def.n, sigma0 = fit ? fit.sigma0 : 6, nodeSd = calNodeSd();
  const cols = { dev: {}, off: {}, wall: null, P: 1 };
  const known = r.spots.concat(r.links || []);   // node links are real knowledge too
  for (const d of new Set([...known.map((p) => p.device), ref])) cols.dev[d] = cols.P++;
  for (const k of S.nodeOrder) cols.off[k] = cols.P++;
  if (S.walls.length) cols.wall = cols.P++;
  const interest = [0, ...Object.values(cols.off), ...(cols.wall != null ? [cols.wall] : [])];
  const rows = calRows(known, cols);
  const w = (row) => 1 / (sigma0 * sigma0 + calPosVar(n, row.dM, row.sd, nodeSd));
  let [A] = calNormal(rows, cols, rows.map(w));
  for (let i = 0; i < cols.P; i++) A[i][i] += 1e-4;   // weak prior: keeps the matrix invertible
  const covI = (M) => { const inv = invert(M); return inv ? interest.map((i) => interest.map((j) => inv[i][j])) : null; };
  const scale = fit ? Math.max(1, (fit.sigma * fit.sigma) / (sigma0 * sigma0)) : 1;
  const sdOf = (M) => { const inv = invert(M); return inv ? Object.fromEntries(interest.map((i) => [i, Math.sqrt(Math.max(0, inv[i][i] * scale))])) : {}; };
  const now = sdOf(A);
  const g = S.grid, xs = S.nodeOrder.map((k) => S.nodes[k][0]), ys = S.nodeOrder.map((k) => S.nodes[k][1]);
  const cands = [];
  for (let x = Math.min(...xs) - 4; x <= Math.max(...xs) + 4; x += 2) {
    for (let y = Math.min(...ys) - 4; y <= Math.max(...ys) + 4; y += 2) {
      if (x < g.x0 || x > g.x1 || y < g.y0 || y > g.y1) continue;
      if (S.walls.some((wl) => calDistToSeg(x, y, wl) < 1.5)) continue;    // not on a wall
      if (r.spots.some((p) => Math.hypot(p.x - x, p.y - y) < 5)) continue;   // not on an existing spot
      cands.push([x, y]);
    }
  }
  const targets = [];
  for (let t = 0; t < 3; t++) {
    const before = logdet(covI(A) || [[1]]);
    let best = null;
    for (const [x, y] of cands) {
      if (targets.some((q) => Math.hypot(q.x - x, q.y - y) < 8)) continue;
      const add = calRows([{ x, y, device: ref, rssi: Object.fromEntries(S.nodeOrder.map((k) => [k, -60])), pos_sd_ft: 2 }], cols);
      const B = A.map((rw) => [...rw]);
      for (const row of add) { const wt = w(row); for (let a = 0; a < cols.P; a++) if (row.x[a]) for (let c = 0; c < cols.P; c++) B[a][c] += wt * row.x[a] * row.x[c]; }
      const ci = covI(B);
      if (!ci) continue;
      const gain = before - logdet(ci);
      if (!best || gain > best.gain) best = { x, y, gain, B };
    }
    if (!best || !(best.gain > 0.01)) break;
    // reason: the parameter whose uncertainty shrinks the most (relative)
    const sdB = sdOf(best.B), sdA = sdOf(A);
    let why = null, most = 0;
    for (const i of interest) {
      const red = 1 - sdB[i] / sdA[i];
      if (red > most) { most = red; why = i; }
    }
    const node = Object.keys(cols.off).find((k) => cols.off[k] === why);
    const label = why === 0 ? "n" : why === cols.wall ? "wall loss" : `${node} offset`;
    targets.push({ x: best.x, y: best.y, letter: "ABC"[t], why: label, gain: best.gain });
    A = best.B;
  }
  const quality = {
    n: now[0],
    wall: cols.wall != null ? now[cols.wall] : null,
    offset: Math.max(...Object.values(cols.off).map((i) => now[i] || 0)),
  };
  const good = quality.n <= CAL_GOALS.n && (quality.wall == null || quality.wall <= CAL_GOALS.wall) && quality.offset <= CAL_GOALS.offset;
  S.calib.suggest = { sig, band, targets: good ? [] : targets, quality, good, ref };
  return S.calib.suggest;
}

function calDistToSeg(x, y, wl) {
  const vx = wl.x2 - wl.x1, vy = wl.y2 - wl.y1, L2 = vx * vx + vy * vy || 1;
  const t = Math.max(0, Math.min(1, ((x - wl.x1) * vx + (y - wl.y1) * vy) / L2));
  return Math.hypot(x - (wl.x1 + t * vx), y - (wl.y1 + t * vy));
}

// What the panel and map show for the selected band.
function calAnalyse() {
  const r = calBest(calBand());
  const c = r.best || r.candidates.slice().sort((a, b) => (a.median ?? 1e9) - (b.median ?? 1e9))[0] || null;
  const errs = c ? c.errs : [];
  S.calib.view = { band: r.band, spots: r.spots, fit: c && c.fit, loo: c ? c.loo : [], result: r,
    median: c ? c.median : null, defMedian: r.defMedian,
    p90: errs.length >= 10 ? errs[Math.min(errs.length - 1, Math.ceil(errs.length * 0.9) - 1)] : null,
    worst: errs.length ? errs[errs.length - 1] : null };
  return S.calib.view;
}

// The server selects and saves each band's model (console/calfit.py, the same
// maths as here); the page only recomputes the analysis it shows. Not while a
// node is dragged (that layout is only in this browser).
async function calAutoApply() {
  if (!S.data || !S.grid || !S.calib.loaded) return;
  try { calSuggest(S.band || "wifi"); } catch { /* suggestions are optional */ }
  renderLiveCal();
}

// Live model per band = saved fit, else the layout defaults.
function calUseFits() {
  for (const [band, m] of Object.entries(S.data.layout.models)) {
    const f = S.calib.fits[band];
    S.models[band] = f
      ? { n: f.n, p0: f.p0, sigma: f.sigma, wall: f.wall || 0, txSpread: f.txSpread || 8 }
      : { n: m.path_loss_exp, p0: m.ref_rssi_1m, sigma: m.sigma_db, wall: m.wall_db || 0, txSpread: 8 };
    S.offsets[band] = (f && f.offsets) || {};
  }
  S.version++;
  renderModelCard();
  refresh();
}

function renderLiveCal() {
  const on = S.calib.on;
  $("calib-card").hidden = !on;
  $("calibrate").classList.toggle("active", on);
  const sg = S.calib.suggest;
  $("calibrate").innerHTML = sg && sg.targets.length && !on ? `Calibrate <span class="badge">${sg.targets.length}</span>` : "Calibrate";
  if (S.data && S.data.calibration_version !== undefined && S.calib.version !== undefined &&
      S.data.calibration_version !== S.calib.version && !S.calib.loading) {
    S.calib.loading = true;  // spots or fits changed (any page): reload, even with the panel closed
    calLoad().finally(() => { S.calib.loading = false; });
  }
  if (!on || !S.data) return;
  const dev = calDev();
  $("cal-ref").innerHTML = dev ? `<b>${escapeHtml(displayName(dev))}</b>` : "select in list";
  const radioOf = dev && S.selfRadio[dev.id];
  $("cal-spot").textContent = radioOf ? radioOf : S.calib.spot ? `${S.calib.spot[0]}, ${S.calib.spot[1]} ft` : "click map";
  $("cal-record").disabled = !dev || !(S.calib.spot || radioOf) || S.calib.recording;
  $("cal-record").textContent = S.calib.recording ? "Recording…" : "Record";

  const e = dev && S.est.get(dev.id);
  let live = "";
  if (dev) {
    const heard = Object.keys(dev.nodes).length;
    live = `${heard} node${heard === 1 ? "" : "s"}`;
    if (e && e.kind === "fix" && S.calib.spot) live += ` · ${Math.hypot(e.x - S.calib.spot[0], e.y - S.calib.spot[1]).toFixed(0)} ft from spot`;
  }
  $("cal-live").innerHTML = live;

  const v = calAnalyse();
  calRenderSuggestions(v.band);
  const looById = Object.fromEntries(v.loo.map((l) => [l.id, l]));
  let num = 0;
  const rowsHtml = v.spots.map((p) => {
    const l = looById[p.id];
    const w = p.warn && p.warn.length ? ` <span title="${escapeHtml(p.warn.join(" "))}" style="color:#ffcf70">⚠</span>` : "";
    const actions = p.kind === "recorded"
      ? `${dev && dev.id !== p.device ? `<button data-reassign="${p.id}" title="Use selected device">↻</button>` : ""}<button data-del="${p.id}" title="Delete">×</button>`
      : "";
    const src = "";
    return `<tr><td>${p.kind === "recorded" ? ++num : "◇"}${w}</td><td title="${escapeHtml(p.device || "")}">${escapeHtml(calName(p))}${src}</td><td>${p.x.toFixed(1)}, ${p.y.toFixed(1)}</td><td>${Object.keys(p.rssi).length}</td>` +
      `<td>${l ? l.err.toFixed(0) + " ft" : "—"}</td><td class="nowrap">${actions}</td></tr>`;
  }).join("");
  // Collapsed by default; only the table inside is re-rendered, so an opened section stays open.
  $("cal-points-box").hidden = !v.spots.length;
  const flaggedSpots = v.spots.filter((p) => p.warn && p.warn.length).length;
  $("cal-points-sum").textContent = `Spots (${v.spots.length})${flaggedSpots ? ` · ${flaggedSpots} ⚠` : ""}`;
  $("cal-points").innerHTML = v.spots.length
    ? `<tr><th>#</th><th>device</th><th>spot</th><th>nodes</th><th>error</th><th></th></tr>${rowsHtml}` : "";
  $("cal-points").querySelectorAll("[data-del]").forEach((b) => {
    b.onclick = async () => { await calApi("api/calibration/points/delete", { ids: [b.dataset.del] }).catch(calErr); calLoad(); };
  });
  $("cal-points").querySelectorAll("[data-reassign]").forEach((b) => {
    b.onclick = async () => {
      const d = calDev();
      if (!d) return;
      $("cal-progress").textContent = "Rebuilding…";
      try {
        await calApi(`api/calibration/points/${b.dataset.reassign}/reassign`, { device: d.id, device_name: displayName(d), device_type: d.type, band: d.band });
        $("cal-progress").textContent = "Rebuilt";
      } catch (err) { calErr(err); }
      calLoad();
    };
  });

  // scoped delete: only the selected device's spots in this band, two clicks
  const mine = dev ? S.calib.points.filter((p) => p.band === v.band && p.device === dev.id) : [];
  const clear = $("cal-clear");
  clear.disabled = !mine.length;
  clear.hidden = !mine.length;
  if (!S.calib.confirmClear) clear.textContent = `Delete device's spots (${mine.length})`;

  const del = S.calib.deleted || [];
  // Collapsible sections are fixed elements; only their contents are re-rendered, so an open one stays open.
  $("cal-deleted-box").hidden = !del.length;
  $("cal-deleted-sum").textContent = `Deleted (${del.length})`;
  $("cal-deleted").innerHTML = del.slice(0, 30).map((p) =>
    `<div class="del-row"><span>${escapeHtml(calName(p))} · ${p.x.toFixed(1)}, ${p.y.toFixed(1)}</span><button data-restore="${p.id}" class="btn small">Restore</button></div>`).join("");
  $("cal-deleted").querySelectorAll("[data-restore]").forEach((b) => {
    b.onclick = async () => { await calApi("api/calibration/points/restore", { ids: [b.dataset.restore] }).catch(calErr); calLoad(); };
  });

  const box = $("cal-fit"), r = v.result;
  const describe = (o) => [o.offsets && "offsets", o.walls && "walls", `tx ±${o.txSpread}`, o.links && "node links"].filter(Boolean).join(", ");
  const flagged = v.spots.filter((p) => p.warn && p.warn.length).length;
  const head = r.adopted
    ? `Error <b>${r.best.median.toFixed(1)} ft</b> (default ${r.defMedian.toFixed(1)}) · ${r.best.errs.length} spots · ${describe(r.best.opts)}`
    : `Default model${r.spots.length ? ` · ${r.reason}` : ""}`;
  const rows = r.candidates.slice().sort((a, b) => (a.median ?? 1e9) - (b.median ?? 1e9)).map((c) =>
    `<tr class="${c === r.best && r.adopted ? "best" : ""}${c.problems.length ? " bad" : ""}"><td>${describe(c.opts)}</td><td>${c.fit.n}</td>` +
    `<td>${c.opts.walls ? c.fit.wall : "—"}</td><td>${c.fit.sigma}</td><td>${c.median != null ? c.median.toFixed(1) : "—"}</td></tr>`).join("");
  const le = r.linksEffect, ft = (v) => (v == null ? "—" : `${v.toFixed(1)} ft`);
  const linksLine = le ? `<div class="muted">Node links (${le.links}): ${ft(le.without)} without → ${ft(le.with)} with</div>` : "";
  box.innerHTML = head + (flagged ? ` · <span class="warn">${flagged} ⚠</span>` : "") + linksLine;
  $("cal-cands-box").hidden = !r.candidates.length;
  $("cal-cands-sum").textContent = `Candidates (${r.candidates.length})`;
  $("cal-cands").innerHTML = `<tr><th></th><th>n</th><th>wall</th><th>σ</th><th>ft</th></tr>${rows}` +
    `<tr><td>default</td><td>${r.def.n}</td><td>${r.def.wall || "—"}</td><td>${r.def.sigma}</td><td>${r.defMedian != null ? r.defMedian.toFixed(1) : "—"}</td></tr>`;
  drawCalScatter(v);
  calTrace();
}

function calRenderSuggestions(band) {
  const sg = calSuggest(band);
  $("cal-quality").innerHTML = sg.good ? '<span class="q ok">done</span>' : "";
  $("cal-suggest").innerHTML = sg.targets.length
    ? sg.targets.map((t) => `<div class="sg"><span class="letter">${t.letter}</span><span class="txt">${t.x}, ${t.y} ft</span>` +
        `<button class="btn small" data-go="${t.letter}">Go</button></div>`).join("")
    : "";
  $("cal-suggest").querySelectorAll("[data-go]").forEach((b) => {
    b.onclick = () => { const t = sg.targets.find((q2) => q2.letter === b.dataset.go); if (t) calSetSpot(t.x, t.y); };
  });
}

function calErr(err) { $("cal-progress").textContent = `Failed: ${err.message || err}`; }

// Live RSSI per node for the selected device over the last minute.
async function calTrace() {
  const dev = calDev();
  if (!dev || S.calib.traceBusy) return drawCalTrace();
  S.calib.traceBusy = true;
  try {
    const data = await calApi(`api/samples?id=${encodeURIComponent(dev.id)}&since=${S.data.now - 60}`);
    S.calib.trace = { id: dev.id, now: S.data.now, nodes: data.nodes || {} };
  } catch { /* keep the last trace */ } finally {
    S.calib.traceBusy = false;
  }
  drawCalTrace();
}

function calCanvas(id, h) {
  const cv = $(id), c = cv.getContext("2d"), dpr = window.devicePixelRatio || 1, w = cv.clientWidth;
  cv.width = w * dpr; cv.height = h * dpr; c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
  c.font = "10px system-ui";
  return [c, w];
}

function drawCalTrace() {
  const h = 90, [c, w] = calCanvas("cal-trace", h);
  const tr = S.calib.trace, dev = calDev();
  const pad = { l: 30, r: 6, t: 6, b: 12 }, yMin = -100, yMax = -20;
  const Y = (r) => pad.t + ((yMax - r) / (yMax - yMin)) * (h - pad.t - pad.b);
  c.fillStyle = "#6b7890"; c.strokeStyle = "#243048";
  for (let r = -100; r <= -20; r += 20) { c.beginPath(); c.moveTo(pad.l, Y(r)); c.lineTo(w - pad.r, Y(r)); c.stroke(); c.fillText(r, 2, Y(r) + 3); }
  if (!tr || !dev || tr.id !== dev.id) return;
  const X = (t) => pad.l + ((t - (tr.now - 60)) / 60) * (w - pad.l - pad.r);

  for (const [node, samples] of Object.entries(tr.nodes)) {
    c.strokeStyle = nodeColor(node); c.fillStyle = nodeColor(node); c.lineWidth = 1.5;
    c.beginPath();
    samples.forEach(([t, v], i) => (i ? c.lineTo(X(t), Y(v)) : c.moveTo(X(t), Y(v))));
    c.stroke();
    const last = samples[samples.length - 1];
    if (last) c.fillText(`${node} ${last[1]}`, Math.min(X(last[0]) + 3, w - 60), Y(last[1]) - 3);
  }
}

function drawCalScatter(v) {
  const h = 130, [c, w] = calCanvas("cal-scatter", h);
  const pad = { l: 30, r: 6, t: 6, b: 16 }, yMin = -100, yMax = -20;
  // With several devices, shift each to the common P0 so their slopes are comparable.
  const shift = (dev) => (v.fit && v.fit.p0ByDevice[dev] != null ? v.fit.p0 - v.fit.p0ByDevice[dev] : 0);
  const pts = v.spots.flatMap((p) => Object.entries(p.rssi).filter(([k]) => S.nodes[k])
    .map(([k, r]) => [k, Math.hypot(p.x - S.nodes[k][0], p.y - S.nodes[k][1]), r + shift(p.device)]));
  const dMax = Math.max(50, ...pts.map((q) => q[1])) * 1.05;
  const X = (d) => pad.l + (d / dMax) * (w - pad.l - pad.r);
  const Y = (r) => pad.t + ((yMax - Math.max(yMin, Math.min(yMax, r))) / (yMax - yMin)) * (h - pad.t - pad.b);
  c.fillStyle = "#6b7890"; c.strokeStyle = "#243048";
  for (let r = -100; r <= -20; r += 20) { c.beginPath(); c.moveTo(pad.l, Y(r)); c.lineTo(w - pad.r, Y(r)); c.stroke(); c.fillText(r, 2, Y(r) + 3); }
  for (let d = 0; d <= dMax; d += 10) c.fillText(`${d}ft`, X(d) - 7, h - 3);
  const curve = (m, color, dash) => {
    c.strokeStyle = color; c.setLineDash(dash); c.lineWidth = 1.5; c.beginPath();
    for (let i = 1; i <= 60; i++) { const d = (i / 60) * dMax, r = m.p0 - 10 * m.n * Math.log10(Math.max(d / FT_PER_M, 0.3)); i === 1 ? c.moveTo(X(d), Y(r)) : c.lineTo(X(d), Y(r)); }
    c.stroke(); c.setLineDash([]);
  };
  const dm = S.data.layout.models[v.band];
  curve({ n: dm.path_loss_exp, p0: dm.ref_rssi_1m }, "#6b7890", [3, 3]);
  if (v.fit) curve(v.fit, "#ffb347", []);
  for (const [k, d, r] of pts) { c.fillStyle = nodeColor(k); c.beginPath(); c.arc(X(d), Y(r), 3.5, 0, Math.PI * 2); c.fill(); }

}

function drawCal(ctx, toS, sc) {
  const v = S.calib.view;
  if (v) {
    const looById = Object.fromEntries(v.loo.map((l) => [l.id, l]));
    ctx.font = "600 11px system-ui"; ctx.textAlign = "center";
    let num = 0;
    v.spots.forEach((p) => {
      const [sx, sy] = toS(p.x, p.y);
      const l = looById[p.id];
      if (l) {
        const [ex, ey] = toS(l.x, l.y);
        ctx.strokeStyle = "rgba(255,93,115,.7)"; ctx.lineWidth = 1.5; ctx.setLineDash([3, 3]);
        ctx.beginPath(); ctx.moveTo(sx, sy); ctx.lineTo(ex, ey); ctx.stroke(); ctx.setLineDash([]);
        ctx.strokeStyle = "#ff5d73"; ctx.beginPath(); ctx.arc(ex, ey, 4, 0, Math.PI * 2); ctx.stroke();
      }
      if (p.kind !== "recorded") return;
      num++;
      ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(sx, sy, 8, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#0b0f17"; ctx.fillText(String(num), sx, sy + 4);
    });
    ctx.textAlign = "left";
  }
  for (const t of (S.calib.suggest && S.calib.suggest.band === calBand() ? S.calib.suggest.targets : [])) {
    const [tx, ty] = toS(t.x, t.y);
    ctx.strokeStyle = "rgba(255,179,71,.9)"; ctx.lineWidth = 2; ctx.setLineDash([5, 4]);
    ctx.beginPath(); ctx.arc(tx, ty, 13, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#ffb347"; ctx.font = "700 13px system-ui"; ctx.textAlign = "center";
    ctx.fillText(t.letter, tx, ty + 5); ctx.textAlign = "left";
  }
  if (S.calib.spot) {
    const [sx, sy] = toS(...S.calib.spot);
    ctx.strokeStyle = "#ffb347"; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(sx - 12, sy); ctx.lineTo(sx + 12, sy); ctx.moveTo(sx, sy - 12); ctx.lineTo(sx, sy + 12); ctx.stroke();
    ctx.beginPath(); ctx.arc(sx, sy, 7, 0, Math.PI * 2); ctx.stroke();

  }
}

$("calibrate").onclick = () => {
  S.calib.on = !S.calib.on;
  renderLiveCal();
  refresh();
};
$("cal-record").onclick = calRecord;
$("cal-rebuild").onclick = async () => {
  $("cal-progress").textContent = "Rebuilding…";
  try {
    const out = await calApi("api/calibration/rebuild", {});
    const gone = out.skipped.filter((s) => s.why.startsWith("its log")).length;
    $("cal-progress").textContent = `Rebuilt ${out.rebuilt.length}, unchanged ${out.unchanged.length}` +
      (out.skipped.length ? `, skipped ${out.skipped.length}${gone ? ` (${gone} without a log here)` : ""}` : "");
  } catch (err) { calErr(err); }
  calLoad();
};
$("cal-clear").onclick = async () => {
  const dev = calDev(), clear = $("cal-clear");
  if (!dev) return;
  const ids = S.calib.points.filter((p) => p.band === dev.band && p.device === dev.id).map((p) => p.id);
  if (!S.calib.confirmClear) {  // first click arms, second click deletes
    S.calib.confirmClear = true;
    clear.textContent = `Confirm (${ids.length})`;
    setTimeout(() => { S.calib.confirmClear = false; renderLiveCal(); }, 4000);
    return;
  }
  S.calib.confirmClear = false;
  await calApi("api/calibration/points/delete", { ids }).catch(calErr);
  calLoad();
};
calLoad();
