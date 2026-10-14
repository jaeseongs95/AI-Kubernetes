"use strict";

// AIK-045 desktop GUI shell main process. Security settings come from the
// AIK-002 spike; renderer IPC goes only through the AIK-004 DesktopRouter.
// AIK-046/047: connection commands go through the same router; API keys are typed
// only into the main-owned key entry window (./key-entry.cjs) and go from there
// straight to the engine.
// AIK-087: project folders are picked only in the main-owned native folder dialog
// (./folder-picker.cjs); the chosen path goes from main straight to the engine.
const {app, BrowserWindow, dialog, ipcMain, protocol, session} = require("electron");
const fs = require("node:fs");
const path = require("node:path");
const platform = require("./platform.cjs");
const {EngineSupervisor, EngineError} = require("./engine.cjs");
const {CHANNEL, READ_COMMANDS, WRITE_COMMANDS, BackendError, DesktopRouter, makeRendererHandler} =
  require("../../../src/aik_ipc/desktop.cjs");
const {KEY_CHANNEL, KeyEntry, KeyEntryError} = require("./key-entry.cjs");
const {FolderPicker, FolderPickerError} = require("./folder-picker.cjs");

const APP_URL = "aik://app/index.html";
// IPC session scope of this app window. The selected project folder (AIK-087) is engine data
// (workspaces.select), not this routing scope; connections stay in this scope.
const WORKSPACE_ID = "local";
const ROUTES = Object.freeze(["connections", "overview", "goals", "workers", "evidence", "usage", "settings"]);
const PRELOAD = path.resolve(__dirname, "..", "..", "..", "src", "aik_ipc", "preload.cjs");
const KEY_PRELOAD = path.resolve(__dirname, "..", "..", "..", "src", "aik_ipc", "key_entry_preload.cjs");
const KEY_ENTRY_TIMEOUT_MS = 1800000;
const FOLDER_DIALOG_TIMEOUT_MS = 1800000;
const SLOW_TIMEOUT_MS = 155000; // a connection command may queue behind a vendor check in the engine
const ENGINE_ENTRY = path.resolve(__dirname, "..", "engine", "aik_engine_entry.py");
const smoke = process.argv.includes("--smoke");
const reportArgument = process.argv.find((value) => value.startsWith("--report-dir="));
const reportDirectory = reportArgument ? path.resolve(reportArgument.slice("--report-dir=".length)) : null;
const started = performance.now();
const observed = new Map(); // smoke evidence: validated backend answers seen by main, never renderer claims
let window;
let engine;
let keyEntry;
let folderPicker;
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

function engineFailure(error) {
  // An engine answer with its own fixed code keeps that code; the router drops unknown ones.
  if (error instanceof EngineError && error.code === "ENGINE_REJECTED" && typeof error.detail === "string") {
    return new BackendError(error.detail);
  }
  return backendError(error);
}

function reader(command) {
  return async (_workspaceId, context) => {
    try {
      const result = await engine.request(command, {...(context?.payload ?? {})});
      if (smoke) observed.set(command, {ok: true, result});
      return result;
    } catch (error) {
      const failure = engineFailure(error);
      if (smoke) observed.set(command, {ok: false, code: failure.code});
      throw failure;
    }
  };
}

async function keyEntryContext(payload) {
  if (payload.mode === "add") {
    const catalog = await engine.request("connections.catalog");
    const connector = (catalog?.connectors ?? []).find((item) =>
      item?.provider === payload.provider && item?.execution_kind === "api");
    if (!connector) throw new BackendError("UNSUPPORTED_PROVIDER");
    return {mode: "add", provider: payload.provider, account_label: payload.account_label,
      display_name: String(connector.display_name), connection_id: null, expected_revision: null};
  }
  const listed = await engine.request("connections.list");
  const connection = (listed?.connections ?? []).find((item) => item?.connection_id === payload.connection_id);
  if (!connection) throw new BackendError("UNKNOWN_CONNECTION");
  if (connection.execution_kind !== "api") throw new BackendError("WRONG_METHOD");
  return {mode: "rotate", provider: null, account_label: String(connection.account_label),
    display_name: String(connection.display_name), connection_id: payload.connection_id,
    expected_revision: payload.expected_revision};
}

async function requestKeyEntry(_workspaceId, context) {
  try {
    const entryContext = await keyEntryContext(context.payload);
    return await keyEntry.open(entryContext, context.signal);
  } catch (error) {
    if (error instanceof BackendError) throw error;
    if (error instanceof KeyEntryError) throw new BackendError(error.code);
    throw engineFailure(error);
  }
}

async function submitKey(context, secret) {
  const payload = context.mode === "add"
    ? {provider: context.provider, account_label: context.account_label, secret}
    : {connection_id: context.connection_id, expected_revision: context.expected_revision, secret};
  try {
    return await engine.request(context.mode === "add" ? "connections.add_key" : "connections.rotate_key", payload);
  } catch (error) {
    throw engineFailure(error);
  } finally {
    payload.secret = null;
  }
}

async function requestFolder(_workspaceId, context) {
  try {
    return await folderPicker.pick(context.payload, context.signal);
  } catch (error) {
    if (error instanceof FolderPickerError) throw new BackendError(error.code);
    throw engineFailure(error);
  }
}

function createKeyWindow() {
  const keyWindow = new BrowserWindow({
    parent: window, modal: true, width: 520, height: 320, resizable: false, minimizable: false, maximizable: false,
    show: true, backgroundColor: "#0c1716", title: "API 키 입력", autoHideMenuBar: true,
    webPreferences: {
      preload: KEY_PRELOAD,
      nodeIntegration: false, contextIsolation: true, sandbox: true, spellcheck: false, devTools: false,
      webSecurity: true, allowRunningInsecureContent: false, webviewTag: false
    }
  });
  keyWindow.setContentProtection(true); // keep the key out of screen capture where the OS supports it
  keyWindow.webContents.setWindowOpenHandler(() => ({action: "deny"}));
  keyWindow.webContents.on("will-navigate", (event) => event.preventDefault());
  keyWindow.webContents.on("will-attach-webview", (event) => event.preventDefault());
  return keyWindow;
}

function handlers() {
  const result = {};
  for (const command of [...READ_COMMANDS, ...WRITE_COMMANDS]) {
    result[command] = command === "connections.request_key_entry" ? requestKeyEntry
      : command === "workspaces.request_folder" ? requestFolder : reader(command);
  }
  return result;
}

function timeouts() {
  const result = {"connections.request_key_entry": KEY_ENTRY_TIMEOUT_MS,
    "workspaces.request_folder": FOLDER_DIALOG_TIMEOUT_MS};
  for (const command of [...READ_COMMANDS, ...WRITE_COMMANDS]) {
    if ((command.startsWith("connections.") || command.startsWith("workspaces.")) && !Object.hasOwn(result, command)) {
      result[command] = SLOW_TIMEOUT_MS;
    }
  }
  return result;
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

const BRIDGE_KEYS = ["archiveWorkspace", "cancelLogin", "checkConnection", "completeLogin", "connectionCatalog",
  "connectionImpact", "connections", "deleteConnection", "engineInfo", "handshake", "loginState", "logoutConnection",
  "readmodelSources", "renameConnection", "requestFolder", "requestKeyEntry", "restoreWorkspace", "selectWorkspace",
  "startLogin", "status", "workspaceScope", "workspaces"].join(",");

async function probeKeyEntry() {
  // Opens the key entry window from the renderer bridge, checks its isolation, captures it, then cancels.
  // No key is typed. NOT_RUN when the engine offers no API-key connector.
  let catalog;
  try { catalog = await engine.request("connections.catalog"); }
  catch { return {status: "NOT_RUN", reason: "catalog_unavailable"}; }
  const connector = (catalog?.connectors ?? []).find((item) => item?.execution_kind === "api");
  if (!connector) return {status: "NOT_RUN", reason: "no_api_connector"};
  await page(`window.__aikKeyProbe = window.aikDesktop.requestKeyEntry("${WORKSPACE_ID}", "add",
    ${JSON.stringify(connector.provider)}, "smoke probe", null, null); true`); // do not await the open window
  await waitFor(() => Boolean(keyEntry.current) && !keyEntry.current.window.webContents.isLoading());
  const keyWindow = keyEntry.current.window;
  const keyPage = (expression) => keyWindow.webContents.executeJavaScript(expression);
  await waitFor(() => keyPage("document.body.dataset.ready === '1'"));
  const keyBridge = await keyPage("Object.keys(window.aikKeyEntry ?? {}).sort().join(',')");
  const keyHasAppBridge = await keyPage("typeof window.aikDesktop !== 'undefined' || typeof window.require !== 'undefined'");
  const appHasKeyBridge = await page("typeof window.aikKeyEntry !== 'undefined'");
  const inputType = await keyPage("document.getElementById('key-input')?.type ?? null");
  const image = await keyWindow.webContents.capturePage();
  fs.writeFileSync(path.join(reportDirectory, "key-entry.png"), image.toPNG());
  keyEntry.close();
  const outcome = await page("window.__aikKeyProbe.then((value) => value?.ok === true ? value.result.outcome : value?.error?.code)");
  const isolated = keyBridge === "cancel,context,submit" && !keyHasAppBridge && !appHasKeyBridge;
  return {status: isolated && inputType === "password" && outcome === "cancelled" ? "PASS" : "FAIL",
    provider: connector.provider, isolated, input_type: inputType, outcome, screenshot: "key-entry.png"};
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
    if (route === "settings") {
      // The project folder panel answers from its own engine query; capture it once it has.
      await waitFor(() => page(`(document.getElementById("workspaces-panel")?.dataset.state ?? "LOADING") !== "LOADING"`));
    }
    const state = await page(`document.getElementById("route-${route}").dataset.state ?? null`);
    // Let the compositor paint the new route before capturing it.
    await page("new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))");
    const image = await window.webContents.capturePage();
    const file = `route-${route}.png`;
    fs.writeFileSync(path.join(reportDirectory, file), image.toPNG());
    routes.push({route, rendered: true, state, screenshot: file});
  }
  const connection = await page("document.getElementById('connection-bar').dataset.connection");
  const workspacesPanel = await page(`(() => { const panel = document.getElementById("workspaces-panel");
    return panel ? {state: panel.dataset.state ?? null, vm: panel.dataset.vm ?? null} : null; })()`);
  const keyEntryProbe = await probeKeyEntry();
  const engineInfo = observed.get("engine.info");
  const failed = rendererNodeAccess || !window.isVisible() || routes.length !== ROUTES.length ||
    bridgeKeys !== BRIDGE_KEYS || keyEntryProbe.status === "FAIL";
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
    connections: observed.get("connections.list") ?? null,
    workspaces: observed.get("workspaces.list") ?? null,
    workspaces_panel: workspacesPanel,
    key_entry: keyEntryProbe,
    routes,
    metrics: {
      elapsed_ms: Math.round(performance.now() - started),
      main_rss_bytes: process.memoryUsage().rss,
      renderer_working_set_kb: metrics?.memory?.workingSetSize ?? null
    },
    not_run: ["packaged frozen engine (AIK-057)", "typing and saving a real API key, official vendor login",
      "real VM/AGS data (AIK-044)", "native folder dialog and registering a project folder (needs a VM project)"]
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
  keyEntry = new KeyEntry({createWindow: createKeyWindow, submit: submitKey});
  folderPicker = new FolderPicker({showDialog: (options) => dialog.showOpenDialog(window, options),
    request: (command, payload) => engine.request(command, payload)});
  window.on("closed", () => keyEntry.close());
  const router = new DesktopRouter({readers: handlers(), commandTimeoutsMs: timeouts()});
  ipcMain.handle(CHANNEL, makeRendererHandler({router, getWindow: () => window,
    getWorkspaceId: () => WORKSPACE_ID, appUrl: APP_URL}));
  ipcMain.handle(KEY_CHANNEL, (event, message) => keyEntry.handle(event, message));
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
