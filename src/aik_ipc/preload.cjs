"use strict";
// Sandboxed Electron preload: use Web Crypto from the secure app origin;
// do not import Node builtins or provide a generic renderer invoke method.
// AIK-045 adds the read-only engine.info and readmodel.sources queries.
// AIK-046/047 add connection commands with fixed payload shapes; none carries a
// secret (API keys are typed only into the main-owned key entry window).
// AIK-087 adds project folder commands; none carries a path (folders are picked
// only in the main-owned native folder dialog).
const {contextBridge, ipcRenderer} = require("electron");
const CHANNEL = "aik:desktop:v1";
function send(command, workspaceId, payload = {}) {
  return ipcRenderer.invoke(CHANNEL, {
    protocol_version: 1,
    request_id: globalThis.crypto.randomUUID(),
    workspace_id: workspaceId,
    command,
    payload
  });
}
const text = (value) => (value === null || value === undefined ? null : String(value));
const revision = (value) => (value === null || value === undefined ? null : Number(value));
contextBridge.exposeInMainWorld("aikDesktop", Object.freeze({
  handshake: () => send("protocol.handshake", "unbound"),
  status: (workspaceId) => send("system.status", workspaceId),
  engineInfo: (workspaceId) => send("engine.info", workspaceId),
  readmodelSources: (workspaceId) => send("readmodel.sources", workspaceId),
  connectionCatalog: (workspaceId) => send("connections.catalog", workspaceId),
  connections: (workspaceId) => send("connections.list", workspaceId),
  connectionImpact: (workspaceId, connectionId) =>
    send("connections.impact", workspaceId, {connection_id: text(connectionId)}),
  loginState: (workspaceId) => send("connections.login_state", workspaceId),
  checkConnection: (workspaceId, connectionId) =>
    send("connections.check", workspaceId, {connection_id: text(connectionId)}),
  renameConnection: (workspaceId, connectionId, expectedRevision, accountLabel) =>
    send("connections.rename", workspaceId, {connection_id: text(connectionId),
      expected_revision: revision(expectedRevision), account_label: text(accountLabel)}),
  deleteConnection: (workspaceId, connectionId, expectedRevision, acknowledgeImpact) =>
    send("connections.delete", workspaceId, {connection_id: text(connectionId),
      expected_revision: revision(expectedRevision), acknowledge_impact: acknowledgeImpact === true}),
  logoutConnection: (workspaceId, connectionId, expectedRevision) =>
    send("connections.logout", workspaceId, {connection_id: text(connectionId),
      expected_revision: revision(expectedRevision)}),
  startLogin: (workspaceId, provider, accountLabel, connectionId) =>
    send("connections.start_login", workspaceId, {provider: text(provider), account_label: text(accountLabel),
      connection_id: text(connectionId)}),
  cancelLogin: (workspaceId) => send("connections.cancel_login", workspaceId),
  completeLogin: (workspaceId) => send("connections.complete_login", workspaceId),
  requestKeyEntry: (workspaceId, mode, provider, accountLabel, connectionId, expectedRevision) =>
    send("connections.request_key_entry", workspaceId, {mode: text(mode), provider: text(provider),
      account_label: text(accountLabel), connection_id: text(connectionId),
      expected_revision: revision(expectedRevision)}),
  workspaces: (workspaceId) => send("workspaces.list", workspaceId),
  workspaceScope: (workspaceId, targetId) => send("workspaces.scope", workspaceId, {workspace_id: text(targetId)}),
  selectWorkspace: (workspaceId, targetId) => send("workspaces.select", workspaceId, {workspace_id: text(targetId)}),
  archiveWorkspace: (workspaceId, targetId, expectedRevision) =>
    send("workspaces.archive", workspaceId, {workspace_id: text(targetId), expected_revision: revision(expectedRevision)}),
  restoreWorkspace: (workspaceId, targetId, expectedRevision) =>
    send("workspaces.restore", workspaceId, {workspace_id: text(targetId), expected_revision: revision(expectedRevision)}),
  requestFolder: (workspaceId, mode, name, folderName, targetId, expectedRevision) =>
    send("workspaces.request_folder", workspaceId, {mode: text(mode), name: text(name), folder_name: text(folderName),
      workspace_id: text(targetId), expected_revision: revision(expectedRevision)})
}));
