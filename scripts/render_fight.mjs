// Frame dumper for render_fight.sh: drives headless Chrome over the DevTools protocol (no npm deps).
// usage: node --experimental-websocket render_fight.mjs <fight.html[?motion=reduced]> <outdir> [first] [last]
// Writes <outdir>/f%05d.png for every frame (or frames first..last), <outdir>/audio.wav and
// <outdir>/stats.json ({frames, particles: [count per frame], fx: [{shake, tilt, pan, zoom, flash, bloom} per frame]}).
// FIGHT_STATS_ONLY=1 skips the screenshots (tests sweep effect state fast).
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const [target, outdir, first, last] = process.argv.slice(2);
const [html, query = ""] = target.split("?");
const statsOnly = process.env.FIGHT_STATS_ONLY === "1";
mkdirSync(outdir, { recursive: true });
const chrome = process.env.CHROME || "google-chrome";
const profile = mkdtempSync(join(tmpdir(), "fight-chrome-"));
const proc = spawn(chrome, ["--headless=new", "--no-sandbox", "--disable-gpu-sandbox", "--use-angle=swiftshader",
  "--enable-unsafe-swiftshader", "--hide-scrollbars", "--mute-audio", "--window-size=1280,720",
  `--user-data-dir=${profile}`, "--remote-debugging-port=0", "about:blank"], { stdio: ["ignore", "ignore", "pipe"], detached: true });
const port = await new Promise((ok, fail) => {
  proc.stderr.on("data", d => { const m = /ws:\/\/[^:]+:(\d+)\//.exec(String(d)); if (m) ok(m[1]); });
  proc.once("exit", () => fail(new Error("chrome exited")));
});
const page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find(t => t.type === "page");
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise(ok => ws.addEventListener("open", ok));
let id = 0;
const pending = new Map();
ws.addEventListener("message", m => { const r = JSON.parse(m.data); pending.get(r.id)?.(r); pending.delete(r.id); });
const send = (method, params = {}) => new Promise((ok, fail) => {
  pending.set(++id, r => (r.error ? fail(new Error(r.error.message)) : ok(r.result)));
  ws.send(JSON.stringify({ id, method, params }));
});
const evaluate = async expr => (await send("Runtime.evaluate", { expression: expr, returnByValue: true })).result.value;

try {
  await send("Emulation.setDeviceMetricsOverride", { width: 1280, height: 720, deviceScaleFactor: 1, mobile: false });
  await send("Page.navigate", { url: `${pathToFileURL(resolve(html))}${query && `?${query}`}#frame=0` });
  while (!(await evaluate("window.__ready === true"))) await new Promise(ok => setTimeout(ok, 50));
  const total = await evaluate("window.__frames");
  const particles = [], fx = [];
  const lo = Number(first ?? 0), hi = Math.min(Number(last ?? total - 1), total - 1);
  for (let f = lo; f <= hi; f++) {
    const s = await evaluate(`window.__draw(${f}), [window.__particles, window.__fx]`);
    particles.push(s[0]); fx.push(s[1]);
    if (statsOnly) continue;
    const { data } = await send("Page.captureScreenshot", { format: "png", clip: { x: 0, y: 0, width: 1280, height: 720, scale: 1 } });
    writeFileSync(join(outdir, `f${String(f - lo).padStart(5, "0")}.png`), Buffer.from(data, "base64"));
  }
  writeFileSync(join(outdir, "stats.json"), JSON.stringify({ frames: total, particles, fx }));
  writeFileSync(join(outdir, "audio.wav"), Buffer.from(await evaluate("window.__wav()"), "base64"));
} finally {
  ws.close();
  const exited = new Promise(ok => proc.once("exit", ok));
  process.kill(-proc.pid);  // the whole group: Chrome's helpers keep writing the profile otherwise
  await exited;
  await new Promise(ok => setTimeout(ok, 300));
  rmSync(profile, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 });
}
