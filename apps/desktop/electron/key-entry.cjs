"use strict";

// AIK-046/047: the main-owned API key entry window.
//
// The renderer only asks for this window (connections.request_key_entry) and
// later receives a secret-free result. The key is typed into a separate
// sandboxed window whose preload exposes nothing but context/submit/cancel on
// its own channel; main checks that each message comes from that window's
// main frame at the key entry URL, hands the key to the engine once and drops
// its reference. The key is never logged, echoed or returned.
const {types} = require("node:util");
const {connectionWithCheck} = require("../../../src/aik_ipc/connections.cjs");

const KEY_CHANNEL = "aik:key-entry:v1";
const KEY_URL = "aik://app/key-entry.html";
const MAX_SECRET_BYTES = 16384;
// Codes the window may show so the user can correct the input and retry.
const RETRY_CODES = new Set(["INVALID_SECRET", "VAULT_LOCKED", "VAULT_UNAVAILABLE", "VAULT_ERROR", "STORE_BUSY",
  "DUPLICATE_CONNECTION", "REVISION_CONFLICT", "UNKNOWN_CONNECTION", "UNSUPPORTED_PROVIDER", "INVALID_LABEL",
  "SECRET_CLEANUP_UNCONFIRMED", "STORE_UNAVAILABLE", "CONNECTIONS_NOT_CONFIGURED", "BACKEND_UNAVAILABLE",
  "BACKEND_TIMEOUT", "KEY_ENTRY_BUSY"]);

class KeyEntryError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function plain(value, fields) {
  if (value === null || typeof value !== "object" || types.isProxy(value) ||
      Object.getPrototypeOf(value) !== Object.prototype) return false;
  const keys = Reflect.ownKeys(value);
  return keys.length === fields.length && keys.every((key) => typeof key === "string" && fields.includes(key) &&
    Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"));
}

function acceptableSecret(value) {
  if (typeof value !== "string" || value.length === 0 || Buffer.byteLength(value, "utf8") > MAX_SECRET_BYTES) {
    return false;
  }
  return value.trim() === value && !/[\u0000-\u001f\u007f]/.test(value);
}

class KeyEntry {
  /**
   * @param createWindow () => BrowserWindow-like (sandboxed, key entry preload)
   * @param submit async (context, secret) => engine result {connection, check}; throws {code}
   */
  constructor({createWindow, submit, url = KEY_URL}) {
    if (typeof createWindow !== "function" || typeof submit !== "function") throw new TypeError("KeyEntry bindings");
    this.createWindow = createWindow;
    this.submit = submit;
    this.url = url;
    this.current = null;
  }

  /** Open the window for ``context``; resolves with a secret-free outcome when it closes. */
  open(context, signal = null) {
    if (this.current) return Promise.reject(new KeyEntryError("KEY_ENTRY_BUSY"));
    let window;
    try { window = this.createWindow(); }
    catch { return Promise.reject(new KeyEntryError("KEY_ENTRY_UNAVAILABLE")); }
    return new Promise((resolve, reject) => {
      const entry = {window, context: Object.freeze({...context}), submitting: false, settled: false};
      const settle = (outcome, error = null) => {
        if (entry.settled) return;
        entry.settled = true;
        if (this.current === entry) this.current = null;
        if (!window.isDestroyed()) window.close();
        if (error) reject(error); else resolve(outcome);
      };
      entry.settle = settle;
      this.current = entry;
      window.once("closed", () => settle(Object.freeze({outcome: "cancelled", connection: null, check: null})));
      if (signal) {
        if (signal.aborted) return settle(null, new KeyEntryError("BACKEND_TIMEOUT"));
        signal.addEventListener("abort", () => settle(null, new KeyEntryError("BACKEND_TIMEOUT")), {once: true});
      }
      Promise.resolve().then(() => window.loadURL(this.url))
        .catch(() => settle(null, new KeyEntryError("KEY_ENTRY_UNAVAILABLE")));
    });
  }

  trusted(event) {
    const entry = this.current;
    try {
      const contents = entry?.window?.webContents;
      return Boolean(entry && !entry.window.isDestroyed() && event && event.sender === contents &&
        event.senderFrame === contents.mainFrame && event.senderFrame?.url === this.url);
    } catch { return false; }
  }

  /** ipcMain.handle(KEY_CHANNEL, ...) handler. */
  async handle(event, message) {
    if (!this.trusted(event)) return {ok: false, code: "UNTRUSTED_SENDER"};
    const entry = this.current;
    if (plain(message, ["op"]) && message.op === "context") {
      const {mode, display_name: displayName, account_label: label} = entry.context;
      return {ok: true, context: {mode, display_name: displayName, account_label: label}};
    }
    if (plain(message, ["op"]) && message.op === "cancel") {
      entry.settle(Object.freeze({outcome: "cancelled", connection: null, check: null}));
      return {ok: true};
    }
    if (!plain(message, ["op", "secret"]) || message.op !== "submit") return {ok: false, code: "INVALID_REQUEST"};
    let secret = Object.getOwnPropertyDescriptor(message, "secret").value;
    if (!acceptableSecret(secret)) {
      secret = null;
      return {ok: false, code: "INVALID_SECRET"};
    }
    if (entry.submitting) return {ok: false, code: "KEY_ENTRY_BUSY"};
    entry.submitting = true;
    let result, code = null;
    try {
      result = await this.submit(entry.context, secret);
    } catch (error) {
      code = typeof error?.code === "string" && RETRY_CODES.has(error.code) ? error.code : "BACKEND_UNAVAILABLE";
    } finally {
      secret = null;
      entry.submitting = false;
    }
    if (entry.settled) return {ok: false, code: "BACKEND_TIMEOUT"};
    if (code) return {ok: false, code};
    const snapshot = connectionWithCheck(result);
    if (!snapshot) return {ok: false, code: "BACKEND_UNAVAILABLE"};
    entry.settle(Object.freeze({outcome: "saved", connection: snapshot.connection, check: snapshot.check}));
    return {ok: true};
  }

  close() {
    if (this.current) this.current.settle(Object.freeze({outcome: "cancelled", connection: null, check: null}));
  }
}

module.exports = {KEY_CHANNEL, KEY_URL, MAX_SECRET_BYTES, KeyEntry, KeyEntryError, acceptableSecret};
