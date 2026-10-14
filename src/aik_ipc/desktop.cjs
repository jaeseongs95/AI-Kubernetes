"use strict";

// AIK-004: read-only renderer IPC. Native main owns caller/workspace binding.
// Do not expose this router or a generic invoke method through the preload.
// AIK-045 adds two read-only queries (engine.info, readmodel.sources). Every
// backend result is copied field by field into a frozen DTO before it leaves.
const {performance} = require("node:perf_hooks");
const {types} = require("node:util");

const CHANNEL = "aik:desktop:v1";
const VERSION = 1;
const MAX_FRAME_BYTES = 16384;
const REQUEST_FIELDS = ["protocol_version", "request_id", "workspace_id", "command", "payload"];
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WORKSPACE = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/;
const COMMANDS = new Set(["protocol.handshake", "system.status", "engine.info", "readmodel.sources"]);
const READ_COMMANDS = Object.freeze(["system.status", "engine.info", "readmodel.sources"]);
const STATES = new Set(["READY", "DEGRADED", "BLOCKED"]);
const DIRECTOR_STATES = new Set(["NOT_CONFIGURED", "STOPPED", "RUNNING", "UNAVAILABLE"]);
const PLATFORM_FAMILIES = new Set(["windows", "linux", "macos"]);
const STATE_ROOT_STATES = new Set(["WRITABLE", "ABSENT", "NOT_WRITABLE", "UNAVAILABLE"]);
const READMODEL_STATES = new Set(["NOT_CONFIGURED", "ABSENT", "READY", "BUSY", "INVALID", "SCOPE_MISMATCH"]);
const READMODEL_SOURCES = new Set(["vm", "aik_runtime", "dispatch", "lifecycle", "evidence", "ags_peer"]);
const VERSION_TEXT = /^\d{1,4}\.\d{1,4}\.\d{1,6}$/;
const BACKEND_CODES = new Set(["BACKEND_UNAVAILABLE", "BACKEND_INCOMPATIBLE", "BACKEND_TIMEOUT"]);
const MESSAGES = Object.freeze({
  INVALID_REQUEST: "The IPC request shape is invalid.",
  INCOMPATIBLE_PROTOCOL: "Frontend and backend IPC protocol versions differ.",
  UNTRUSTED_SENDER: "The IPC sender is not the trusted app frame.",
  UNKNOWN_WORKSPACE: "The request does not match the active workspace.",
  UNSUPPORTED_COMMAND: "The IPC command is not available to the renderer.",
  DUPLICATE_REQUEST: "The request identifier was already used.",
  BACKEND_UNAVAILABLE: "The backend cannot provide the requested state.",
  BACKEND_INCOMPATIBLE: "The engine speaks a different protocol version than this app.",
  BACKEND_TIMEOUT: "The backend did not respond within the request deadline.",
  INVALID_RESPONSE: "The backend returned an invalid response.",
  ROUTER_BUSY: "The IPC router has reached its bounded concurrency limit."
});

function ownData(object, key) {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
}

function exactPlain(value, fields) {
  // Reject Proxy *before* any operation that could invoke its traps.
  if (value === null || typeof value !== "object" || types.isProxy(value) ||
      Object.getPrototypeOf(value) !== Object.prototype) return false;
  const keys = Reflect.ownKeys(value);
  return keys.length === fields.length && keys.every((key) => typeof key === "string" &&
    fields.includes(key) && Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"));
}

function readRequest(value) {
  if (!exactPlain(value, REQUEST_FIELDS)) return {code: "INVALID_REQUEST"};
  const version = ownData(value, "protocol_version");
  if (!Number.isSafeInteger(version) || version !== VERSION) return {code: "INCOMPATIBLE_PROTOCOL"};
  const id = ownData(value, "request_id");
  if (typeof id !== "string" || !UUID_V4.test(id)) return {code: "INVALID_REQUEST"};
  const workspace = ownData(value, "workspace_id");
  if (typeof workspace !== "string" || !WORKSPACE.test(workspace)) return {code: "INVALID_REQUEST"};
  const command = ownData(value, "command");
  if (typeof command !== "string" || !COMMANDS.has(command)) return {code: "UNSUPPORTED_COMMAND"};
  if (!exactPlain(ownData(value, "payload"), [])) return {code: "INVALID_REQUEST"};
  // All accepted values are bounded ASCII primitives. Allow 160 extra bytes
  // for envelope punctuation/keys, without invoking a caller's toJSON hook.
  if (id.length + workspace.length + command.length + 160 > MAX_FRAME_BYTES) return {code: "INVALID_REQUEST"};
  return {request: Object.freeze({protocol_version: VERSION, request_id: id,
    workspace_id: workspace, command, payload: Object.freeze({})})};
}

function requestCode(value) {
  try { return readRequest(value).code ?? null; }
  catch { return "INVALID_REQUEST"; }
}

function safeRequestId(request) {
  try {
    if (!request || typeof request !== "object" || types.isProxy(request)) return null;
    const id = ownData(request, "request_id");
    return typeof id === "string" && UUID_V4.test(id) ? id : null;
  } catch { return null; }
}

function failed(request, code) {
  const selected = typeof code === "string" && Object.hasOwn(MESSAGES, code) ? code : "BACKEND_UNAVAILABLE";
  const error = {code: selected, message: MESSAGES[selected]};
  if (selected === "INCOMPATIBLE_PROTOCOL") error.supported_versions = Object.freeze([VERSION]);
  return Object.freeze({protocol_version: VERSION, request_id: safeRequestId(request),
    ok: false, error: Object.freeze(error)});
}

function statusSnapshot(value) {
  if (!exactPlain(value, ["state", "director_state"])) return null;
  const state = ownData(value, "state"), director = ownData(value, "director_state");
  if (!STATES.has(state) || !DIRECTOR_STATES.has(director)) return null;
  return Object.freeze({state, director_state: director});
}

function safeCount(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function engineInfoSnapshot(value) {
  const fields = ["engine_version", "protocol_version", "platform_family", "python_version", "frozen",
    "state_root", "readmodel", "director_state"];
  if (!exactPlain(value, fields)) return null;
  const copy = Object.fromEntries(fields.map(key => [key, ownData(value, key)]));
  if (typeof copy.engine_version !== "string" || !VERSION_TEXT.test(copy.engine_version) ||
      copy.protocol_version !== VERSION || !PLATFORM_FAMILIES.has(copy.platform_family) ||
      typeof copy.python_version !== "string" || !VERSION_TEXT.test(copy.python_version) ||
      typeof copy.frozen !== "boolean" || !STATE_ROOT_STATES.has(copy.state_root) ||
      !READMODEL_STATES.has(copy.readmodel) || !DIRECTOR_STATES.has(copy.director_state)) return null;
  return Object.freeze(copy);
}

function sourceSnapshot(value) {
  const fields = ["source", "applied_cursor", "max_seen_cursor", "open_gaps", "conflicts", "stale"];
  if (!exactPlain(value, fields)) return null;
  const copy = Object.fromEntries(fields.map(key => [key, ownData(value, key)]));
  if (!READMODEL_SOURCES.has(copy.source) || !safeCount(copy.applied_cursor) || !safeCount(copy.max_seen_cursor) ||
      copy.max_seen_cursor < copy.applied_cursor || !safeCount(copy.open_gaps) || !safeCount(copy.conflicts) ||
      typeof copy.stale !== "boolean" || copy.stale !== copy.open_gaps > 0) return null;
  return Object.freeze(copy);
}

function readmodelSourcesSnapshot(value) {
  if (!exactPlain(value, ["readmodel", "authoritative", "sources"])) return null;
  const readmodel = ownData(value, "readmodel"), list = ownData(value, "sources");
  if (!READMODEL_STATES.has(readmodel) || ownData(value, "authoritative") !== false) return null;
  if (!Array.isArray(list) || types.isProxy(list) || Object.getPrototypeOf(list) !== Array.prototype ||
      list.length > READMODEL_SOURCES.size || (readmodel !== "READY" && list.length !== 0)) return null;
  const sources = [], seen = new Set();
  for (let index = 0; index < list.length; index++) {
    const item = sourceSnapshot(ownData(list, String(index)));
    if (!item || seen.has(item.source)) return null;
    seen.add(item.source);
    sources.push(item);
  }
  return Object.freeze({readmodel, authoritative: false, sources: Object.freeze(sources)});
}

const SNAPSHOTS = Object.freeze({
  "system.status": statusSnapshot,
  "engine.info": engineInfoSnapshot,
  "readmodel.sources": readmodelSourcesSnapshot
});

class BackendError extends Error {
  // Trusted main-process readers throw this to name why the backend cannot answer.
  constructor(code) {
    super(BACKEND_CODES.has(code) ? code : "BACKEND_UNAVAILABLE");
    this.code = BACKEND_CODES.has(code) ? code : "BACKEND_UNAVAILABLE";
  }
}

function backendCode(error) {
  // Never touch a Proxy: instanceof and property reads would run its traps.
  try {
    if (error === null || typeof error !== "object" || types.isProxy(error) || !(error instanceof BackendError)) {
      return "BACKEND_UNAVAILABLE";
    }
    const code = ownData(error, "code");
    return BACKEND_CODES.has(code) ? code : "BACKEND_UNAVAILABLE";
  } catch { return "BACKEND_UNAVAILABLE"; }
}

function validStatus(value) {
  try { return statusSnapshot(value) !== null; }
  catch { return false; }
}

function sameDocument(url, appUrl) {
  // The app document, optionally with an in-page #fragment (AIK-045 hash routes).
  // Query strings, other paths and other origins stay untrusted.
  return typeof url === "string" && typeof appUrl === "string" &&
    (url === appUrl || (url.startsWith(appUrl + "#") && !url.includes("?")));
}

function trustedSender(event, webContents, appUrl) {
  try {
    return Boolean(webContents && !webContents.isDestroyed() && event && event.sender === webContents &&
      event.senderFrame === webContents.mainFrame && sameDocument(event.senderFrame?.url, appUrl) &&
      !event.senderFrame?.isDestroyed?.());
  } catch { return false; }
}

class DesktopRouter {
  constructor({readStatus = null, readers = {}, maxInflight = 8, replayLimit = 4096, replayWindowMs = 600000,
    requestTimeoutMs = 5000, clock = () => performance.now()} = {}) {
    if (readStatus !== null && typeof readStatus !== "function") throw new TypeError("readStatus must be a function");
    if (readers === null || typeof readers !== "object" || types.isProxy(readers) ||
      Object.getPrototypeOf(readers) !== Object.prototype || Object.keys(readers).some(key =>
      !READ_COMMANDS.includes(key) || typeof readers[key] !== "function") ||
      (readStatus !== null && Object.hasOwn(readers, "system.status"))) throw new TypeError("readers");
    if (!Number.isInteger(maxInflight) || maxInflight < 1 || maxInflight > 64) throw new RangeError("maxInflight");
    if (!Number.isInteger(replayLimit) || replayLimit < maxInflight || replayLimit > 65536) throw new RangeError("replayLimit");
    if (!Number.isSafeInteger(replayWindowMs) || replayWindowMs < 1000 || replayWindowMs > 86400000 ||
        typeof clock !== "function") throw new RangeError("replayWindowMs");
    if (!Number.isInteger(requestTimeoutMs) || requestTimeoutMs < 1 || requestTimeoutMs > 60000) throw new RangeError("requestTimeoutMs");
    this.readers = Object.freeze({...readers, ...(readStatus === null ? {} : {"system.status": readStatus})});
    this.maxInflight = maxInflight;
    this.replayLimit = replayLimit;
    this.replayWindowMs = replayWindowMs;
    this.requestTimeoutMs = requestTimeoutMs;
    this.clock = clock;
    this.lastNow = 0;
    this.seen = new Map();
    this.inflight = 0;
  }

  now() {
    const value = this.clock();
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0 ||
        value > Number.MAX_SAFE_INTEGER - this.replayWindowMs) throw new Error("BACKEND_UNAVAILABLE");
    this.lastNow = Math.max(this.lastNow, value); // rollback must not shorten a replay fence
    return this.lastNow;
  }

  complete(entry) {
    entry.pending = false;
    try { entry.expires = this.now() + this.replayWindowMs; }
    catch { entry.expires = Infinity; } // unknown time cannot justify deleting a fence
  }

  async readBounded(reader, request, workspaceId, entry) {
    const snapshot = SNAPSHOTS[request.command];
    this.inflight++;
    const controller = new AbortController();
    let timedOut = false, finished = false;
    const finish = () => {
      if (finished) return;
      finished = true;
      this.inflight--; this.complete(entry);
    };
    const success = value => {
      try {
        if (timedOut) return {code: "BACKEND_TIMEOUT"}; // do not read a late result's properties
        const result = snapshot(value);
        return result ? {result} : {code: "INVALID_RESPONSE"};
      } catch { return {code: "INVALID_RESPONSE"}; }
      finally { finish(); }
    };
    const failure = error => { finish(); return {code: timedOut ? "BACKEND_TIMEOUT" : backendCode(error)}; };
    let returned;
    try {
      returned = reader(workspaceId, Object.freeze({
        signal: controller.signal, requestId: request.request_id
      }));
    } catch (error) { return failure(error); }
    // Only native Promises or a plain status DTO are supported. Arbitrary
    // thenables/Proxies cannot run code through Promise assimilation.
    if (!types.isPromise(returned)) return success(returned);
    let operation;
    try { operation = Promise.prototype.then.call(returned, success, failure); }
    catch (error) { return failure(error); }
    let timer;
    const timeout = new Promise(resolve => {
      timer = setTimeout(() => {
        timedOut = true;
        resolve({code: "BACKEND_TIMEOUT"});
        controller.abort(); // cooperative cancellation; not evidence of completion
      }, this.requestTimeoutMs);
    });
    try { return await Promise.race([operation, timeout]); }
    finally { clearTimeout(timer); }
    // The operation's settlement handlers retain/release the physical slot.
    // A timeout alone never releases capacity for another stalled operation.
  }

  async dispatch(event, supplied, {getWindow, getWorkspaceId, appUrl = "aik://app/index.html"} = {}) {
    let request = supplied;
    try {
      const contents = getWindow()?.webContents;
      if (!trustedSender(event, contents, appUrl)) return failed(request, "UNTRUSTED_SENDER");
      const decoded = readRequest(supplied);
      if (decoded.code) return failed(request, decoded.code);
      request = decoded.request; // use only owned, frozen primitives after validation
      const caller = Object.freeze({sender: contents, senderFrame: contents.mainFrame});
      const handshake = request.command === "protocol.handshake";
      const workspaceId = getWorkspaceId();
      if (handshake ? request.workspace_id !== "unbound" :
          typeof workspaceId !== "string" || !WORKSPACE.test(workspaceId) ||
          request.workspace_id !== workspaceId || workspaceId === "unbound") return failed(request, "UNKNOWN_WORKSPACE");
      const now = this.now();
      for (const [id, entry] of this.seen) {
        if (!entry.pending && entry.expires <= now) this.seen.delete(id);
      }
      const key = request.request_id.toLowerCase();
      if (this.seen.has(key)) return failed(request, "DUPLICATE_REQUEST");
      if ((!handshake && this.inflight >= this.maxInflight) || this.seen.size >= this.replayLimit) return failed(request, "ROUTER_BUSY");
      const entry = {pending: true, expires: Infinity};
      this.seen.set(key, entry);
      let outcome;
      if (handshake) {
        outcome = {result: Object.freeze({ipc_version: VERSION, boundary: "RENDERER_READ_ONLY", backend_state: "NOT_QUALIFIED"})};
        this.complete(entry);
      } else if (!Object.hasOwn(this.readers, request.command)) {
        this.complete(entry);
        outcome = {code: "BACKEND_UNAVAILABLE"};
      } else {
        outcome = await this.readBounded(this.readers[request.command], request, workspaceId, entry);
      }
      if (!trustedSender(caller, getWindow()?.webContents, appUrl)) return failed(request, "UNTRUSTED_SENDER");
      if (!handshake && getWorkspaceId() !== workspaceId) return failed(request, "UNKNOWN_WORKSPACE");
      if (outcome.code) return failed(request, outcome.code);
      return Object.freeze({protocol_version: VERSION, request_id: request.request_id,
        workspace_id: request.workspace_id, command: request.command, ok: true, result: outcome.result});
    } catch { return failed(request, "BACKEND_UNAVAILABLE"); }
  }
}

function makeRendererHandler({router, getWindow, getWorkspaceId, appUrl}) {
  if (!(router instanceof DesktopRouter) || typeof getWindow !== "function" || typeof getWorkspaceId !== "function") {
    throw new TypeError("Trusted main-process bindings required");
  }
  return (event, request) => router.dispatch(event, request, {getWindow, getWorkspaceId, appUrl});
}

module.exports = {CHANNEL, VERSION, MAX_FRAME_BYTES, READ_COMMANDS, requestCode, failed, validStatus,
  trustedSender, BackendError, DesktopRouter, makeRendererHandler};
