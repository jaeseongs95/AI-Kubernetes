"use strict";
// AIK-045: read-only engine queries behind the AIK-004 router. Synthetic DTOs only.
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {VERSION, READ_COMMANDS, BackendError, DesktopRouter, makeRendererHandler} = require("../src/aik_ipc/desktop.cjs");

const INFO = Object.freeze({engine_version: "0.1.0", protocol_version: 1, platform_family: "linux",
  python_version: "3.14.8", frozen: false, state_root: "ABSENT", readmodel: "READY", director_state: "NOT_CONFIGURED"});
const SOURCE = Object.freeze({source: "dispatch", applied_cursor: 1, max_seen_cursor: 3, open_gaps: 1, conflicts: 0, stale: true});

function fixture(readers, url = "aik://app/index.html") {
  const frame = {url, isDestroyed: () => false};
  const webContents = {mainFrame: frame, isDestroyed: () => false};
  const router = new DesktopRouter({readers});
  const handler = makeRendererHandler({router, getWindow: () => ({webContents}),
    getWorkspaceId: () => "local", appUrl: "aik://app/index.html"});
  const event = {sender: webContents, senderFrame: frame};
  const ask = (command, payload = {}) => handler(event, {protocol_version: VERSION, request_id: randomUUID(),
    workspace_id: "local", command, payload});
  return {ask};
}

test("engine.info and readmodel.sources return frozen copies of validated DTOs", async () => {
  const sources = {readmodel: "READY", authoritative: false, sources: [{...SOURCE}]};
  const f = fixture({"engine.info": async () => ({...INFO}), "readmodel.sources": () => sources});
  const info = await f.ask("engine.info");
  assert.equal(info.ok, true);
  assert.deepEqual(info.result, INFO);
  assert.ok(Object.isFrozen(info.result));
  const answer = await f.ask("readmodel.sources");
  assert.deepEqual(answer.result, sources);
  assert.ok(Object.isFrozen(answer.result.sources) && Object.isFrozen(answer.result.sources[0]));
  sources.sources[0].applied_cursor = 99; // later mutation of the backend object does not leak
  assert.equal(answer.result.sources[0].applied_cursor, 1);
});

test("a command without a configured reader is BACKEND_UNAVAILABLE", async () => {
  const f = fixture({});
  for (const command of READ_COMMANDS) {
    const payload = command === "connections.impact" ? {connection_id: "conn-0123456789abcdef"} : {};
    assert.equal((await f.ask(command, payload)).error.code, "BACKEND_UNAVAILABLE");
  }
});

test("invalid or inconsistent backend results are rejected, never passed through", async () => {
  const bad = [
    ["engine.info", {...INFO, path: "/home/user"}],
    ["engine.info", {...INFO, protocol_version: 2}],
    ["engine.info", {...INFO, platform_family: "plan9"}],
    ["engine.info", {...INFO, readmodel: "OK"}],
    ["readmodel.sources", {readmodel: "READY", authoritative: true, sources: []}],
    ["readmodel.sources", {readmodel: "ABSENT", authoritative: false, sources: [SOURCE]}],
    ["readmodel.sources", {readmodel: "READY", authoritative: false, sources: [{...SOURCE, stale: false}]}],
    ["readmodel.sources", {readmodel: "READY", authoritative: false, sources: [{...SOURCE, max_seen_cursor: 0}]}],
    ["readmodel.sources", {readmodel: "READY", authoritative: false, sources: [{...SOURCE, source: "other"}]}],
    ["readmodel.sources", {readmodel: "READY", authoritative: false, sources: [SOURCE, SOURCE]}],
    ["readmodel.sources", {readmodel: "READY", authoritative: false, sources: new Proxy([], {})}],
  ];
  for (const [command, value] of bad) {
    const f = fixture({[command]: async () => value});
    assert.equal((await f.ask(command)).error.code, "INVALID_RESPONSE", JSON.stringify(value));
  }
});

test("readers name incompatibility; other failures stay generic", async () => {
  const cases = [
    [() => { throw new BackendError("BACKEND_INCOMPATIBLE"); }, "BACKEND_INCOMPATIBLE"],
    [async () => { throw new BackendError("BACKEND_INCOMPATIBLE"); }, "BACKEND_INCOMPATIBLE"],
    [async () => { throw new BackendError("SOMETHING_ELSE"); }, "BACKEND_UNAVAILABLE"],
    [async () => { throw new Error("BACKEND_INCOMPATIBLE"); }, "BACKEND_UNAVAILABLE"],
    [async () => { throw new Proxy(new BackendError("BACKEND_INCOMPATIBLE"), {}); }, "BACKEND_UNAVAILABLE"],
  ];
  for (const [reader, code] of cases) {
    const f = fixture({"engine.info": reader});
    const answer = await f.ask("engine.info");
    assert.equal(answer.error.code, code);
    assert.equal(typeof answer.error.message, "string");
  }
});

test("reader configuration accepts only known read commands", () => {
  assert.throws(() => new DesktopRouter({readers: {"vault.resolve": () => ({})}}), TypeError);
  assert.throws(() => new DesktopRouter({readers: {"engine.info": "not a function"}}), TypeError);
  assert.throws(() => new DesktopRouter({readers: new Proxy({}, {})}), TypeError);
  assert.throws(() => new DesktopRouter({readStatus: () => ({}), readers: {"system.status": () => ({})}}), TypeError);
});

test("hash routes of the app document stay trusted; other URLs do not", async () => {
  const reader = {"engine.info": async () => ({...INFO})};
  for (const url of ["aik://app/index.html", "aik://app/index.html#/overview", "aik://app/index.html#/settings"]) {
    assert.equal((await fixture(reader, url).ask("engine.info")).ok, true, url);
  }
  for (const url of ["aik://app/index.html?x=1", "aik://app/index.html#/a?x=1", "aik://app/index.htmlx",
    "aik://app/other.html#/overview", "aik://evil/index.html#/overview", "https://evil.example/#aik://app/index.html"]) {
    assert.equal((await fixture(reader, url).ask("engine.info")).error.code, "UNTRUSTED_SENDER", url);
  }
});
