// Screenshots for console/USER_GUIDE.md, taken from a DEMO console (synthetic
// devices, example layout): never point this at a real site.
//
//   1. Start a demo console (its own empty data; pick any throwaway admin password):
//        docker run -d --name rf-console-demo -e CONSOLE_DEMO=only -e CONSOLE_DATA=/tmp/demo \
//          -e CONSOLE_ADMIN_PASSWORD="$DEMO_PW" -e CONSOLE_OPERATOR_PASSWORD="$DEMO_PW-op" \
//          -p 127.0.0.1:18080:8080 black-relay/rf-console:dev
//      (on another host, forward it: ssh -N -L 18080:127.0.0.1:18080 <host>)
//   2. From detection/rf-subsystem:
//        SCREENSHOT_PASSWORD="$DEMO_PW" node console/docs/screenshots.mjs
//   3. docker rm -f rf-console-demo
//
// Options: --url (default http://localhost:18080), --out (default console/docs/img),
// --chrome (default: Google Chrome on macOS, else google-chrome / chromium on PATH).
// No npm packages: drives Chrome over the DevTools protocol.

import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const arg = (name, def) => { const i = process.argv.indexOf(`--${name}`); return i > 0 ? process.argv[i + 1] : def; };
const URL_ = arg("url", "http://localhost:18080");
const OUT = arg("out", "console/docs/img");
const CHROME = arg("chrome", process.platform === "darwin"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" : "google-chrome");
const PASSWORD = process.env.SCREENSHOT_PASSWORD || "";
const PORT = 9333;
const W = 1440, H = 900;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

fs.mkdirSync(OUT, { recursive: true });
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "rf-shots-"));
const chrome = spawn(CHROME, ["--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  `--window-size=${W},${H}`, "--hide-scrollbars", "--force-color-profile=srgb", "about:blank"], { stdio: "ignore" });

let ws, nextId = 1;
const pending = new Map();
function send(method, params = {}) {
  const id = nextId++;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
}
async function js(expr) {
  const r = await send("Runtime.evaluate", { expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}
async function shot(name, selector) {
  let clip;
  if (selector) {
    const b = await js(`const e = document.querySelector(${JSON.stringify(selector)}); if (!e || e.hidden) return null;
      const r = e.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height };`);
    if (!b) throw new Error(`${name}: ${selector} not visible`);
    const pad = 6;
    clip = { x: Math.max(0, b.x - pad), y: Math.max(0, b.y - pad), width: b.width + 2 * pad, height: b.height + 2 * pad, scale: 1 };
  }
  const r = await send("Page.captureScreenshot", { format: "png", ...(clip ? { clip } : {}) });
  fs.writeFileSync(path.join(OUT, `${name}.png`), Buffer.from(r.data, "base64"));
  console.log(`wrote ${path.join(OUT, name)}.png`);
}
async function load() {
  await send("Page.navigate", { url: URL_ });
  await sleep(4000);
}

try {
  let targets;
  for (let i = 0; i < 50 && !targets; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); } catch { await sleep(200); }
  }
  const page = targets.find((t) => t.type === "page");
  ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((r) => ws.addEventListener("open", r));
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.reject(new Error(m.error.message)) : p.resolve(m.result); }
  });
  await send("Page.enable");
  await send("Emulation.setDeviceMetricsOverride", { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  // Sign-in page first (before signing in)
  await load();
  if (await js(`for (let i = 0; i < 50 && document.getElementById("login").hidden; i++) await new Promise((r) => setTimeout(r, 100));
    return !document.getElementById("login").hidden`)) await shot("sign-in");

  if (PASSWORD) await js(`await fetch("api/login", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: ${JSON.stringify(PASSWORD)} }) });`);
  // Demo content: an alert area, a priority device and a linger alarm, so alerts and marks show.
  await js(`const post = (p, b) => fetch(p, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) });
    const st = await (await fetch("api/state")).json();
    if (!(st.areas || []).length) await post("api/areas/add", { name: "Entrance", points: [[2, 4], [18, 4], [18, 36], [2, 36]], linger_s: 120 });
    await post("api/marks", { device: "a4:de:00:00:00:01", kind: "priority", scope: "site" });
    await post("api/marks", { device: "a4:de:00:00:00:03", kind: "friendly", scope: "team" });
    await post("api/marks", { device: "a4:de:00:00:00:02", linger_s: 900, scope: "site" });`);
  await js(`localStorage.clear(); return true;`);
  await load();
  await sleep(10000);                                   // positions settle, alerts arrive

  await js(`S.hideTruth = true; setMode("live"); select(null); return true;`);   // demo-only true-position markers off
  await sleep(1500);
  await shot("overview");
  await shot("alerts", "#alerts-card");

  await js(`select("a4:de:00:00:00:02"); return true;`);
  await sleep(2500);
  await shot("device");
  await shot("device-card", "#selected-card");

  await js(`select(null); setMode("setup"); document.getElementById("nodes-btn").click(); return true;`);
  await sleep(1500);
  await shot("setup-nodes");
  await js(`document.getElementById("walls-btn").click(); return true;`);
  await sleep(1000);
  await shot("setup-walls");
  await js(`document.getElementById("areas-btn").click(); return true;`);
  await sleep(1000);
  await shot("setup-areas");
  await js(`document.getElementById("calibrate").click(); select("a4:de:00:00:00:04"); return true;`);
  await sleep(2500);
  await shot("calibrate");
  await shot("calibrate-panel", "#calib-card");

  await js(`setMode("live"); select(null); document.getElementById("site-btn").click();
    for (let i = 0; i < 50 && !document.querySelector("#site-menu .msec"); i++) await new Promise((r) => setTimeout(r, 100));
    return true;`);
  await sleep(500);
  await shot("site-menu");
} finally {
  try { ws && ws.close(); } catch { /* closing */ }
  const exited = new Promise((r) => chrome.once("exit", r));
  chrome.kill();
  await Promise.race([exited, sleep(5000)]);
  fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
}
