"use strict";

// AIK-004: a renderer-facing, read-only IPC boundary. All decisions about
// the caller and active workspace come from the trusted Electron main process.
// Do not expose dispatch() or a generic invoke() method in a renderer preload.
const {performance} = require("node:perf_hooks");

const CHANNEL = "aik:desktop:v1";
const VERSION = 1;
const MAX_FRAME_BYTES = 16384;
const REQUEST_FIELDS = ["protocol_version", "request_id", "workspace_id", "command", "payload"];
const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const WORKSPACE = /^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$/;
const COMMANDS = new Set(["protocol.handshake", "system.status"]);
const STATES = new Set(["READY", "DEGRADED", "BLOCKED"]);
const DIRECTOR_STATES = new Set(["NOT_CONFIGURED", "STOPPED", "RUNNING", "UNAVAILABLE"]);
const MESSAGES = Object.freeze({
  INVALID_REQUEST: "The IPC request shape is invalid.",
  INCOMPATIBLE_PROTOCOL: "Frontend and backend IPC protocol versions differ.",
  UNTRUSTED_SENDER: "The IPC sender is not the trusted app frame.",
  UNKNOWN_WORKSPACE: "The request does not match the active workspace.",
  UNSUPPORTED_COMMAND: "The IPC command is not available to the renderer.",
  DUPLICATE_REQUEST: "The request identifier was already used.",
  BACKEND_UNAVAILABLE: "The backend cannot provide the requested state.",
  INVALID_RESPONSE: "The backend returned an invalid response.",
  ROUTER_BUSY: "The IPC router has reached its bounded concurrency limit."
});

function ownData(object, key) {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
}

function exactPlain(value, fields) {
  if (value === null || typeof value !== "object" || Object.getPrototypeOf(value) !== Object.prototype) return false;
  const keys = Reflect.ownKeys(value);
  return keys.length === fields.length && keys.every((key) => typeof key === "string" &&
    fields.includes(key) && Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"));
}

function requestCode(value) {
  try {
    if (!exactPlain(value, REQUEST_FIELDS)) return "INVALID_REQUEST";
    const version = ownData(value, "protocol_version");
    if (!Number.isSafeInteger(version) || version !== VERSION) return "INCOMPATIBLE_PROTOCOL";
    const id = ownData(value, "request_id");
    if (typeof id !== "string" || !UUID_V4.test(id)) return "INVALID_REQUEST";
    const workspace = ownData(value, "workspace_id");
    if (typeof workspace !== "string" || !WORKSPACE.test(workspace)) return "INVALID_REQUEST";
    const command = ownData(value, "command");
    if (typeof command !== "string" || !COMMANDS.has(command)) return "UNSUPPORTED_COMMAND";
    if (!exactPlain(ownData(value, "payload"), [])) return "INVALID_REQUEST";
    if (Buffer.byteLength(JSON.stringify(value), "utf8") > MAX_FRAME_BYTES) return "INVALID_REQUEST";
    return null;
  } catch {
    return "INVALID_REQUEST";
  }
}

function safeRequestId(request) {
  try {
    if (!request || typeof request !== "object") return null;
    const id = ownData(request, "request_id");
    return typeof id === "string" && UUID_V4.test(id) ? id : null;
  } catch { return null; }
}

function failed(request, code) {
  const error = {code, message: MESSAGES[code] || MESSAGES.BACKEND_UNAVAILABLE};
  if (code === "INCOMPATIBLE_PROTOCOL") error.supported_versions = [VERSION];
  return {protocol_version: VERSION, request_id: safeRequestId(request), ok: false, error};
}

function validStatus(value) {
  return exactPlain(value, ["state", "director_state"]) &&
    STATES.has(ownData(value, "state")) && DIRECTOR_STATES.has(ownData(value, "director_state"));
}

function trustedSender(event, webContents, appUrl) {
  try {
    return Boolean(webContents && !webContents.isDestroyed() && event && event.sender === webContents &&
      event.senderFrame === webContents.mainFrame && event.senderFrame?.url === appUrl &&
      !event.senderFrame?.isDestroyed?.());
  } catch { return false; }
}

class DesktopRouter {
  constructor({readStatus = null, maxInflight = 8, replayLimit = 4096, replayWindowMs = 600000, clock = () => performance.now()} = {}) {
    if (readStatus !== null && typeof readStatus !== "function") throw new TypeError("readStatus must be a function");
    if (!Number.isInteger(maxInflight) || maxInflight < 1 || maxInflight > 64) throw new RangeError("maxInflight");
    if (!Number.isInteger(replayLimit) || replayLimit < maxInflight) throw new RangeError("replayLimit");
    if (!Number.isSafeInteger(replayWindowMs) || replayWindowMs < 1000 || typeof clock !== "function") throw new RangeError("replayWindowMs");
    this.readStatus = readStatus;
    this.maxInflight = maxInflight;
    this.replayLimit = replayLimit;
    this.replayWindowMs = replayWindowMs;
    this.clock = clock;
    this.seen = new Map();
    this.inflight = 0;
  }

  async dispatch(event, request, {getWindow, getWorkspaceId, appUrl = "aik://app/index.html"}) {
    // These callbacks must be owned by Electron main, NEVER by an IPC payload.
    let webContents;
    try { webContents = getWindow()?.webContents; } catch { return failed(request, "BACKEND_UNAVAILABLE"); }
    if (!trustedSender(event, webContents, appUrl)) return failed(request, "UNTRUSTED_SENDER");
    const bad = requestCode(request);
    if (bad) return failed(request, bad);
    const command = request.command;
    let workspaceId;
    try { workspaceId = getWorkspaceId(); } catch { return failed(request, "BACKEND_UNAVAILABLE"); }
    if (command === "protocol.handshake") {
      if (request.workspace_id !== "unbound") return failed(request, "UNKNOWN_WORKSPACE");
    } else if (typeof workspaceId !== "string" || !WORKSPACE.test(workspaceId) ||
               request.workspace_id !== workspaceId || workspaceId === "unbound") {
      return failed(request, "UNKNOWN_WORKSPACE");
    }
    const now = this.clock();
    for (const [id, expires] of this.seen) {
      if (expires <= now) this.seen.delete(id);
      else break; // insertion order and fixed replay window
    }
    if (this.seen.has(request.request_id)) return failed(request, "DUPLICATE_REQUEST");
    if (this.inflight >= this.maxInflight || this.seen.size >= this.replayLimit) {
      return failed(request, "ROUTER_BUSY"); // fail closed rather than silently evict replay evidence
    }
    this.seen.set(request.request_id, now + this.replayWindowMs);
    this.inflight += 1;
    try {
      let result;
      if (command === "protocol.handshake") {
        result = {ipc_version: VERSION, boundary: "RENDERER_READ_ONLY", backend_state: "NOT_QUALIFIED"};
      } else {
        if (this.readStatus === null) return failed(request, "BACKEND_UNAVAILABLE");
        result = await this.readStatus(workspaceId);
        if (!validStatus(result)) return failed(request, "INVALID_RESPONSE");
      }
      // An asynchronous status lookup must not return stale workspace data.
      if (!trustedSender(event, getWindow()?.webContents, appUrl)) return failed(request, "UNTRUSTED_SENDER");
      if (command !== "protocol.handshake" && getWorkspaceId() !== workspaceId) {
        return failed(request, "UNKNOWN_WORKSPACE");
      }
      return {protocol_version: VERSION, request_id: request.request_id, workspace_id: request.workspace_id,
        command, ok: true, result};
    } catch {
      return failed(request, "BACKEND_UNAVAILABLE"); // never serialize exception or secret-bearing stack
    } finally {
      this.inflight -= 1;
    }
  }
}

function makeRendererHandler({router, getWindow, getWorkspaceId, appUrl}) {
  if (!(router instanceof DesktopRouter) || typeof getWindow !== "function" || typeof getWorkspaceId !== "function") {
    throw new TypeError("Trusted main-process bindings required");
  }
  return (event, request) => router.dispatch(event, request, {getWindow, getWorkspaceId, appUrl});
}

module.exports = {CHANNEL, VERSION, MAX_FRAME_BYTES, requestCode, failed, validStatus,
  trustedSender, DesktopRouter, makeRendererHandler};
