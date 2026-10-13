"use strict";

const {contextBridge, ipcRenderer} = require("electron");

function request(command) {
  return ipcRenderer.invoke("aik:foundation-probe", {
    protocol_version: 1,
    request_id: globalThis.crypto.randomUUID(),
    workspace_id: "foundation-spike",
    command,
    payload: {}
  });
}

contextBridge.exposeInMainWorld("aik", Object.freeze({
  health: () => request("health"),
  runWorkerProbe: () => request("worker_probe"),
  runVaultProbe: () => request("vault_probe"),
  runAuthVaultProbe: () => request("auth_vault_probe")
}));
