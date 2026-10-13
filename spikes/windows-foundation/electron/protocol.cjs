"use strict";

const VERSION = 1;
const WORKSPACE = "foundation-spike";
const CHANNEL = "aik:foundation-probe";
const COMMANDS = new Set(["health", "worker_probe", "vault_probe", "auth_vault_probe"]);
const AUTH_VAULT_CASES = ["dpapi_round_trip", "lock_contention", "malformed_reference", "truncated_blob", "foreign_blob_simulated", "deleted_reference", "auth_clock_expiry", "credential_removed", "output_redaction", "other_user_profile"];
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
}

function exact(value, names) {
  return object(value) && Object.keys(value).length === names.length && names.every((key) => Object.hasOwn(value, key));
}

function validateRequest(request) {
  if (!exact(request, ["protocol_version", "request_id", "command", "workspace_id", "payload"])) return "INVALID_REQUEST";
  if (request.protocol_version !== VERSION) return "INCOMPATIBLE_PROTOCOL";
  if (typeof request.request_id !== "string" || !UUID.test(request.request_id)) return "INVALID_REQUEST";
  if (request.workspace_id !== WORKSPACE) return "UNKNOWN_WORKSPACE";
  if (!COMMANDS.has(request.command)) return "UNSUPPORTED_COMMAND";
  if (!exact(request.payload, [])) return "INVALID_REQUEST";
  return null;
}

function trustedSender(event, webContents, url) {
  return Boolean(webContents && !webContents.isDestroyed() && event.sender === webContents &&
    event.senderFrame === webContents.mainFrame && event.senderFrame?.url === url);
}

function failure(request, code) {
  return {
    protocol_version: VERSION,
    request_id: typeof request?.request_id === "string" && UUID.test(request.request_id) ? request.request_id : null,
    workspace_id: WORKSPACE,
    command: COMMANDS.has(request?.command) ? request.command : null,
    ok: false,
    error: {code, message: "요청을 처리하지 못했습니다. 오류 코드를 확인하세요."}
  };
}

function validResult(command, value) {
  if (!object(value) || !["PASS", "NOT_RUN", "FAIL"].includes(value.status) || typeof value.code !== "string") return false;
  if (command === "health") {
    return exact(value, ["status", "code", "pid", "platform", "python_version", "frozen", "product_integration"]) &&
      value.status === "PASS" && value.code === "SIDECAR_READY" && Number.isSafeInteger(value.pid) && value.pid > 0 &&
      ["win32", "linux", "darwin"].includes(value.platform) && typeof value.python_version === "string" && /^\d+\.\d+\.\d+$/.test(value.python_version) &&
      typeof value.frozen === "boolean" && value.product_integration === "NOT_RUN";
  }
  if (command === "worker_probe") {
    return exact(value, ["status", "code", "pid", "parent_pid", "artifact_bytes", "artifact_sha256", "temporary_artifact_removed"]) &&
      value.status === "PASS" && value.code === "WORKER_PROBE_OK" &&
      Number.isSafeInteger(value.pid) && value.pid > 0 && Number.isSafeInteger(value.parent_pid) && value.parent_pid > 0 &&
      Number.isSafeInteger(value.artifact_bytes) && value.artifact_bytes > 0 &&
      typeof value.artifact_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.artifact_sha256) && value.temporary_artifact_removed === true;
  }
  if (command === "auth_vault_probe") {
    // AIK-022 synthetic failure paths: fixed case codes only, never values or paths.
    return exact(value, ["status", "backend", "code", "cases"]) && value.backend === "windows-user-dpapi" &&
      /^[A-Z0-9_]{1,64}$/.test(value.code) && object(value.cases) &&
      Object.keys(value.cases).every((name) => AUTH_VAULT_CASES.includes(name) && /^[A-Z0-9_]{1,64}$/.test(value.cases[name])) &&
      (value.status === "NOT_RUN" ? Object.keys(value.cases).length === 0 : exact(value.cases, AUTH_VAULT_CASES)) &&
      (value.status !== "PASS" || value.code === "AUTH_VAULT_FAILURE_PATHS_OK");
  }
  return exact(value, ["status", "backend", "code"]) && value.backend === "windows-user-dpapi" &&
    /^[A-Z0-9_]{1,64}$/.test(value.code) &&
    (value.status !== "PASS" || value.code === "VAULT_ROUNDTRIP_OK");
}

function validateResponse(response, request) {
  if (!object(response) || response.protocol_version !== VERSION || response.request_id !== request.request_id ||
      response.workspace_id !== WORKSPACE || response.command !== request.command) return false;
  if (response.ok === true) {
    return exact(response, ["protocol_version", "request_id", "workspace_id", "command", "ok", "result"]) &&
      validResult(request.command, response.result);
  }
  return response.ok === false && exact(response, ["protocol_version", "request_id", "workspace_id", "command", "ok", "error"]) &&
    exact(response.error, ["code", "message"]) && typeof response.error.code === "string" && /^[A-Z0-9_]{1,64}$/.test(response.error.code) &&
    typeof response.error.message === "string" && response.error.message.length < 256;
}

module.exports = {VERSION, WORKSPACE, CHANNEL, validateRequest, validateResponse, trustedSender, failure};
