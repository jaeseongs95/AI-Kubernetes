"use strict";
// Sandboxed Electron preload: use Web Crypto from the secure app origin;
// do not import Node builtins or provide a generic renderer invoke method.
const {contextBridge, ipcRenderer} = require("electron");
const CHANNEL = "aik:desktop:v1";
function send(command, workspaceId) {
  return ipcRenderer.invoke(CHANNEL, {
    protocol_version: 1,
    request_id: globalThis.crypto.randomUUID(),
    workspace_id: workspaceId,
    command,
    payload: {}
  });
}
contextBridge.exposeInMainWorld("aikDesktop", Object.freeze({
  handshake: () => send("protocol.handshake", "unbound"),
  status: (workspaceId) => send("system.status", workspaceId)
}));
