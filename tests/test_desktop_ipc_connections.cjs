"use strict";
// AIK-046/047: connection commands behind the AIK-004 router. Synthetic DTOs only; no secret is used.
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const {runInNewContext} = require("node:vm");
const {VERSION, READ_COMMANDS, WRITE_COMMANDS, BackendError, DesktopRouter, makeRendererHandler} =
  require("../src/aik_ipc/desktop.cjs");
const {PAYLOADS, readPayload} = require("../src/aik_ipc/connections.cjs");

const CONNECTION = Object.freeze({connection_id: "conn-0123456789abcdef", account_label: "업무 키",
  provider: "vendor-a", display_name: "Vendor A API", execution_kind: "api", method: "api_key", state: "verified",
  verified_at: "2026-10-14T03:40:00.123456Z", revision: 2, binding_revision: 1,
  last_check: {state: "verified", code: null, checked_at: "2026-10-14T03:40:00.123456Z"},
  usage: {references: 1, blocking: 0, external_checked: false}});
const CHECK = Object.freeze({state: "verified", code: null});

function fixture(readers, options = {}) {
  const frame = {url: "aik://app/index.html", isDestroyed: () => false};
  const webContents = {mainFrame: frame, isDestroyed: () => false};
  const router = new DesktopRouter({readers, ...options});
  const handler = makeRendererHandler({router, getWindow: () => ({webContents}),
    getWorkspaceId: () => "local", appUrl: "aik://app/index.html"});
  const event = {sender: webContents, senderFrame: frame};
  const ask = (command, payload = {}) => handler(event, {protocol_version: VERSION, request_id: randomUUID(),
    workspace_id: "local", command, payload});
  return {ask};
}

const VALID = {
  "connections.catalog": {}, "connections.list": {}, "connections.login_state": {}, "connections.cancel_login": {},
  "connections.complete_login": {},
  "connections.impact": {connection_id: "conn-1"},
  "connections.check": {connection_id: "conn-1"},
  "connections.rename": {connection_id: "conn-1", expected_revision: 1, account_label: "새 이름"},
  "connections.delete": {connection_id: "conn-1", expected_revision: 1, acknowledge_impact: false},
  "connections.logout": {connection_id: "conn-1", expected_revision: 3},
  "connections.start_login": {provider: "vendor-b", account_label: "구독", connection_id: null},
  "connections.request_key_entry": {mode: "add", provider: "vendor-a", account_label: "키", connection_id: null,
    expected_revision: null}
};

test("renderer commands never include a key-bearing command", () => {
  const all = [...READ_COMMANDS, ...WRITE_COMMANDS];
  assert.equal(all.some((command) => /add_key|rotate_key|secret|vault/.test(command)), false);
  assert.deepEqual(Object.keys(PAYLOADS).sort(), all.filter((c) => c.startsWith("connections.")).sort());
  for (const [command, schema] of Object.entries(PAYLOADS)) {
    assert.equal(Object.keys(schema).some((field) => /secret|key$|token|password/.test(field)), false, command);
  }
});

test("payloads are exact, typed and frozen copies", () => {
  for (const [command, payload] of Object.entries(VALID)) {
    const copy = readPayload(command, payload);
    assert.ok(copy, command);
    assert.equal(Object.isFrozen(copy), true);
    assert.deepEqual(copy, payload);
  }
  const bad = [
    ["connections.check", {connection_id: "conn-1", secret: "x"}],
    ["connections.check", {}],
    ["connections.check", {connection_id: "../etc"}],
    ["connections.rename", {connection_id: "conn-1", expected_revision: 0, account_label: "a"}],
    ["connections.rename", {connection_id: "conn-1", expected_revision: 1.5, account_label: "a"}],
    ["connections.rename", {connection_id: "conn-1", expected_revision: 1, account_label: " edge"}],
    ["connections.rename", {connection_id: "conn-1", expected_revision: 1, account_label: "a\u0000b"}],
    ["connections.rename", {connection_id: "conn-1", expected_revision: 1, account_label: "x".repeat(129)}],
    ["connections.delete", {connection_id: "conn-1", expected_revision: 1, acknowledge_impact: "yes"}],
    ["connections.start_login", {provider: "vendor-b", account_label: null, connection_id: null}],
    ["connections.start_login", {provider: "vendor-b", account_label: "a", connection_id: "conn-1"}],
    ["connections.request_key_entry", {mode: "add", provider: "v", account_label: "a", connection_id: "conn-1",
      expected_revision: null}],
    ["connections.request_key_entry", {mode: "rotate", provider: null, account_label: null, connection_id: "conn-1",
      expected_revision: null}],
    ["connections.request_key_entry", {mode: "show", provider: null, account_label: null, connection_id: "conn-1",
      expected_revision: 1}],
    ["connections.check", Object.create({connection_id: "conn-1"})],
    ["connections.check", new Proxy({connection_id: "conn-1"}, {})]
  ];
  for (const [command, payload] of bad) assert.equal(readPayload(command, payload), null, JSON.stringify(payload));
});

test("handlers receive the validated payload and results become frozen DTOs", async () => {
  const seen = [];
  const f = fixture({
    "connections.check": (_ws, context) => { seen.push(context.payload); return {connection: {...CONNECTION}, check: {...CHECK}}; },
    "connections.list": () => ({store: "READY", connections: [{...CONNECTION}]}),
    "connections.catalog": () => ({connectors: [{provider: "vendor-a", execution_kind: "api", method: "api_key",
      display_name: "Vendor A API"}]}),
    "connections.login_state": () => ({state: "idle", provider: null, cancel_supported: false, code: null,
      connection_id: null}),
    "connections.request_key_entry": () => ({outcome: "cancelled", connection: null, check: null})
  });
  const checked = await f.ask("connections.check", {connection_id: "conn-0123456789abcdef"});
  assert.equal(checked.ok, true);
  assert.deepEqual(seen, [{connection_id: "conn-0123456789abcdef"}]);
  assert.equal(Object.isFrozen(seen[0]), true);
  assert.equal(Object.isFrozen(checked.result.connection.usage), true);
  assert.equal((await f.ask("connections.list")).result.connections.length, 1);
  assert.equal((await f.ask("connections.catalog")).ok, true);
  assert.equal((await f.ask("connections.login_state")).ok, true);
  const entry = await f.ask("connections.request_key_entry", VALID["connections.request_key_entry"]);
  assert.deepEqual(entry.result, {outcome: "cancelled", connection: null, check: null});
});

test("inconsistent connection DTOs never reach the renderer", async () => {
  const bad = [
    {...CONNECTION, credential_ref: "secret://aik-vault/0123"},
    {...CONNECTION, method: "host_login"},
    {...CONNECTION, state: "ok"},
    {...CONNECTION, verified_at: "yesterday"},
    {...CONNECTION, usage: {references: null, blocking: 1, external_checked: false}},
    {...CONNECTION, usage: {references: 1, blocking: 2, external_checked: false}},
    {...CONNECTION, last_check: {state: "verified", code: "lower case", checked_at: CONNECTION.verified_at}},
    {...CONNECTION, account_label: "a\nb"}
  ];
  for (const connection of bad) {
    const f = fixture({"connections.list": () => ({store: "READY", connections: [connection]})});
    assert.equal((await f.ask("connections.list")).error.code, "INVALID_RESPONSE", JSON.stringify(connection));
  }
  const f = fixture({"connections.list": () => ({store: "ABSENT", connections: [CONNECTION]})});
  assert.equal((await f.ask("connections.list")).error.code, "INVALID_RESPONSE");
  const g = fixture({"connections.request_key_entry": () => ({outcome: "saved", connection: null, check: null})});
  assert.equal((await g.ask("connections.request_key_entry", VALID["connections.request_key_entry"])).error.code,
    "INVALID_RESPONSE");
});

test("connection service codes pass through; unknown codes stay generic", async () => {
  const f = fixture({
    "connections.check": () => { throw new BackendError("REVISION_CONFLICT"); },
    "connections.rename": () => { throw new BackendError("sk-secret-canary"); }
  });
  const conflict = await f.ask("connections.check", {connection_id: "conn-1"});
  assert.equal(conflict.error.code, "REVISION_CONFLICT");
  const generic = await f.ask("connections.rename", VALID["connections.rename"]);
  assert.equal(generic.error.code, "BACKEND_UNAVAILABLE");
  assert.equal(JSON.stringify(generic).includes("canary"), false);
});

test("per-command timeouts are bounded and applied", async () => {
  assert.throws(() => new DesktopRouter({commandTimeoutsMs: {"connections.add_key": 10}}), RangeError);
  assert.throws(() => new DesktopRouter({commandTimeoutsMs: {"connections.check": 1800001}}), RangeError);
  assert.throws(() => new DesktopRouter({readers: {"connections.add_key": () => ({})}}), TypeError);
  let signal;
  const f = fixture({"connections.check": (_ws, context) => { signal = context.signal; return new Promise(() => {}); }},
    {requestTimeoutMs: 60000, commandTimeoutsMs: {"connections.check": 20}});
  const answer = await f.ask("connections.check", {connection_id: "conn-1"});
  assert.equal(answer.error.code, "BACKEND_TIMEOUT");
  assert.equal(signal.aborted, true);
});

test("key entry preload exposes three calls on its own channel and nothing else", async () => {
  const source = readFileSync(join(__dirname, "../src/aik_ipc/key_entry_preload.cjs"), "utf8");
  const calls = [];
  let name, api;
  runInNewContext(source, {
    require: (module) => {
      assert.equal(module, "electron");
      return {contextBridge: {exposeInMainWorld: (n, value) => { name = n; api = value; }},
        ipcRenderer: {invoke: (...args) => { calls.push(args); return Promise.resolve({ok: true}); }}};
    }
  });
  assert.equal(name, "aikKeyEntry");
  assert.deepEqual(Object.keys(api).sort(), ["cancel", "context", "submit"]);
  assert.equal(Object.isFrozen(api), true);
  await api.context();
  await api.submit("synthetic");
  await api.cancel();
  assert.deepEqual(calls.map(([channel, message]) => [channel, message.op]),
    [["aik:key-entry:v1", "context"], ["aik:key-entry:v1", "submit"], ["aik:key-entry:v1", "cancel"]]);
  assert.notEqual("aik:key-entry:v1", require("../src/aik_ipc/desktop.cjs").CHANNEL);
});
