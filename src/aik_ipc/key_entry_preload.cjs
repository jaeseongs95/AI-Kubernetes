"use strict";
// AIK-046/047: sandboxed preload of the main-owned API key entry window only.
// It exposes three calls on a dedicated channel and nothing else; the main
// app window never loads this preload, and this window never gets aikDesktop.
const {contextBridge, ipcRenderer} = require("electron");
const CHANNEL = "aik:key-entry:v1";
contextBridge.exposeInMainWorld("aikKeyEntry", Object.freeze({
  context: () => ipcRenderer.invoke(CHANNEL, {op: "context"}),
  submit: (secret) => ipcRenderer.invoke(CHANNEL, {op: "submit", secret: String(secret)}),
  cancel: () => ipcRenderer.invoke(CHANNEL, {op: "cancel"})
}));
