"use strict";

const {app, BrowserWindow, ipcMain, protocol, session} = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const {SidecarClient} = require("./sidecar.cjs");
const {CHANNEL, trustedSender, validateRequest, failure} = require("./protocol.cjs");
const APP_URL = "aik://app/index.html";
const smoke = process.argv.includes("--spike-smoke");
const reportArgument = process.argv.find((value) => value.startsWith("--report-dir="));
const reportDirectory = reportArgument ? path.resolve(reportArgument.slice("--report-dir=".length)) : null;
let window;
let client;
let quitting = false;
const observed = new Map();
const started = performance.now();

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
  const types = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"};
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

function startSidecar() {
  if (app.isPackaged) {
    if (process.platform !== "win32") throw new Error("WINDOWS_REQUIRED");
    return new SidecarClient({executable: path.join(process.resourcesPath, "engine", "aik-engine.exe")});
  }
  // This path is developer configuration, not a renderer or runtime command.
  const executable = process.env.AIK_SPIKE_PYTHON;
  if (!executable || !path.isAbsolute(executable)) throw new Error("AIK_SPIKE_PYTHON_REQUIRED");
  return new SidecarClient({
    executable,
    args: ["-I", path.join(app.getAppPath(), "python", "sidecar_entry.py")]
  });
}

async function waitFor(check, deadlineMs = 20000) {
  const deadline = performance.now() + deadlineMs;
  while (performance.now() < deadline) {
    if (await check()) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("SMOKE_TIMEOUT");
}

function safeCode(error) {
  const code = error instanceof Error ? error.message : "";
  return /^[A-Z][A-Z0-9_]{1,63}$/.test(code) ? code : "SPIKE_FAILURE";
}

function writeReport(report) {
  const destination = path.join(reportDirectory, "report.json");
  const temporary = path.join(reportDirectory, "report.json.tmp");
  fs.writeFileSync(temporary, JSON.stringify(report, null, 2) + "\n", {encoding: "utf8", flag: "wx"});
  fs.renameSync(temporary, destination);
}

async function runSmoke() {
  await waitFor(() => window.webContents.executeJavaScript("Boolean(document.getElementById('health-check'))"));
  const rendererNodeAccess = await window.webContents.executeJavaScript(
    "typeof window.require !== 'undefined' || typeof window.process !== 'undefined'"
  );
  const checks = {};
  for (const [name, command] of [["health", "health"], ["worker", "worker_probe"], ["vault", "vault_probe"]]) {
    await window.webContents.executeJavaScript("document.getElementById('" + name + "-check').click()");
    await waitFor(() => observed.has(command));
    const response = observed.get(command);
    checks[name] = response.ok ? response.result : {status: "FAIL", code: response.error.code};
    await waitFor(() => window.webContents.executeJavaScript(
      "document.getElementById('" + name + "-result').dataset.status === '" + checks[name].status + "'"
    ));
  }
  const statuses = Object.values(checks).map((result) => result.status);
  const state = statuses.includes("FAIL") || rendererNodeAccess || !window.isVisible() ? "FAIL" :
    statuses.includes("NOT_RUN") || process.platform !== "win32" ? "BLOCKED" : "PASS";
  const processMetrics = app.getAppMetrics();
  const rendererMetric = processMetrics.find((metric) => metric.pid === window.webContents.getOSProcessId());
  const report = {
    schema_version: 1,
    status: state,
    scope: "AIK-002 Windows foundation spike",
    platform: process.platform,
    window_visible: window.isVisible(),
    renderer_node_access: rendererNodeAccess,
    checks,
    metrics: {
      elapsed_ms: Math.round(performance.now() - started),
      main_rss_bytes: process.memoryUsage().rss,
      renderer_working_set_kb: rendererMetric?.memory?.workingSetSize ?? null
    },
    electron_version: process.versions.electron,
    packaged: app.isPackaged,
    product_integration: "NOT_RUN"
  };
  const screenshot = await window.webContents.capturePage();
  fs.writeFileSync(path.join(reportDirectory, "screenshot.png"), screenshot.toPNG());
  writeReport(report);
  await client.close();
  app.exit(state === "PASS" ? 0 : state === "BLOCKED" ? 2 : 1);
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
    width: 1120, height: 840, minWidth: 880, minHeight: 700,
    show: false, backgroundColor: "#0c1716", title: "AI-Kubernetes · Windows 기반 검증",
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      nodeIntegration: false, contextIsolation: true, sandbox: true,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false
    }
  });
  window.webContents.setWindowOpenHandler(() => ({action: "deny"}));
  window.webContents.on("will-navigate", (event) => event.preventDefault());
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());
  client = startSidecar();
  ipcMain.handle(CHANNEL, async (event, request) => {
    if (!trustedSender(event, window.webContents, APP_URL)) return failure(request, "UNTRUSTED_SENDER");
    const invalid = validateRequest(request);
    if (invalid) return failure(request, invalid);
    let response;
    try { response = await client.request(request); }
    catch (error) { response = failure(request, safeCode(error)); }
    // Smoke evidence comes from the validated backend response, never renderer-supplied results.
    if (smoke) observed.set(request.command, response);
    return response;
  });
  await window.loadURL(APP_URL);
  window.show();
  if (smoke) await runSmoke();
}

app.whenReady().then(startup).catch(async (error) => {
  if (smoke) {
    writeReport({schema_version: 1, status: "FAIL", scope: "AIK-002 Windows foundation spike",
      code: safeCode(error), platform: process.platform, product_integration: "NOT_RUN"});
  } else process.stderr.write(safeCode(error) + "\n");
  if (client) await client.close();
  app.exit(1);
});
app.on("window-all-closed", () => app.quit());
app.on("before-quit", (event) => {
  if (quitting || !client) return;
  event.preventDefault();
  quitting = true;
  client.close().finally(() => app.exit(0));
});
