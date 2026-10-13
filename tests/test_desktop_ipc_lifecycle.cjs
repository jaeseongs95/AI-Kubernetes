"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {DesktopRouter, makeRendererHandler, requestCode, failed, validStatus} = require("../src/aik_ipc/desktop.cjs");

const status = () => ({state: "READY", director_state: "STOPPED"});
function fixture(readStatus = status, options = {}) {
  const frame = {url: "aik://app/index.html", isDestroyed: () => false};
  const contents = {mainFrame: frame, isDestroyed: () => false};
  let workspace = "workspace-1";
  let currentWindow = {webContents: contents};
  const router = new DesktopRouter({readStatus, ...options});
  const handler = makeRendererHandler({router, getWindow: () => currentWindow,
    getWorkspaceId: () => workspace, appUrl: frame.url});
  const event = {sender: contents, senderFrame: frame};
  const request = () => ({protocol_version: 1, request_id: randomUUID(),
    command: "system.status", workspace_id: "workspace-1", payload: {}});
  return {router, handler, event, request, frame, contents,
    workspace: id => { workspace = id; }, window: w => { currentWindow = w; }};
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return {promise, resolve, reject};
}
const tick = () => new Promise(resolve => setImmediate(resolve));

// These eight regression cases fail against the pinned 02f25be source.
test("request identity is a snapshot, not a mutable reference across await", async () => {
  const work = deferred();
  const f = fixture(() => work.promise);
  const req = f.request();
  const original = {...req};
  const pending = f.handler(f.event, req);
  req.request_id = "secret-canary";
  req.workspace_id = "secret-canary";
  work.resolve(status());
  const response = await pending;
  assert.equal(response.request_id, original.request_id);
  assert.equal(response.workspace_id, original.workspace_id);
  assert.equal(JSON.stringify(response).includes("secret-canary"), false);
});

test("validated backend status is copied before exposing it", async () => {
  const shared = status();
  const f = fixture(() => shared);
  const response = await f.handler(f.event, f.request());
  shared.state = "secret-canary";
  shared.credential = "secret-canary";
  assert.deepEqual(response.result, status());
  assert.notEqual(response.result, shared);
});

test("UUID casing cannot bypass replay identity", async () => {
  let calls = 0;
  const f = fixture(() => { calls++; return status(); });
  const req = f.request();
  req.request_id = "abcdefab-1234-4abc-8abc-123456789abc";
  assert.equal((await f.handler(f.event, req)).ok, true);
  const result = await f.handler(f.event, {...req, request_id: req.request_id.toUpperCase()});
  assert.equal(result.error?.code, "DUPLICATE_REQUEST");
  assert.equal(calls, 1);
});

test("pending work retains its replay fence after the replay window", async () => {
  let now = 0, calls = 0;
  const work = deferred();
  const f = fixture(() => { calls++; return work.promise; }, {replayWindowMs: 1000, clock: () => now});
  const req = f.request();
  const first = f.handler(f.event, req);
  now = 2000;
  const second = f.handler(f.event, {...req});
  work.resolve(status());
  const responses = await Promise.all([first, second]);
  assert.equal(responses[1].error?.code, "DUPLICATE_REQUEST");
  assert.equal(calls, 1);
});

test("stalled backend yields a bounded timeout instead of hanging the caller", async () => {
  const work = deferred();
  const f = fixture(() => work.promise, {requestTimeoutMs: 20});
  const pending = f.handler(f.event, f.request());
  let watchdog;
  const result = await Promise.race([pending, new Promise(resolve => {
    watchdog = setTimeout(() => resolve({watchdog: true}), 250);
  })]);
  clearTimeout(watchdog);
  work.resolve(status());
  await pending;
  assert.equal(result.error?.code, "BACKEND_TIMEOUT");
});

test("request proxies are rejected without running traps", () => {
  let traps = 0;
  const request = fixture().request();
  const proxy = new Proxy(request, {getPrototypeOf: value => { traps++; return Object.getPrototypeOf(value); }});
  assert.equal(requestCode(proxy), "INVALID_REQUEST");
  assert.equal(traps, 0);
});

test("error helper never reflects an arbitrary code string", () => {
  const response = failed({}, "secret-canary");
  assert.equal(response.error.code, "BACKEND_UNAVAILABLE");
  assert.equal(JSON.stringify(response).includes("secret-canary"), false);
});

test("request validation never invokes inherited toJSON", () => {
  let called = 0;
  const req = fixture().request();
  Object.defineProperty(Object.prototype, "toJSON", {
    configurable: true, value() { called++; return {}; }
  });
  let code;
  try { code = requestCode(req); } finally { delete Object.prototype.toJSON; }
  assert.equal(code, null);
  assert.equal(called, 0);
});

test("timeout keeps physical capacity reserved until ignored work really settles", async () => {
  const work = deferred();
  let signal, calls = 0;
  const f = fixture((_workspace, context) => { calls++; signal = context.signal; return work.promise; },
    {maxInflight: 1, replayLimit: 8, requestTimeoutMs: 10});
  const req = f.request();
  assert.equal((await f.handler(f.event, req)).error.code, "BACKEND_TIMEOUT");
  assert.equal(signal.aborted, true);
  assert.equal(f.router.inflight, 1);
  assert.equal((await f.handler(f.event, f.request())).error.code, "ROUTER_BUSY");
  assert.equal(calls, 1);
  work.resolve(status());
  await tick();
  assert.equal(f.router.inflight, 0);
  assert.equal((await f.handler(f.event, f.request())).ok, true);
});

test("late rejection is handled without releasing capacity twice or leaking its message", async () => {
  const work = deferred();
  const f = fixture(() => work.promise, {requestTimeoutMs: 10});
  const response = await f.handler(f.event, f.request());
  work.reject(Error("secret-canary"));
  await tick();
  assert.equal(response.error.code, "BACKEND_TIMEOUT");
  assert.equal(JSON.stringify(response).includes("secret-canary"), false);
  assert.equal(f.router.inflight, 0);
});

test("late Proxy response is discarded without evaluating its properties", async () => {
  const work = deferred();
  let traps = 0;
  const f = fixture(() => work.promise, {requestTimeoutMs: 10});
  assert.equal((await f.handler(f.event, f.request())).error.code, "BACKEND_TIMEOUT");
  // Native Promise resolution necessarily checks `then`; count only shape traps.
  work.resolve(new Proxy(status(), {getPrototypeOf: () => { traps++; return Object.prototype; }}));
  await tick();
  assert.equal(traps, 0);
  assert.equal(f.router.inflight, 0);
});

test("cooperative backend receives a frozen context and abort releases only on settlement", async () => {
  let observed;
  const f = fixture((workspace, context) => {
    observed = {workspace, context};
    return new Promise((_resolve, reject) => context.signal.addEventListener("abort", () => reject(Error("aborted")), {once: true}));
  }, {requestTimeoutMs: 10});
  const req = f.request();
  assert.equal((await f.handler(f.event, req)).error.code, "BACKEND_TIMEOUT");
  await tick();
  assert.equal(observed.workspace, "workspace-1");
  assert.equal(observed.context.requestId, req.request_id);
  assert.equal(Object.isFrozen(observed.context), true);
  assert.equal(f.router.inflight, 0);
});

test("replay protection lasts a full window after late work settles", async () => {
  let now = 0;
  const work = deferred();
  const f = fixture(() => work.promise, {clock: () => now, replayWindowMs: 1000, requestTimeoutMs: 10});
  const req = f.request();
  assert.equal((await f.handler(f.event, req)).error.code, "BACKEND_TIMEOUT");
  now = 5000;
  assert.equal((await f.handler(f.event, req)).error.code, "DUPLICATE_REQUEST");
  work.resolve(status());
  await tick();
  now = 5500;
  assert.equal((await f.handler(f.event, req)).error.code, "DUPLICATE_REQUEST");
  now = 6001;
  assert.equal((await f.handler(f.event, req)).ok, true);
});

test("handshake remains available during a stalled backend but never qualifies it", async () => {
  const work = deferred();
  const f = fixture(() => work.promise, {maxInflight: 1, replayLimit: 8, requestTimeoutMs: 10});
  const first = f.handler(f.event, f.request());
  const response = await f.handler(f.event, {...f.request(), command: "protocol.handshake", workspace_id: "unbound"});
  assert.equal(response.result.backend_state, "NOT_QUALIFIED");
  work.resolve(status());
  await first;
});

test("window or frame change during a read prevents success", async () => {
  for (const change of [f => f.window(null), f => { f.frame.isDestroyed = () => true; },
    f => { f.frame.url = "https://other.invalid"; }]) {
    const work = deferred();
    const f = fixture(() => work.promise);
    const pending = f.handler(f.event, f.request());
    change(f);
    work.resolve(status());
    assert.equal((await pending).error.code, "UNTRUSTED_SENDER");
  }
});

test("scope loss after a deadline still cannot return a status for the former workspace", async () => {
  const work = deferred();
  const f = fixture(() => work.promise, {requestTimeoutMs: 10});
  const pending = f.handler(f.event, f.request());
  f.workspace("workspace-2");
  const response = await pending;
  work.resolve(status());
  await tick();
  assert.equal(response.error.code, "UNKNOWN_WORKSPACE");
});

test("callback results with getters, symbols, Proxies or thenables are rejected without executing hooks", async () => {
  let hooks = 0;
  const values = [
    Object.defineProperty(status(), "state", {get() { hooks++; return "READY"; }}),
    {...status(), [Symbol("hidden")]: "secret-canary"},
    new Proxy(status(), {getPrototypeOf() { hooks++; return Object.prototype; }}),
    {then() { hooks++; }},
    Object.defineProperty(status(), "then", {get() { hooks++; throw Error("secret-canary"); }})
  ];
  for (const value of values) {
    const f = fixture(() => value);
    const response = await f.handler(f.event, f.request());
    assert.equal(response.error.code, "INVALID_RESPONSE");
    assert.equal(validStatus(value), false);
  }
  assert.equal(hooks, 0);
});

test("success and error envelopes are frozen and detached", async () => {
  const f = fixture();
  const response = await f.handler(f.event, f.request());
  assert.equal(Object.isFrozen(response), true);
  assert.equal(Object.isFrozen(response.result), true);
  const error = failed({}, "INCOMPATIBLE_PROTOCOL");
  assert.equal(Object.isFrozen(error), true);
  assert.equal(Object.isFrozen(error.error), true);
  assert.equal(Object.isFrozen(error.error.supported_versions), true);
});

test("native callback failures produce a fixed error and release the physical slot", async () => {
  for (const read of [() => { throw Error("secret-canary"); }, () => Promise.reject(Error("secret-canary"))]) {
    const f = fixture(read);
    const response = await f.handler(f.event, f.request());
    assert.equal(response.error.code, "BACKEND_UNAVAILABLE");
    assert.equal(JSON.stringify(response).includes("secret-canary"), false);
    assert.equal(f.router.inflight, 0);
  }
});

test("invalid clock values or throwing clock fail before invoking the backend", async () => {
  for (const clock of [() => NaN, () => Infinity, () => -1, () => "1", () => { throw Error("secret-canary"); }]) {
    let calls = 0;
    const f = fixture(() => { calls++; return status(); }, {clock});
    const response = await f.handler(f.event, f.request());
    assert.equal(response.error.code, "BACKEND_UNAVAILABLE");
    assert.equal(calls, 0);
    assert.equal(JSON.stringify(response).includes("secret-canary"), false);
  }
});

test("clock failure during completion conservatively retains replay identity", async () => {
  let broken = false;
  const clock = () => { if (broken) throw Error("secret-canary"); return 1000; };
  const f = fixture(() => { broken = true; return status(); }, {clock});
  const req = f.request();
  assert.equal((await f.handler(f.event, req)).ok, true);
  assert.equal(f.router.inflight, 0);
  broken = false;
  assert.equal((await f.handler(f.event, req)).error.code, "DUPLICATE_REQUEST");
});

test("expired completed entries are pruned even when an earlier entry is still pending", async () => {
  let now = 0, calls = 0;
  const work = deferred();
  const f = fixture(() => ++calls === 1 ? work.promise : status(),
    {clock: () => now, maxInflight: 2, replayLimit: 2, replayWindowMs: 1000});
  const first = f.handler(f.event, f.request());
  assert.equal((await f.handler(f.event, f.request())).ok, true);
  now = 1001;
  assert.equal((await f.handler(f.event, f.request())).ok, true);
  work.resolve(status());
  await first;
});

test("clock rollback does not shorten a completed replay window", async () => {
  let now = 1000;
  const f = fixture(status, {clock: () => now, replayWindowMs: 1000});
  const req = f.request();
  await f.handler(f.event, req);
  now = 10;
  assert.equal((await f.handler(f.event, req)).error.code, "DUPLICATE_REQUEST");
  now = 2001;
  assert.equal((await f.handler(f.event, req)).ok, true);
});

test("deadline and memory bounds reject invalid configuration", () => {
  for (const requestTimeoutMs of [0, -1, true, 1.5, Infinity, 60001]) {
    assert.throws(() => new DesktopRouter({requestTimeoutMs}), RangeError);
  }
  for (const replayLimit of [NaN, Infinity, 65537]) assert.throws(() => new DesktopRouter({replayLimit}), RangeError);
  for (const replayWindowMs of [Infinity, 86400001]) assert.throws(() => new DesktopRouter({replayWindowMs}), RangeError);
});

test("unusable Promise subclass cannot leak a physical slot", async () => {
  class BrokenPromise extends Promise {
    static get [Symbol.species]() { throw Error("secret-canary"); }
  }
  const f = fixture(() => new BrokenPromise(resolve => resolve(status())));
  const response = await f.handler(f.event, f.request());
  assert.equal(response.error.code, "BACKEND_UNAVAILABLE");
  assert.equal(f.router.inflight, 0);
  assert.equal(JSON.stringify(response).includes("secret-canary"), false);
});
