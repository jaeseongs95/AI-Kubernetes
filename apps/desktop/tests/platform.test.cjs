"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs");
const platform = require("../electron/platform.cjs");

test("supported platforms map to families and engine names", () => {
  assert.equal(platform.family("win32"), "windows");
  assert.equal(platform.family("linux"), "linux");
  assert.equal(platform.family("aix"), null);
  const root = path.resolve("/resources");
  assert.equal(path.basename(platform.packagedEngine(root, "win32").executable), "aik-engine.exe");
  assert.equal(path.basename(platform.packagedEngine(root, "linux").executable), "aik-engine");
  assert.throws(() => platform.packagedEngine(root, "aix"), /PLATFORM_UNSUPPORTED/);
  assert.throws(() => platform.packagedEngine("relative", "linux"), /RESOURCES_PATH_REQUIRED/);
});

test("source engine needs an absolute interpreter and runs isolated", () => {
  const python = path.resolve("/usr/bin/python3"), entry = path.resolve("/app/engine/entry.py");
  assert.deepEqual(platform.sourceEngine(python, entry), {executable: python, args: ["-I", entry]});
  assert.throws(() => platform.sourceEngine(undefined, entry), /AIK_DESKTOP_PYTHON_REQUIRED/);
  assert.throws(() => platform.sourceEngine("python3", entry), /AIK_DESKTOP_PYTHON_REQUIRED/);
});

test("engine environment is an allow list per platform", () => {
  const source = {Path: "C:\\bin", LOCALAPPDATA: "C:\\L", ANTHROPIC_API_KEY: "synthetic", HOME: "/home/u",
    DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/bus", AIK_DESKTOP_PYTHON: "/usr/bin/python3"};
  assert.deepEqual(platform.engineEnvironment(source, "win32"), {Path: "C:\\bin", LOCALAPPDATA: "C:\\L"});
  assert.deepEqual(platform.engineEnvironment(source, "linux"), {HOME: "/home/u", DBUS_SESSION_BUS_ADDRESS: "unix:path=/run/bus"});
  assert.deepEqual(platform.spawnOptions("win32"), {windowsHide: true});
});

test("only platform.cjs inspects the operating system", () => {
  const directory = path.join(__dirname, "..", "electron");
  for (const name of fs.readdirSync(directory)) {
    if (name === "platform.cjs") continue;
    const source = fs.readFileSync(path.join(directory, name), "utf8");
    assert.doesNotMatch(source, /process\.platform|os\.platform\(|require\("node:os"\)/, name);
  }
});
