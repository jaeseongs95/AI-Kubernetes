"use strict";

const {spawn} = require("node:child_process");
const {randomBytes} = require("node:crypto");
const path = require("node:path");
const {VERSION, validateRequest, validateResponse} = require("./protocol.cjs");
const MAX_FRAME = 65536;

function minimalEnvironment(source = process.env) {
  const allowed = new Set(["SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LOCALAPPDATA", "USERPROFILE", "APPDATA", "PATH", "LANG", "LC_ALL"]);
  return Object.fromEntries(Object.entries(source).filter(([key]) => allowed.has(key.toUpperCase())));
}

class SidecarClient {
  constructor({executable, args = [], timeoutMs = 15000}) {
    if (!path.isAbsolute(executable)) throw new Error("EXECUTABLE_PATH_REQUIRED");
    this.timeoutMs = timeoutMs;
    this.nonce = randomBytes(32).toString("hex");
    this.pending = new Map();
    this.closed = false;
    this.buffer = Buffer.alloc(0);
    this.child = spawn(executable, args, {
      stdio: ["pipe", "pipe", "pipe"],
      cwd: path.dirname(executable),
      windowsHide: true,
      env: minimalEnvironment()
    });
    this.exited = new Promise((resolve) => this.child.once("close", () => {
      this.exitedReceived = true;
      resolve();
    }));
    this.ready = new Promise((resolve, reject) => {
      this.resolveReady = resolve;
      this.rejectReady = reject;
    });
    // Prevent an unhandled rejection between process startup and the first UI call.
    this.ready.catch(() => {});
    this.startTimer = setTimeout(() => this.fail("SIDECAR_START_TIMEOUT"), timeoutMs);
    this.child.on("error", () => this.fail("SIDECAR_START_FAILED"));
    this.child.on("exit", () => this.fail("SIDECAR_EXITED"));
    this.child.stdin.on("error", () => this.fail("SIDECAR_PIPE_CLOSED"));
    this.child.stderr.on("data", () => {}); // Drain, without returning or logging raw child diagnostics.
    this.child.stdout.on("data", (chunk) => this.consume(chunk));
    this.child.stdin.write(JSON.stringify({kind: "bootstrap", protocol_version: VERSION, nonce: this.nonce}) + "\n");
  }

  consume(chunk) {
    if (this.closed) return;
    this.buffer = Buffer.concat([this.buffer, chunk]);
    if (this.buffer.length > MAX_FRAME * 2) return this.fail("SIDECAR_INVALID_RESPONSE");
    let newline;
    while ((newline = this.buffer.indexOf(10)) !== -1) {
      const line = this.buffer.subarray(0, newline);
      this.buffer = this.buffer.subarray(newline + 1);
      if (line.length > MAX_FRAME) return this.fail("SIDECAR_INVALID_RESPONSE");
      let message;
      try { message = JSON.parse(line.toString("utf8")); } catch { return this.fail("SIDECAR_INVALID_RESPONSE"); }
      if (!this.readyReceived) {
        if (!message || Object.keys(message).sort().join(",") !== "kind,pid,protocol_version" ||
            message.kind !== "ready" || message.protocol_version !== VERSION || message.pid !== this.child.pid) {
          return this.fail("SIDECAR_INVALID_RESPONSE");
        }
        this.readyReceived = true;
        clearTimeout(this.startTimer);
        this.resolveReady(message);
        continue;
      }
      const pending = this.pending.get(message?.request_id);
      let valid = false;
      try { valid = Boolean(pending && validateResponse(message, pending.request)); } catch {}
      if (!valid) return this.fail("SIDECAR_INVALID_RESPONSE");
      clearTimeout(pending.timer);
      this.pending.delete(message.request_id);
      if (message.ok === false) {
        message.error.message = "요청을 처리하지 못했습니다. 오류 코드를 확인하세요.";
      }
      pending.resolve(message);
    }
    if (this.buffer.length > MAX_FRAME) this.fail("SIDECAR_INVALID_RESPONSE");
  }

  async request(request) {
    const error = validateRequest(request);
    if (error) throw new Error(error);
    await this.ready;
    if (this.closed) throw new Error("SIDECAR_UNAVAILABLE");
    if (this.pending.size >= 4 || this.pending.has(request.request_id)) throw new Error("SIDECAR_BUSY");
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.fail("SIDECAR_REQUEST_TIMEOUT"), this.timeoutMs);
      this.pending.set(request.request_id, {request, resolve, reject, timer});
      this.child.stdin.write(JSON.stringify({...request, capability: this.nonce}) + "\n");
    });
  }

  fail(code) {
    if (this.closed) return;
    this.closed = true;
    clearTimeout(this.startTimer);
    this.rejectReady(new Error(code));
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(code));
    }
    this.pending.clear();
    this.child.stdin.destroy();
    if (this.child.exitCode === null) this.child.kill();
    this.nonce = null;
  }

  async close() {
    if (this.exitedReceived) return;
    if (!this.child.stdin.destroyed) this.child.stdin.end();
    const timer = setTimeout(() => this.child.kill(), 1000);
    await this.exited;
    clearTimeout(timer);
  }
}

module.exports = {SidecarClient, minimalEnvironment};
