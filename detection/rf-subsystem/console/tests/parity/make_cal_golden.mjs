// Runs the page's calibration code (static/calib.js, app.js, walls.js as of
// commit ae0412d, before model fitting moved to the server) on synthetic
// spots and writes cal_golden.json; console/calfit.py is tested against it in
// test_calfit.py. To regenerate (from detection/rf-subsystem/console):
//   for f in app calib walls; do git show ae0412d:detection/rf-subsystem/console/static/$f.js > /tmp/$f.js; done
//   node tests/parity/make_cal_golden.mjs /tmp > tests/parity/cal_golden.json
import fs from "node:fs";
import vm from "node:vm";

const dir = process.argv[2];
const src = (f) => fs.readFileSync(`${dir}/${f}.js`, "utf8");
const appSrc = src("app"), wallsSrc = src("walls"), calSrc = src("calib");
function extract(s, name) { const i0 = s.indexOf(`function ${name}(`); if (i0 < 0) throw new Error(name); let i = s.indexOf("{", i0), d = 0; for (; i < s.length; i++) { if (s[i] === "{") d++; else if (s[i] === "}" && --d === 0) break; } return s.slice(i0, i + 1); }
const consts = (s, ns) => ns.map((n) => s.match(new RegExp(`const ${n} = [^;]+;`))[0]).join("\n");
const code = [consts(appSrc, ["FT_PER_M", "LOCAL_MEAN_FT"]), consts(wallsSrc, ["WALL_SAMPLES", "WALL_SD_DEFAULT", "wallSampleCache"]),
  consts(calSrc, ["CAL_N_RANGE", "CAL_MAX_SIGMA", "CAL_MIN_LOO", "CAL_TX_SPREADS", "LN10"]),
  "const calSpotSd = (p) => (p.pos_sd_ft != null ? p.pos_sd_ft : 2); const calNodeSd = () => (S.data && S.data.layout.node_sd_ft != null ? S.data.layout.node_sd_ft : 2);",
  ...["segCross", "wallsCrossed", "seededRandom", "wallSamples", "wallsExpected"].map((n) => extract(wallsSrc, n)),
  ...["buildGrid", "txLambda"].map((n) => extract(appSrc, n)),
  ...["median", "calSpots", "calLinks", "calPosVar", "calRows", "calNormal", "calFit", "solve", "calLocate", "calLeaveOneOut", "calBest"].map((n) => extract(calSrc, n)),
  "this.api = { buildGrid, calBest, wallsExpected };"].join("\n");

// deterministic synthetic data from a known model
let seed = 12345;
const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const gauss = () => Math.sqrt(-2 * Math.log(1 - rnd())) * Math.cos(2 * Math.PI * rnd());
const nodes = { "wbt-1": [0, 0], "wbt-2": [30, 0], "wbt-3": [0, 40], "wbt-4": [30, 40] };
const walls = [{ x1: 15, y1: -2, x2: 15, y2: 18, sd_ft: 1 }, { x1: -2, y1: 20, x2: 32, y2: 20, sd_ft: 2 }, { x1: 8, y1: 28, x2: 22, y2: 34, sd_ft: 0 }];
const truth = { n: 2.4, wall: 4, off: { "wbt-1": 3, "wbt-2": -2, "wbt-3": 0, "wbt-4": -1 } };
const models = { wifi: { path_loss_exp: 2.7, ref_rssi_1m: -40, sigma_db: 6, wall_db: 4 }, bt: { path_loss_exp: 2.5, ref_rssi_1m: -59, sigma_db: 6, wall_db: 4 },
                 tpms: { path_loss_exp: 2.2, ref_rssi_1m: -45, sigma_db: 7, wall_db: 3 } };
let ctx;
function reading(x, y, k, p0, noise) {
  const [nx, ny] = nodes[k], dM = Math.max(Math.hypot(x - nx, y - ny) / 3.28084, 0.3);
  const w = ctx.api.wallsExpected(x, y, nx, ny);
  return Math.round(p0 + truth.off[k] - 10 * truth.n * Math.log10(dM) - truth.wall * w + noise * gauss());
}
const S = { nodes, nodeOrder: Object.keys(nodes), walls, drag: null, version: 0, calib: { points: [], best: null },
  data: { layout: { models, node_sd_ft: 1.5 }, node_links: { links: [] } } };
ctx = { S, Math, Float32Array, Map, Set, Object, JSON, Infinity, Array, Number };
vm.createContext(ctx); vm.runInContext(code, ctx); ctx.api.buildGrid();

const spots = [];
const devs = { wifi: [["aa:00:00:00:00:01", -45], ["aa:00:00:00:00:02", -38]], bt: [["bb:00:00:00:00:01", -60]] };
let id = 0;
for (const band of ["wifi", "bt"]) {
  const count = band === "wifi" ? 12 : 7;
  for (let i = 0; i < count; i++) {
    const [dev, p0] = devs[band][i % devs[band].length];
    const x = Math.round((rnd() * 36 - 3) * 2) / 2, y = Math.round((rnd() * 46 - 3) * 2) / 2;
    const rssi = {};
    for (const k of Object.keys(nodes)) { const r = reading(x, y, k, p0, band === "wifi" ? 2.5 : 4); if (r > -95) rssi[k] = r; }
    spots.push({ id: `s${id++}`, band, device: dev, x, y, rssi, pos_sd_ft: [0.5, 2, 5][i % 3] });
  }
}
const radios = { "wbt-1": "dc:00:00:00:00:01", "wbt-2": "dc:00:00:00:00:02", "wbt-3": "dc:00:00:00:00:03", "wbt-4": "dc:00:00:00:00:04" };
const links = Object.entries(radios).map(([node, mac]) => ({ node, device: mac, x: nodes[node][0], y: nodes[node][1],
  rssi: Object.fromEntries(Object.keys(nodes).filter((k) => k !== node).map((k) => [k, reading(nodes[node][0], nodes[node][1], k, -42, 1.5)])) }));
S.calib.points = spots;
S.data.node_links.links = links;

const out = { input: { nodes, walls, models, node_sd_ft: 1.5, spots, links }, output: {} };
for (const band of ["wifi", "bt"]) {
  const r = ctx.api.calBest(band);
  out.output[band] = {
    defMedian: r.defMedian, adopted: r.adopted, reason: r.reason, best: r.best ? r.candidates.indexOf(r.best) : null,
    spots: r.spots.map((p) => p.id), links: (r.links || []).map((p) => p.id),
    candidates: r.candidates.map((c) => ({ opts: c.opts, fit: c.fit, errs: c.errs, median: c.median, problems: c.problems, params: c.params })),
  };
}
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
