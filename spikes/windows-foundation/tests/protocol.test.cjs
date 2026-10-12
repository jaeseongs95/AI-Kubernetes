"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {validateRequest, validateResponse, trustedSender, failure} = require("../electron/protocol.cjs");

function request() {
  return {protocol_version: 1, request_id: randomUUID(), workspace_id: "foundation-spike", command: "health", payload: {}};
}

test("IPC rejects unknown commands, credentials, scopes and protocol versions", () => {
  assert.equal(validateRequest(request()), null);
  for (const [key, value, expected] of [
    ["command", "shell", "UNSUPPORTED_COMMAND"],
    ["workspace_id", "other", "UNKNOWN_WORKSPACE"],
    ["protocol_version", 2, "INCOMPATIBLE_PROTOCOL"],
    ["protocol_version", true, "INCOMPATIBLE_PROTOCOL"],
    ["payload", {path: "C:/Users"}, "INVALID_REQUEST"],
    ["request_id", "forged", "INVALID_REQUEST"],
  ]) assert.equal(validateRequest({...request(), [key]: value}), expected);
  assert.equal(validateRequest({...request(), capability: "renderer-must-not-set"}), "INVALID_REQUEST");
});

test("only the exact app window, main frame and URL are trusted", () => {
  const frame = {url: "aik://app/index.html"};
  const contents = {mainFrame: frame, isDestroyed: () => false};
  assert.equal(trustedSender({sender: contents, senderFrame: frame}, contents, frame.url), true);
  assert.equal(trustedSender({sender: {}, senderFrame: frame}, contents, frame.url), false);
  assert.equal(trustedSender({sender: contents, senderFrame: {url: frame.url}}, contents, frame.url), false);
  assert.equal(trustedSender({sender: contents, senderFrame: frame}, contents, "https://example.test"), false);
});

test("response schema rejects secret fields and mismatched request identity", () => {
  const req = request();
  const result = {status: "PASS", code: "SIDECAR_READY", pid: 123, platform: "win32", python_version: "3.14.8", frozen: true, product_integration: "NOT_RUN"};
  const response = {...req, ok: true, result};
  delete response.payload;
  assert.equal(validateResponse(response, req), true);
  assert.equal(validateResponse({...response, result: {...result, secret: "canary"}}, req), false);
  assert.equal(validateResponse({...response, request_id: randomUUID()}, req), false);
  assert.equal(validateResponse({...response, result: {...result, product_integration: "PASS"}}, req), false);
  assert.equal(validateResponse({...response, result: {...result, python_version: {toString: null, valueOf: null}}}, req), false);
  const error = {...req, ok: false, error: {code: {toString: null, valueOf: null}, message: "invalid"}};
  delete error.payload;
  assert.equal(validateResponse(error, req), false);
});

test("sanitized failures never echo arbitrary request values", () => {
  const response = failure({request_id: "secret-canary", command: "secret-canary"}, "INVALID_REQUEST");
  assert.equal(JSON.stringify(response).includes("secret-canary"), false);
});
