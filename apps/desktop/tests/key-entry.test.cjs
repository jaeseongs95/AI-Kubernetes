"use strict";
// AIK-046/047: main-owned key entry window with a fake BrowserWindow. No real key is used.
const test = require("node:test");
const assert = require("node:assert/strict");
const {EventEmitter} = require("node:events");
const {KEY_URL, KeyEntry, KeyEntryError, acceptableSecret} = require("../electron/key-entry.cjs");

const SECRET = "sk-synthetic-entry-key-0001";
const CONNECTION = {connection_id: "conn-0123456789abcdef", account_label: "업무 키", provider: "vendor-a",
  display_name: "Vendor A API", execution_kind: "api", method: "api_key", state: "verified",
  verified_at: "2026-10-14T03:40:00Z", revision: 2, binding_revision: 1, last_check: null,
  usage: {references: 0, blocking: 0, external_checked: false}};
const CONTEXT = {mode: "add", provider: "vendor-a", account_label: "업무 키", display_name: "Vendor A API",
  connection_id: null, expected_revision: null};

class FakeWindow extends EventEmitter {
  constructor() {
    super();
    this.destroyed = false;
    const frame = {url: KEY_URL};
    this.webContents = {mainFrame: frame};
    this.loaded = null;
  }
  loadURL(url) { this.loaded = url; return Promise.resolve(); }
  isDestroyed() { return this.destroyed; }
  close() { if (!this.destroyed) { this.destroyed = true; this.emit("closed"); } }
}

function setup(submit = async () => ({connection: CONNECTION, check: {state: "verified", code: null}})) {
  const windows = [];
  const submitted = [];
  const entry = new KeyEntry({createWindow: () => { const w = new FakeWindow(); windows.push(w); return w; },
    submit: async (context, secret) => { submitted.push([context.mode, secret]); return submit(context, secret); }});
  const eventFor = (window) => ({sender: window.webContents, senderFrame: window.webContents.mainFrame});
  return {entry, windows, submitted, eventFor};
}

test("save flow: context, submit once, secret-free outcome, window closed", async () => {
  const {entry, windows, submitted, eventFor} = setup();
  const opened = entry.open(CONTEXT);
  await new Promise((resolve) => setImmediate(resolve));
  const [window] = windows;
  assert.equal(window.loaded, KEY_URL);
  const context = await entry.handle(eventFor(window), {op: "context"});
  assert.deepEqual(context, {ok: true, context: {mode: "add", display_name: "Vendor A API", account_label: "업무 키"}});
  assert.deepEqual(await entry.handle(eventFor(window), {op: "submit", secret: SECRET}), {ok: true});
  const outcome = await opened;
  assert.equal(outcome.outcome, "saved");
  assert.equal(outcome.connection.connection_id, CONNECTION.connection_id);
  assert.equal(JSON.stringify(outcome).includes(SECRET), false);
  assert.equal(window.destroyed, true);
  assert.deepEqual(submitted, [["add", SECRET]]);
  assert.equal(entry.current, null);
});

test("only the key window's main frame at the key URL is trusted", async () => {
  const {entry, windows, submitted} = setup();
  entry.open(CONTEXT);
  await new Promise((resolve) => setImmediate(resolve));
  const [window] = windows;
  const other = new FakeWindow();
  const attempts = [
    {sender: other.webContents, senderFrame: other.webContents.mainFrame},
    {sender: window.webContents, senderFrame: {url: KEY_URL}},
    {sender: window.webContents, senderFrame: Object.assign(window.webContents.mainFrame, {})}
  ];
  for (const event of attempts.slice(0, 2)) {
    assert.deepEqual(await entry.handle(event, {op: "submit", secret: SECRET}), {ok: false, code: "UNTRUSTED_SENDER"});
  }
  window.webContents.mainFrame.url = "aik://app/index.html";
  assert.deepEqual(await entry.handle(attempts[2], {op: "context"}), {ok: false, code: "UNTRUSTED_SENDER"});
  assert.deepEqual(submitted, []);
});

test("rejected input and engine errors keep the window open for a retry", async () => {
  let fail = "INVALID_SECRET";
  const {entry, windows, submitted, eventFor} = setup(async () => {
    if (fail) { const error = new Error("x"); error.code = fail; throw error; }
    return {connection: CONNECTION, check: {state: "unknown", code: "NETWORK"}};
  });
  const opened = entry.open(CONTEXT);
  await new Promise((resolve) => setImmediate(resolve));
  const event = eventFor(windows[0]);
  for (const secret of ["", " padded ", "line\nbreak", 7, "x".repeat(16385)]) {
    assert.deepEqual(await entry.handle(event, {op: "submit", secret}), {ok: false, code: "INVALID_SECRET"});
  }
  assert.deepEqual(await entry.handle(event, {op: "submit", secret: SECRET, extra: 1}), {ok: false, code: "INVALID_REQUEST"});
  assert.deepEqual(await entry.handle(event, {op: "submit", secret: SECRET}), {ok: false, code: "INVALID_SECRET"});
  fail = "sk-leak-canary";
  assert.deepEqual(await entry.handle(event, {op: "submit", secret: SECRET}), {ok: false, code: "BACKEND_UNAVAILABLE"});
  assert.equal(windows[0].destroyed, false);
  fail = null;
  assert.deepEqual(await entry.handle(event, {op: "submit", secret: SECRET}), {ok: true});
  assert.equal((await opened).check.state, "unknown");
  assert.equal(submitted.length, 3);
});

test("cancel, close and timeout settle once; a second window is refused while one is open", async () => {
  const {entry, windows, eventFor} = setup();
  const first = entry.open(CONTEXT);
  await new Promise((resolve) => setImmediate(resolve));
  await assert.rejects(entry.open(CONTEXT), (error) => error instanceof KeyEntryError && error.code === "KEY_ENTRY_BUSY");
  assert.deepEqual(await entry.handle(eventFor(windows[0]), {op: "cancel"}), {ok: true});
  assert.deepEqual(await first, {outcome: "cancelled", connection: null, check: null});

  const second = entry.open(CONTEXT);
  await new Promise((resolve) => setImmediate(resolve));
  windows[1].close(); // the user closed the window
  assert.equal((await second).outcome, "cancelled");

  const controller = new AbortController();
  const third = entry.open(CONTEXT, controller.signal);
  await new Promise((resolve) => setImmediate(resolve));
  controller.abort();
  await assert.rejects(third, (error) => error.code === "BACKEND_TIMEOUT");
  assert.equal(windows[2].destroyed, true);
  assert.equal(entry.current, null);
});

test("a window that cannot load is reported, not left open", async () => {
  const entry = new KeyEntry({createWindow: () => Object.assign(new FakeWindow(), {loadURL: () => Promise.reject(new Error("x"))}),
    submit: async () => ({})});
  await assert.rejects(entry.open(CONTEXT), (error) => error.code === "KEY_ENTRY_UNAVAILABLE");
  assert.equal(entry.current, null);
  assert.equal(acceptableSecret("sk-ok"), true);
});
