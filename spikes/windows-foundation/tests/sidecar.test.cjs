"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const path = require("node:path");
const {SidecarClient, minimalEnvironment} = require("../electron/sidecar.cjs");
const entry = path.resolve(__dirname, "../python/sidecar_entry.py");
const python = process.env.AIK_SPIKE_PYTHON;
const request = (command) => ({protocol_version: 1, request_id: randomUUID(), workspace_id: "foundation-spike", command, payload: {}});

test("sidecar child does not inherit provider secrets or Python injection variables", () => {
  const environment = minimalEnvironment({PATH: "/bin", SystemRoot: "C:/Windows", OPENAI_API_KEY: "never-pass", ANTHROPIC_API_KEY: "never-pass", NODE_OPTIONS: "--require injected", PYTHONPATH: "injected"});
  assert.deepEqual(environment, {PATH: "/bin", SystemRoot: "C:/Windows"});
});

test("Node client executes real Python IPC and separate file worker", {skip: !python}, async () => {
  const client = new SidecarClient({executable: python, args: ["-I", entry]});
  try {
    const health = await client.request(request("health"));
    const worker = await client.request(request("worker_probe"));
    const vault = await client.request(request("vault_probe"));
    assert.equal(health.result.pid, client.child.pid);
    assert.equal(worker.result.parent_pid, client.child.pid);
    assert.notEqual(worker.result.pid, client.child.pid);
    assert.equal(worker.result.temporary_artifact_removed, true);
    assert.equal(health.result.product_integration, "NOT_RUN");
    assert.equal(vault.result.status, process.platform === "win32" ? "PASS" : "NOT_RUN");
  } finally { await client.close(); }
});

test("unexpected child death is reported without leaving a hanging request", {skip: !python}, async () => {
  const client = new SidecarClient({executable: python, args: ["-I", entry]});
  await client.ready;
  client.child.kill();
  await new Promise((resolve) => client.child.once("exit", resolve));
  await assert.rejects(client.request(request("health")), /SIDECAR_UNAVAILABLE/);
});

test("child startup errors are sanitized and bounded", async () => {
  const client = new SidecarClient({executable: path.resolve("missing-aik-spike-executable"), timeoutMs: 1000});
  await assert.rejects(client.request(request("health")), /^Error: SIDECAR_START_FAILED$/);
  await client.close();
});
