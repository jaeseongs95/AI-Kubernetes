"use strict";

// AIK-045: main-process client of the stdio desktop engine (src/aik_desktop_engine).
// Generalized from the AIK-002 spike SidecarClient. The renderer never sees this
// object; it reaches the engine only through the DesktopRouter readers.
const {spawn} = require("node:child_process");
const {randomBytes, randomUUID} = require("node:crypto");
const path = require("node:path");

const PROTOCOL_VERSION = 1;
const MAX_FRAME = 65536;
const MAX_INFLIGHT = 4;
const COMMANDS = new Set(["system.status", "engine.info", "readmodel.sources",
  "connections.catalog", "connections.list", "connections.impact", "connections.login_state",
  "connections.add_key", "connections.rotate_key", "connections.check", "connections.rename", "connections.delete",
  "connections.start_login", "connections.cancel_login", "connections.complete_login", "connections.logout",
  "workspaces.list", "workspaces.scope", "workspaces.open", "workspaces.create", "workspaces.select",
  "workspaces.rebind_root", "workspaces.archive", "workspaces.restore"]);
// The engine runs connection and workspace commands one at a time on its own lane, so one
// may wait behind a vendor check or official login; status queries are not delayed.
const SLOW_TIMEOUT_MS = 150000;
const ERROR_CODE = /^[A-Z][A-Z0-9_]{1,63}$/;

class EngineError extends Error {
  // ``detail`` is the engine's own fixed code when the engine answered with an error.
  constructor(code, detail = null) {
    super(code);
    this.code = code;
    this.detail = detail;
  }
}

function plain(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    Object.getPrototypeOf(value) === Object.prototype;
}

function exact(value, names) {
  return plain(value) && Object.keys(value).length === names.length && names.every((key) => Object.hasOwn(value, key));
}

class EngineClient {
  /** One engine process and one bootstrap session. A failed client is never reused. */
  constructor({executable, args = [], env = {}, spawnOptions = {}, timeoutMs = 15000, protocolVersion = PROTOCOL_VERSION,
    spawnProcess = spawn}) {
    if (typeof executable !== "string" || !path.isAbsolute(executable)) throw new EngineError("ENGINE_PATH_REQUIRED");
    this.timeoutMs = timeoutMs;
    this.protocolVersion = protocolVersion;
    this.nonce = randomBytes(32).toString("hex");
    this.pending = new Map();
    this.state = "starting";
    this.failure = null;
    this.info = null;
    this.buffer = Buffer.alloc(0);
    this.child = spawnProcess(executable, args, {
      ...spawnOptions, stdio: ["pipe", "pipe", "pipe"], cwd: path.dirname(executable), env, shell: false
    });
    this.exited = new Promise((resolve) => this.child.once("close", () => { this.closedProcess = true; resolve(); }));
    this.ready = new Promise((resolve, reject) => { this.resolveReady = resolve; this.rejectReady = reject; });
    this.ready.catch(() => {}); // observed through request()
    this.startTimer = setTimeout(() => this.fail("ENGINE_START_TIMEOUT"), timeoutMs);
    this.child.on("error", () => this.fail("ENGINE_START_FAILED"));
    this.child.on("exit", () => this.fail("ENGINE_EXITED"));
    this.child.stdin.on("error", () => this.fail("ENGINE_PIPE_CLOSED"));
    this.child.stderr.on("data", () => {}); // drained, never logged or shown
    this.child.stdout.on("data", (chunk) => this.consume(chunk));
    this.child.stdin.write(JSON.stringify({kind: "bootstrap", protocol_version: protocolVersion, nonce: this.nonce}) + "\n");
  }

  handshake(message) {
    if (exact(message, ["kind", "supported_versions"]) && message.kind === "incompatible") {
      return this.fail("ENGINE_INCOMPATIBLE");
    }
    if (!exact(message, ["kind", "protocol_version", "pid", "engine_version"]) || message.kind !== "ready" ||
        message.pid !== this.child.pid || typeof message.engine_version !== "string") {
      return this.fail("ENGINE_INVALID_RESPONSE");
    }
    if (message.protocol_version !== this.protocolVersion) return this.fail("ENGINE_INCOMPATIBLE");
    clearTimeout(this.startTimer);
    this.state = "ready";
    this.info = Object.freeze({pid: message.pid, engine_version: message.engine_version});
    this.resolveReady(this.info);
  }

  consume(chunk) {
    if (this.state === "failed" || this.state === "closed") return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    let newline;
    while ((newline = this.buffer.indexOf(10)) !== -1) {
      const line = this.buffer.subarray(0, newline);
      this.buffer = this.buffer.subarray(newline + 1);
      if (line.length > MAX_FRAME) return this.fail("ENGINE_INVALID_RESPONSE");
      let message;
      try { message = JSON.parse(line.toString("utf8")); } catch { return this.fail("ENGINE_INVALID_RESPONSE"); }
      if (this.state === "starting") {
        this.handshake(message);
        if (this.state !== "ready") return;
        continue;
      }
      this.answer(message);
      if (this.state !== "ready") return;
    }
    if (this.buffer.length > MAX_FRAME) this.fail("ENGINE_INVALID_RESPONSE");
  }

  answer(message) {
    const pending = plain(message) ? this.pending.get(message.request_id) : undefined;
    if (!pending || message.protocol_version !== this.protocolVersion || message.command !== pending.command) {
      return this.fail("ENGINE_INVALID_RESPONSE");
    }
    let outcome;
    if (message.ok === true && exact(message, ["protocol_version", "request_id", "command", "ok", "result"]) &&
        plain(message.result)) {
      outcome = {result: message.result};
    } else if (message.ok === false && exact(message, ["protocol_version", "request_id", "command", "ok", "error"]) &&
        exact(message.error, ["code"]) && typeof message.error.code === "string" && ERROR_CODE.test(message.error.code)) {
      outcome = {error: message.error.code === "INCOMPATIBLE_PROTOCOL" ? "ENGINE_INCOMPATIBLE" : "ENGINE_REJECTED",
        detail: message.error.code};
    } else {
      return this.fail("ENGINE_INVALID_RESPONSE");
    }
    clearTimeout(pending.timer);
    this.pending.delete(message.request_id);
    if (outcome.error) pending.reject(new EngineError(outcome.error, outcome.detail));
    else pending.resolve(outcome.result);
  }

  async request(command, payload = {}) {
    if (!COMMANDS.has(command)) throw new EngineError("ENGINE_UNSUPPORTED_COMMAND");
    if (!plain(payload)) throw new EngineError("ENGINE_INVALID_PAYLOAD");
    await this.ready.catch(() => { throw new EngineError(this.failure ?? "ENGINE_UNAVAILABLE"); });
    if (this.state !== "ready") throw new EngineError(this.failure ?? "ENGINE_UNAVAILABLE");
    if (this.pending.size >= MAX_INFLIGHT) throw new EngineError("ENGINE_BUSY");
    const requestId = randomUUID();
    return new Promise((resolve, reject) => {
      const laned = command.startsWith("connections.") || command.startsWith("workspaces.");
      const timeoutMs = laned ? Math.max(this.timeoutMs, SLOW_TIMEOUT_MS) : this.timeoutMs;
      const timer = setTimeout(() => this.fail("ENGINE_REQUEST_TIMEOUT"), timeoutMs);
      this.pending.set(requestId, {command, resolve, reject, timer});
      this.child.stdin.write(JSON.stringify({protocol_version: this.protocolVersion, request_id: requestId, command,
        payload, capability: this.nonce}) + "\n");
    });
  }

  fail(code) {
    if (this.state === "failed" || this.state === "closed") return;
    this.state = "failed";
    this.failure = code;
    clearTimeout(this.startTimer);
    this.rejectReady(new EngineError(code));
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new EngineError(code));
    }
    this.pending.clear();
    this.child.stdin.destroy();
    if (this.child.exitCode === null && this.child.signalCode === null) this.child.kill();
    this.nonce = null;
  }

  async close() {
    if (this.state !== "failed") this.state = "closed";
    clearTimeout(this.startTimer);
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new EngineError("ENGINE_CLOSED"));
    }
    this.pending.clear();
    if (this.closedProcess) return;
    if (!this.child.stdin.destroyed) this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill(), 1000);
    await this.exited;
    clearTimeout(timer);
  }
}

class EngineSupervisor {
  /**
   * Starts the engine on demand and starts a new one after a failure, at most
   * once per ``restartDelayMs``. A version mismatch is not retried: restarting
   * the same engine cannot fix it.
   */
  constructor({launch, restartDelayMs = 5000, clock = () => Date.now(), createClient = (options) => new EngineClient(options)}) {
    if (typeof launch !== "function") throw new TypeError("launch");
    this.launch = launch;
    this.restartDelayMs = restartDelayMs;
    this.clock = clock;
    this.createClient = createClient;
    this.client = null;
    this.startedAt = -Infinity;
    this.closing = false;
  }

  current() {
    if (this.closing) throw new EngineError("ENGINE_CLOSED");
    const client = this.client;
    if (client && client.state !== "failed") return client;
    if (client?.failure === "ENGINE_INCOMPATIBLE") throw new EngineError("ENGINE_INCOMPATIBLE");
    if (this.clock() - this.startedAt < this.restartDelayMs) {
      throw new EngineError(client?.failure ?? "ENGINE_UNAVAILABLE");
    }
    this.startedAt = this.clock();
    let options;
    try { options = this.launch(); }
    catch (error) {
      this.client = null;
      throw new EngineError(error instanceof EngineError ? error.code : "ENGINE_NOT_CONFIGURED");
    }
    this.client = this.createClient(options);
    return this.client;
  }

  async request(command, payload = {}) {
    return this.current().request(command, payload);
  }

  async close() {
    this.closing = true;
    if (this.client) await this.client.close();
  }
}

module.exports = {PROTOCOL_VERSION, EngineClient, EngineSupervisor, EngineError};
