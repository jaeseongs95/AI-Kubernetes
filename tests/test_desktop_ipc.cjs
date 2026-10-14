"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {readFileSync} = require("node:fs");
const {runInNewContext} = require("node:vm");
const {join} = require("node:path");
const {CHANNEL, VERSION, requestCode, DesktopRouter, makeRendererHandler} = require("../src/aik_ipc/desktop.cjs");

function fixture(readStatus = async () => ({state: "READY", director_state: "STOPPED"}), options = {}) {
  const frame = {url: "aik://app/index.html", isDestroyed: () => false};
  const webContents = {mainFrame: frame, isDestroyed: () => false};
  const window = {webContents};
  let workspace = "workspace-1";
  const router = new DesktopRouter({readStatus, ...options});
  const handler = makeRendererHandler({router, getWindow: () => window,
    getWorkspaceId: () => workspace, appUrl: frame.url});
  const event = {sender: webContents, senderFrame: frame};
  const request = (command = "system.status", workspace_id = "workspace-1") =>
    ({protocol_version: VERSION, request_id: randomUUID(), workspace_id, command, payload: {}});
  return {router, handler, event, request, frame, webContents, setWorkspace: (id) => { workspace = id; }};
}

test("real request routing validates scope, unique IDs, and strict sanitized status", async () => {
  const f = fixture();
  const first = await f.handler(f.event, f.request());
  assert.deepEqual(first.result, {state: "READY", director_state: "STOPPED"});
  assert.equal(first.ok, true);
  const handshake = await f.handler(f.event, f.request("protocol.handshake", "unbound"));
  assert.deepEqual(handshake.result, {ipc_version: 1, boundary: "RENDERER_NO_SECRETS", backend_state: "NOT_QUALIFIED"});
  const wrong = await f.handler(f.event, f.request("system.status", "other"));
  assert.equal(wrong.error.code, "UNKNOWN_WORKSPACE");
  const replay = f.request();
  assert.equal((await f.handler(f.event, replay)).ok, true);
  assert.equal((await f.handler(f.event, replay)).error.code, "DUPLICATE_REQUEST");
});

test("unsupported command, guessed workspace, malicious or accessor-bearing envelopes fail", async () => {
  const f = fixture();
  const invalid = [
    [f.request("shell.execute"), "UNSUPPORTED_COMMAND"],
    [{...f.request(), protocol_version: 2}, "INCOMPATIBLE_PROTOCOL"],
    [{...f.request(), protocol_version: true}, "INCOMPATIBLE_PROTOCOL"],
    [{...f.request(), payload: {command: "rm -rf /"}}, "INVALID_REQUEST"],
    [{...f.request(), credential: "secret-canary"}, "INVALID_REQUEST"],
    [{...f.request(), workspace_id: "../../etc/passwd"}, "INVALID_REQUEST"],
    [{...f.request(), request_id: "secret-canary"}, "INVALID_REQUEST"],
    [Object.defineProperty(f.request(), "payload", {get: () => { throw Error("secret-canary"); }}), "INVALID_REQUEST"],
    [{...f.request(), payload: Object.create({evil: true})}, "INVALID_REQUEST"],
    [Object.assign(Object.create(null), f.request()), "INVALID_REQUEST"]
  ];
  for (const [value, code] of invalid) {
    const response = await f.handler(f.event, value);
    assert.equal(response.error.code, code);
    assert.equal(JSON.stringify(response).includes("secret-canary"), false);
    if (code === "INCOMPATIBLE_PROTOCOL") assert.deepEqual(response.error.supported_versions, [1]);
  }
  assert.equal(requestCode(f.request()), null);
});

test("foreign frame, other webContents, navigated frame and destroyed window cannot invoke", async () => {
  const f = fixture();
  for (const badEvent of [
    {sender: {}, senderFrame: f.frame},
    {sender: f.webContents, senderFrame: {...f.frame}},
    {sender: f.webContents, senderFrame: {...f.frame, url: "https://evil.example"}},
    {},
  ]) {
    assert.equal((await f.handler(badEvent, f.request())).error.code, "UNTRUSTED_SENDER");
  }
  f.frame.isDestroyed = () => true;
  assert.equal((await f.handler(f.event, f.request())).error.code, "UNTRUSTED_SENDER");
});

test("workspace switching while async handler runs invalidates response", async () => {
  let unblock;
  const f = fixture(() => new Promise((resolve) => { unblock = resolve; }));
  const pending = f.handler(f.event, f.request());
  f.setWorkspace("workspace-2");
  unblock({state: "READY", director_state: "STOPPED"});
  assert.equal((await pending).error.code, "UNKNOWN_WORKSPACE");
});

test("backend exceptions and secret-bearing extra fields never leak to renderer", async () => {
  for (const handler of [
    () => { throw Error("secret-canary"); },
    () => ({state: "READY", director_state: "RUNNING", credential: "secret-canary"}),
    () => ({state: "READY", director_state: "WRONG"}),
    () => ({state: "READY", director_state: "RUNNING", get secret() { throw Error("secret-canary"); }})
  ]) {
    const f = fixture(handler);
    const response = await f.handler(f.event, f.request());
    assert.equal(response.ok, false);
    assert.equal(JSON.stringify(response).includes("secret-canary"), false);
  }
  const f = fixture(null);
  assert.equal((await f.handler(f.event, f.request())).error.code, "BACKEND_UNAVAILABLE");
});

test("sandboxed preload exposes only closed operations without Node builtins", async () => {
  const calls = [];
  let exposed;
  const source = readFileSync(join(__dirname, "../src/aik_ipc/preload.cjs"), "utf8");
  runInNewContext(source, {
    require: (name) => {
      assert.equal(name, "electron");
      return {contextBridge: {exposeInMainWorld: (name, api) => {
        assert.equal(name, "aikDesktop"); exposed = api;
      }}, ipcRenderer: {invoke: (...args) => { calls.push(args); return Promise.resolve({ok: true}); }}};
    },
    globalThis: {crypto: {randomUUID}}
  });
  assert.deepEqual(Object.keys(exposed).sort(), ["cancelLogin", "checkConnection", "completeLogin",
    "connectionCatalog", "connectionImpact", "connections", "deleteConnection", "engineInfo", "handshake",
    "loginState", "logoutConnection", "readmodelSources", "renameConnection", "requestKeyEntry", "startLogin",
    "status"]);
  assert.equal(/add_key|rotate_key/.test(source), false, "the app preload never names a key-bearing command");
  assert.equal(Object.isFrozen(exposed), true);
  await exposed.handshake();
  await exposed.status("workspace-1");
  await exposed.engineInfo("workspace-1");
  await exposed.readmodelSources("workspace-1");
  assert.equal(calls.length, 4);
  assert.deepEqual(calls.slice(2).map(([, request]) => [request.command, request.workspace_id]),
    [["engine.info", "workspace-1"], ["readmodel.sources", "workspace-1"]]);
  assert.equal(calls[0][0], CHANNEL);
  assert.equal(calls[0][1].command, "protocol.handshake");
  assert.equal(calls[1][1].command, "system.status");
  assert.equal(calls[0][1].workspace_id, "unbound");
  assert.equal(calls[1][1].workspace_id, "workspace-1");
  assert.notEqual(calls[0][1].request_id, calls[1][1].request_id);
});

test("inflight and replay limits fail closed without leaking responses", async () => {
  let release;
  const f = fixture(() => new Promise(resolve => { release = resolve; }), {maxInflight: 1, replayLimit: 1});
  const waiting = f.handler(f.event, f.request());
  assert.equal((await f.handler(f.event, f.request())).error.code, "ROUTER_BUSY");
  release({state: "READY", director_state: "STOPPED"});
  assert.equal((await waiting).ok, true);
  assert.equal((await f.handler(f.event, f.request())).error.code, "ROUTER_BUSY");
});

test("handshake does not imply an installed backend or VM/AGS qualification", async () => {
  const f = fixture(null);
  const response = await f.handler(f.event, f.request("protocol.handshake", "unbound"));
  assert.equal(response.ok, true);
  assert.equal(response.result.backend_state, "NOT_QUALIFIED");
  assert.equal((await f.handler(f.event, f.request())).error.code, "BACKEND_UNAVAILABLE");
});


test("replay window expires and frees capacity without accepting immediate replays", async () => {
  let now = 0;
  const f = fixture(async () => ({state: "READY", director_state: "STOPPED"}),
    {maxInflight: 1, replayLimit: 1, replayWindowMs: 1000, clock: () => now});
  const first = f.request();
  assert.equal((await f.handler(f.event, first)).ok, true);
  assert.equal((await f.handler(f.event, first)).error.code, "DUPLICATE_REQUEST");
  now = 1001;
  assert.equal((await f.handler(f.event, f.request())).ok, true);
});

test("main-process binding failure returns sanitized error", async () => {
  const router = new DesktopRouter();
  const handler = makeRendererHandler({router,
    getWindow: () => { throw Error("secret-canary"); }, getWorkspaceId: () => "workspace-1"});
  const result = await handler({}, {request_id: randomUUID()});
  assert.equal(result.error.code, "BACKEND_UNAVAILABLE");
  assert.equal(JSON.stringify(result).includes("secret-canary"), false);
});
