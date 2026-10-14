"use strict";

// AIK-045 desktop GUI shell main process. Security settings come from the
// AIK-002 spike; renderer IPC goes only through the AIK-004 DesktopRouter.
const {app, BrowserWindow, ipcMain, protocol, session} = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const platform = require("./platform.cjs");
const {EngineSupervisor, EngineError} = require("./engine.cjs");
const {CHANNEL, BackendError, DesktopRouter, makeRendererHandler} = require("../../../src/aik_ipc/desktop.cjs");

const APP_URL = "aik://app/index.html";
const WORKSPACE_ID = "local"; // single local workspace until project selection (AIK-087)
const ROUTES = Object.freeze(["connections", "overview", "goals", "workers", "evidence", "usage", "settings"]);
const PRELOAD = path.resolve(__dirname, "..", "..", "..", "src", "aik_ipc", "preload.cjs");
const ENGINE_ENTRY = path.resolve(__dirname, "..", "engine", "aik_engine_entry.py");
const smoke = process.argv.includes("--smoke");
const reportArgument = process.argv.find((value) => value.startsWith("--report-dir="));
const reportDirectory = reportArgument ? path.resolve(reportArgument.slice("--report-dir=".length)) : null;
const started = performance.now();
const observed = new Map(); // smoke evidence: validated backend answers seen by main, never renderer claims
let window;
let engine;
let quitting = false;

if (smoke && !reportDirectory) {
  process.stderr.write("AIK_REPORT_DIRECTORY_REQUIRED\n");
  process.exit(2);
}
if (smoke) {
  fs.mkdirSync(reportDirectory, {recursive: true});
  app.setPath("userData", path.join(reportDirectory, "profile"));
  app.setPath("crashDumps", path.join(reportDirectory, "crash-dumps"));
}
app.enableSandbox();
protocol.registerSchemesAsPrivileged([{scheme: "aik", privileges: {standard: true, secure: true, supportFetchAPI: true}}]);

function localAssets(directory) {
  const mapping = new Map();
  function visit(current, prefix = "") {
    for (const item of fs.readdirSync(current, {withFileTypes: true})) {
      if (item.isSymbolicLink()) throw new Error("ASSET_LINK_REJECTED");
      const absolute = path.join(current, item.name);
      const relative = prefix + item.name;
      if (item.isDirectory()) visit(absolute, relative + "/");
      else if (item.isFile()) mapping.set("/" + relative, absolute);
    }
  }
  visit(directory);
  return mapping;
}

function installAssetProtocol() {
  const assets = localAssets(path.join(app.getAppPath(), "dist"));
  const types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"};
  protocol.handle("aik", (request) => {
    const url = new URL(request.url);
    if (request.method !== "GET" || url.hostname !== "app" || url.port || url.username || url.password || url.search) {
      return new Response(null, {status: 403});
    }
    const asset = assets.get(url.pathname);
    if (!asset) return new Response(null, {status: 404});
    return new Response(fs.readFileSync(asset), {headers: {
      "Content-Type": types[path.extname(asset)] ?? "application/octet-stream",
      "X-Content-Type-Options": "nosniff",
      "Cache-Control": "no-store"
    }});
  });
}

function launchEngine() {
  const selected = app.isPackaged ? platform.packagedEngine(process.resourcesPath)
    : platform.sourceEngine(process.env.AIK_DESKTOP_PYTHON, ENGINE_ENTRY);
  const args = [...selected.args];
  // Developer/installer configuration of the read model; never chosen by the renderer.
  const readmodel = process.env.AIK_DESKTOP_READMODEL, scope = process.env.AIK_DESKTOP_USER_SCOPE;
  if (readmodel || scope) {
    if (!readmodel || !path.isAbsolute(readmodel) || !scope) throw new EngineError("ENGINE_NOT_CONFIGURED");
    args.push("--readmodel", readmodel, "--user-scope", scope);
  }
  return {executable: selected.executable, args, env: platform.engineEnvironment(), spawnOptions: platform.spawnOptions()};
}

function backendError(error) {
  const code = error instanceof EngineError ? error.code : "";
  if (code === "ENGINE_INCOMPATIBLE") return new BackendError("BACKEND_INCOMPATIBLE");
  if (code === "ENGINE_REQUEST_TIMEOUT" || code === "ENGINE_START_TIMEOUT") return new BackendError("BACKEND_TIMEOUT");
  return new BackendError("BACKEND_UNAVAILABLE");
}

function reader(command) {
  return async () => {
    try {
      const result = await engine.request(command);
      if (smoke) observed.set(command, {ok: true, result});
      return result;
    } catch (error) {
      const failure = backendError(error);
      if (smoke) observed.set(command, {ok: false, code: failure.code});
      throw failure;
    }
  };
}

async function waitFor(check, deadlineMs = 30000) {
  const deadline = performance.now() + deadlineMs;
  while (performance.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("SMOKE_TIMEOUT");
}

function safeCode(error) {
  const code = error instanceof Error ? (error.code ?? error.message) : "";
  return /^[A-Z][A-Z0-9_]{1,63}$/.test(code) ? code : "SHELL_FAILURE";
}

function writeReport(report) {
  const destination = path.join(reportDirectory, "report.json");
  const temporary = path.join(reportDirectory, "report.json.tmp");
  fs.writeFileSync(temporary, JSON.stringify(report, null, 2) + "\n", {encoding: "utf8", flag: "wx"});
  fs.renameSync(temporary, destination);
}

function page(expression) {
  return window.webContents.executeJavaScript(expression);
}

async function runSmoke() {
  await waitFor(() => page("document.body.dataset.ready === '1'"));
  const rendererNodeAccess = await page("typeof window.require !== 'undefined' || typeof window.process !== 'undefined'");
  const bridgeKeys = await page("Object.keys(window.aikDesktop ?? {}).sort().join(',')");
  await waitFor(() => page("(document.getElementById('connection-bar')?.dataset.connection ?? 'connecting') !== 'connecting'"));
  const routes = [];
  for (const route of ROUTES) {
    await page(`window.location.hash = "#/${route}"`);
    await waitFor(() => page(`(() => { const view = document.getElementById("route-view");
      return view?.dataset.route === "${route}" && view?.dataset.loaded === "1" &&
        Boolean(document.getElementById("route-${route}")); })()`));
    const state = await page(`document.getElementById("route-${route}").dataset.state ?? null`);
    // Let the compositor paint the new route before capturing it.
    await page("new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))");
    const image = await window.webContents.capturePage();
    const file = `route-${route}.png`;
    fs.writeFileSync(path.join(reportDirectory, file), image.toPNG());
    routes.push({route, rendered: true, state, screenshot: file});
  }
  const connection = await page("document.getElementById('connection-bar').dataset.connection");
  const engineInfo = observed.get("engine.info");
  const failed = rendererNodeAccess || !window.isVisible() || routes.length !== ROUTES.length ||
    bridgeKeys !== "engineInfo,handshake,readmodelSources,status";
  // PASS needs real engine answers seen by main on this platform. "stale" still counts: it marks old
  // read model data (a cursor gap), which the shell must show, not a lost engine.
  const answered = engineInfo?.ok === true && observed.get("system.status")?.ok === true;
  const status = failed ? "FAIL" : answered && ["connected", "stale"].includes(connection) ? "PASS" : "BLOCKED";
  const metrics = app.getAppMetrics().find((metric) => metric.pid === window.webContents.getOSProcessId());
  writeReport({
    schema_version: 1,
    status,
    scope: "AIK-045 desktop GUI shell smoke",
    platform_family: platform.family(),
    electron_version: process.versions.electron,
    packaged: app.isPackaged,
    window_visible: window.isVisible(),
    renderer_node_access: rendererNodeAccess,
    renderer_bridge: bridgeKeys.split(","),
    connection,
    engine: engineInfo ?? null,
    system_status: observed.get("system.status") ?? null,
    readmodel_sources: observed.get("readmodel.sources") ?? null,
    routes,
    metrics: {
      elapsed_ms: Math.round(performance.now() - started),
      main_rss_bytes: process.memoryUsage().rss,
      renderer_working_set_kb: metrics?.memory?.workingSetSize ?? null
    },
    not_run: ["packaged frozen engine (AIK-057)", "vendor login and real VM/AGS data (AIK-044, AIK-046 onward)"]
  });
  await engine.close();
  app.exit(status === "PASS" ? 0 : status === "BLOCKED" ? 2 : 1);
}

async function startup() {
  installAssetProtocol();
  session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.webRequest.onBeforeRequest(
    {urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*", "file://*/*"]},
    (_details, callback) => callback({cancel: true})
  );
  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 880, minHeight: 640,
    show: false, backgroundColor: "#0c1716", title: "AI-Kubernetes", autoHideMenuBar: true,
    webPreferences: {
      preload: PRELOAD,
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false
    }
  });
  window.webContents.setWindowOpenHandler(() => ({action: "deny"}));
  // Hash routes change only the fragment; any real navigation is refused.
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  engine = new EngineSupervisor({launch: launchEngine});
  const router = new DesktopRouter({readers: {
    "system.status": reader("system.status"),
    "engine.info": reader("engine.info"),
    "readmodel.sources": reader("readmodel.sources")
  }});
  ipcMain.handle(CHANNEL, makeRendererHandler({router, getWindow: () => window,
    getWorkspaceId: () => WORKSPACE_ID, appUrl: APP_URL}));
  await window.loadURL(APP_URL);
  window.show();
  if (smoke) await runSmoke();
}

app.whenReady().then(startup).catch(async (error) => {
  if (smoke) {
    writeReport({schema_version: 1, status: "FAIL", scope: "AIK-045 desktop GUI shell smoke",
      code: safeCode(error), platform_family: platform.family()});
  } else process.stderr.write(safeCode(error) + "\n");
  if (engine) await engine.close();
  app.exit(1);
});
app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (quitting || !engine) return;
  event.preventDefault();
  quitting = true;
  engine.close().finally(() => app.exit(0));
});
