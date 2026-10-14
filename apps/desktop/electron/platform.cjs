"use strict";

// AIK-045 / PRN-OS: the only place in the desktop shell that looks at the
// operating system. Every other module asks this one; a new OS is supported by
// adding a row here, not by branching elsewhere (OCP).
const path = require("node:path");

const PLATFORMS = Object.freeze({
  win32: Object.freeze({
    family: "windows",
    engineName: "aik-engine.exe",
    caseInsensitiveEnv: true,
    // Engine environment: only what Python and the vault/paths ports need.
    env: Object.freeze(["SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE", "APPDATA", "PATH", "LANG", "LC_ALL"]),
    spawn: Object.freeze({windowsHide: true})
  }),
  linux: Object.freeze({
    family: "linux",
    engineName: "aik-engine",
    caseInsensitiveEnv: false,
    // DBUS/XDG_SESSION_ID reach the Secret Service and logind adapters.
    env: Object.freeze(["HOME", "TMPDIR", "PATH", "LANG", "LC_ALL", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
      "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_ID"]),
    spawn: Object.freeze({})
  })
});

function describe(platform = process.platform) {
  return Object.hasOwn(PLATFORMS, platform) ? PLATFORMS[platform] : null;
}

function requireSupported(platform = process.platform) {
  const selected = describe(platform);
  if (!selected) throw new Error("PLATFORM_UNSUPPORTED");
  return selected;
}

function family(platform = process.platform) {
  return describe(platform)?.family ?? null;
}

function packagedEngine(resourcesPath, platform = process.platform) {
  if (typeof resourcesPath !== "string" || !path.isAbsolute(resourcesPath)) throw new Error("RESOURCES_PATH_REQUIRED");
  return {executable: path.join(resourcesPath, "engine", requireSupported(platform).engineName), args: []};
}

function sourceEngine(python, entry) {
  // Developer configuration: an absolute interpreter, isolated mode, a fixed entry script.
  if (typeof python !== "string" || !path.isAbsolute(python)) throw new Error("AIK_DESKTOP_PYTHON_REQUIRED");
  if (typeof entry !== "string" || !path.isAbsolute(entry)) throw new Error("ENGINE_ENTRY_REQUIRED");
  return {executable: python, args: ["-I", entry]};
}

function engineEnvironment(source = process.env, platform = process.platform) {
  const selected = requireSupported(platform);
  const allowed = new Set(selected.env);
  return Object.fromEntries(Object.entries(source).filter(([key, value]) => typeof value === "string" &&
    allowed.has(selected.caseInsensitiveEnv ? key.toUpperCase() : key)));
}

function spawnOptions(platform = process.platform) {
  return {...requireSupported(platform).spawn};
}

module.exports = {describe, family, packagedEngine, sourceEngine, engineEnvironment, spawnOptions};
