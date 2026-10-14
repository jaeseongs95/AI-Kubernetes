"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {execFileSync} = require("node:child_process");
const {EngineClient, EngineSupervisor, EngineError} = require("../electron/engine.cjs");

const FAKE = path.join(__dirname, "fixtures", "fake-engine.cjs");
const ENTRY = path.join(__dirname, "..", "engine", "aik_engine_entry.py");

function fake(mode, options = {}) {
  return new EngineClient({executable: process.execPath, args: [FAKE, mode], env: {}, timeoutMs: 2000, ...options});
}

function python() {
  for (const name of ["python3", "python"]) {
    try {
      const found = execFileSync(name, ["-c", "import sys; print(sys.executable)"], {encoding: "utf8"}).trim();
      if (path.isAbsolute(found)) return found;
    } catch {}
  }
  return null;
}

async function code(promise) {
  try { await promise; } catch (error) { return error instanceof EngineError ? error.code : "OTHER"; }
  return "NONE";
}

test("real Python engine answers the three read commands", async (t) => {
  const executable = python();
  if (!executable) return t.skip("Python is not available");
  const client = new EngineClient({executable, args: ["-I", ENTRY], env: {PATH: process.env.PATH ?? "",
    HOME: process.env.HOME ?? "", XDG_STATE_HOME: path.join(__dirname, "no-such-state"),
    LOCALAPPDATA: path.join(__dirname, "no-such-state"), SYSTEMROOT: process.env.SYSTEMROOT ?? ""}});
  try {
    const status = await client.request("system.status");
    assert.deepEqual(status, {state: "DEGRADED", director_state: "NOT_CONFIGURED"});
    const info = await client.request("engine.info");
    assert.equal(info.protocol_version, 1);
    assert.equal(info.readmodel, "NOT_CONFIGURED");
    assert.equal(info.state_root, "ABSENT");
    assert.deepEqual(await client.request("readmodel.sources"), {readmodel: "NOT_CONFIGURED", authoritative: false, sources: []});
    assert.equal(client.info.engine_version, info.engine_version);
  } finally { await client.close(); }
});

test("a fake engine round trip carries the capability", async () => {
  const client = fake("ok");
  try { assert.deepEqual(await client.request("engine.info"), {echo: "engine.info"}); }
  finally { await client.close(); }
});

test("version mismatch is reported as ENGINE_INCOMPATIBLE", async () => {
  for (const mode of ["incompatible", "newer"]) {
    const client = fake(mode);
    assert.equal(await code(client.request("system.status")), "ENGINE_INCOMPATIBLE", mode);
    await client.close();
  }
});

test("broken engines fail closed with fixed codes", async () => {
  const cases = [["garbage", "ENGINE_INVALID_RESPONSE"], ["wrong-id", "ENGINE_INVALID_RESPONSE"],
    ["exit", "ENGINE_EXITED"], ["crash-after-ready", "ENGINE_EXITED"], ["silent", "ENGINE_START_TIMEOUT"],
    ["hang", "ENGINE_REQUEST_TIMEOUT"]];
  for (const [mode, expected] of cases) {
    const client = fake(mode, {timeoutMs: 500});
    assert.equal(await code(client.request("system.status")), expected, mode);
    assert.equal(client.state, "failed");
    assert.equal(await code(client.request("system.status")), expected, mode + " stays failed");
    await client.close();
  }
});

test("only read commands are sent and a non-absolute engine path is refused", async () => {
  const client = fake("ok");
  assert.equal(await code(client.request("vault.resolve")), "ENGINE_UNSUPPORTED_COMMAND");
  await client.close();
  assert.throws(() => new EngineClient({executable: "python3"}), (error) => error.code === "ENGINE_PATH_REQUIRED");
});

test("supervisor restarts after a failure with a delay and never after a version mismatch", async () => {
  let now = 0, launches = 0, mode = "exit";
  const supervisor = new EngineSupervisor({clock: () => now, restartDelayMs: 5000,
    launch: () => { launches++; return {executable: process.execPath, args: [FAKE, mode], env: {}, timeoutMs: 2000}; }});
  assert.equal(await code(supervisor.request("system.status")), "ENGINE_EXITED");
  now = 1000;
  assert.equal(await code(supervisor.request("system.status")), "ENGINE_EXITED"); // within the delay: no relaunch
  assert.equal(launches, 1);
  now = 6000; mode = "ok";
  assert.deepEqual(await supervisor.request("system.status"), {echo: "system.status"});
  assert.equal(launches, 2);
  await supervisor.close();

  const stuck = new EngineSupervisor({clock: () => now, restartDelayMs: 0,
    launch: () => ({executable: process.execPath, args: [FAKE, "incompatible"], env: {}, timeoutMs: 2000})});
  assert.equal(await code(stuck.request("system.status")), "ENGINE_INCOMPATIBLE");
  now += 60000;
  assert.equal(await code(stuck.request("system.status")), "ENGINE_INCOMPATIBLE");
  await stuck.close();

  const unconfigured = new EngineSupervisor({launch: () => { throw new Error("AIK_DESKTOP_PYTHON_REQUIRED"); }});
  assert.equal(await code(unconfigured.request("system.status")), "ENGINE_NOT_CONFIGURED");
});
