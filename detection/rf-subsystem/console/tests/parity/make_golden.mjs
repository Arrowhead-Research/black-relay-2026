// Runs the page's former localization code (static/app.js + walls.js as of
// commit 297b5a7, before positions moved to the server) on fixed inputs and
// writes golden.json; the server's port (locate.py) is tested against it in
// test_locate.py. To regenerate (from detection/rf-subsystem/console):
//   git show 297b5a7:detection/rf-subsystem/console/static/app.js > /tmp/app.js
//   git show 297b5a7:detection/rf-subsystem/console/static/walls.js > /tmp/walls.js
//   node tests/parity/make_golden.mjs /tmp/app.js /tmp/walls.js > tests/parity/golden.json
import fs from "node:fs";
import vm from "node:vm";

const [appSrc, wallsSrc] = process.argv.slice(2).map((p) => fs.readFileSync(p, "utf8"));
function extract(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`missing ${name}`);
  let i = src.indexOf("{", start), depth = 0;
  for (; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) break;
  }
  return src.slice(start, i + 1);
}
const consts = (src, names) => names.map((n) => src.match(new RegExp(`const ${n} = [^;]+;`))[0]).join("\n");
const code = [
  consts(appSrc, ["FT_PER_M", "TAU_SCALE", "LOCAL_MEAN_FT"]),
  consts(wallsSrc, ["WALL_SAMPLES", "WALL_SD_DEFAULT", "wallSampleCache"]),
  ...["segCross", "wallsCrossed", "seededRandom", "wallSamples", "wallsExpected"].map((n) => extract(wallsSrc, n)),
  ...["buildGrid", "costSurface", "txLambda", "filtered", "localize", "proximity"].map((n) => extract(appSrc, n)),
  "this.api = { buildGrid, localize, wallsExpected, S };",
].join("\n");

function run(c) {
  const S = { nodes: c.nodes, walls: c.walls, models: c.models, offsets: c.offsets, model: { minNodes: 2, tau: 15 },
              version: 0, filt: new Map(), drag: null };
  const ctx = { S, Math, Float32Array, Map, Object, Infinity };
  vm.createContext(ctx);
  vm.runInContext(code, ctx);
  ctx.api.buildGrid();
  const g = S.grid;
  const out = { grid: { x0: g.x0, y0: g.y0, x1: g.x1, y1: g.y1, cell: g.cell, nx: g.nx, ny: g.ny }, steps: [] };
  // a few wall-expectation probes (exact numbers every page computes)
  out.walls_probe = c.walls.length ? c.probes.map(([ax, ay, bx, by]) => ctx.api.wallsExpected(ax, ay, bx, by)) : [];
  for (const step of c.steps) {
    out.steps.push(step.map((dev) => {
      const e = ctx.api.localize(dev);
      return e && { kind: e.kind, x: e.x, y: e.y, K: e.K, ids: e.ids, offset: e.offset, res: e.res, updated: e.updated };
    }));
  }
  return out;
}

const nodes = { "wbt-1": [0, 0], "wbt-2": [20, 0], "wbt-3": [20, 30], "wbt-4": [0, 30] };
const walls = [{ x1: 10, y1: -2, x2: 10, y2: 14 }, { x1: 0, y1: 15, x2: 20, y2: 15, sd_ft: 1 },
               { x1: 4.5, y1: 22, x2: 14, y2: 27.5, sd_ft: 0 }, { x1: 15, y1: 5, x2: 25, y2: 5, sd_ft: 3 }];
const models = { wifi: { n: 2.7, p0: -40, sigma: 6, wall: 0, txSpread: 8 },
                 bt: { n: 2.3, p0: -62, sigma: 5, wall: 4.5, txSpread: 4 },
                 tpms: { n: 2.2, p0: -45, sigma: 7, wall: 3, txSpread: 16 } };
const node = (rssi, last) => ({ rssi, last });
const devs = (t, shift) => [
  { id: "aa:00:00:00:00:01", type: "wifi_ap", band: "wifi", nodes: { "wbt-1": node(-48 + shift, t), "wbt-2": node(-61, t), "wbt-3": node(-70, t - 1), "wbt-4": node(-66, t) } },
  { id: "aa:00:00:00:00:02", type: "wifi_client", band: "wifi", nodes: { "wbt-3": node(-55, t), "wbt-2": node(-72 - shift, t) } },
  { id: "bb:00:00:00:00:03", type: "ble", band: "bt", nodes: { "wbt-2": node(-70, t), "wbt-4": node(-82 + shift, t), "wbt-1": node(-77, t) } },
  { id: "tpms:1a2b3c4d", type: "tpms", band: "tpms", nodes: { "wbt-4": node(-60, t), "wbt-3": node(-58 - shift, t), "wbt-1": node(-75, t) } },
  { id: "aa:00:00:00:00:05", type: "wifi_ap", band: "wifi", nodes: { "wbt-1": node(-50, t) } },
];
const steps = [devs(1000, 0), devs(1000, 0), devs(1004, 3), devs(1009, -2), devs(1030, 6)];
const probes = [[0, 0, 20, 30], [20, 0, 0, 30], [0, 30, 12, 3], [20, 30, 9, 9.5], [3, 3, 4, 4]];
const cases = {
  no_walls: { nodes, walls: [], models: { ...models, bt: { ...models.bt, wall: 0 } }, offsets: { wifi: {}, bt: {}, tpms: {} }, steps, probes },
  walls_offsets: { nodes, walls, models, offsets: { wifi: { "wbt-1": 2.5, "wbt-3": -1 }, bt: { "wbt-2": -3 }, tpms: {} }, steps, probes },
};
const out = {};
for (const [k, c] of Object.entries(cases)) out[k] = { input: { nodes: c.nodes, walls: c.walls, models: c.models, offsets: c.offsets, steps: c.steps, probes: c.probes }, output: run(c) };
process.stdout.write(JSON.stringify(out, null, 1) + "\n");
