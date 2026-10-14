"use strict";

// AIK-087: renderer-facing project folder (workspace) commands of the desktop IPC.
// The renderer never sends a folder path. It asks main for the native folder
// dialog with workspaces.request_folder; main sends the chosen path to the engine
// (workspaces.open / create / rebind_root, which are NOT renderer commands).
// Payloads are validated field by field; results are copied into frozen DTOs.
const {exact, copy, own, plainArray, nullable, isId, isCode, isLabel, isRevision} = require("./connections.cjs");

const MAX_WORKSPACES = 256;
const MAX_CONNECTIONS = 256;
const MAX_ROOT = 4096;
const MAX_FOLDER_NAME = 255;
const STATUSES = new Set(["ready", "archived", "root_missing", "root_replaced", "root_unsafe", "access_denied",
  "unsupported_host", "vm_project_missing", "vm_project_mismatch", "vm_project_unknown"]);
const ACTIONS = new Set(["none", "restore_workspace", "reselect_folder", "confirm_rebind_root",
  "restore_access_or_reselect", "not_executable", "rebind_vm_project", "retry_vm_query"]);
const FOLDER_FORBIDDEN = /[<>:"/\\|?*\u0000-\u001f]/u;

// Fixed codes the engine's workspace service and main's folder picker may answer with.
const WORKSPACE_CODES = Object.freeze({
  WORKSPACES_NOT_CONFIGURED: "Project folder management is not configured in this engine.",
  INVALID_NAME: "The project name is invalid.",
  INVALID_FOLDER_NAME: "The new folder name is not portable.",
  ROOT_INVALID: "This folder cannot be a project folder.",
  ROOT_UNAVAILABLE: "The folder is missing or cannot be inspected.",
  ROOT_CONFLICT: "The folder duplicates or nests another project folder.",
  UNKNOWN_WORKSPACE: "The project folder is not registered.",
  VM_NOT_CONFIGURED: "No VM is connected, so a project folder cannot be registered yet.",
  VM_PROJECT_UNAVAILABLE: "The VM project could not be created or confirmed.",
  BINDING_MISMATCH: "The stored selection differs from the verified project binding.",
  WORKSPACE_ERROR: "The project folder operation failed.",
  WORKSPACE_ARCHIVED: "The project folder is archived.",
  WORKSPACE_ROOT_MISSING: "The project folder was moved or deleted.",
  WORKSPACE_ROOT_REPLACED: "The project folder was replaced by a different folder.",
  WORKSPACE_ROOT_UNSAFE: "The project folder path is no longer safe to use.",
  WORKSPACE_ACCESS_DENIED: "The project folder cannot be accessed.",
  WORKSPACE_UNSUPPORTED_HOST: "This location cannot be used as a working folder on this computer.",
  WORKSPACE_VM_PROJECT_MISSING: "The VM project of this folder no longer exists.",
  WORKSPACE_VM_PROJECT_MISMATCH: "The VM project points at a different folder.",
  WORKSPACE_VM_PROJECT_UNKNOWN: "The VM project state could not be confirmed.",
  FOLDER_PICKER_BUSY: "A folder dialog is already open.",
  FOLDER_PICKER_UNAVAILABLE: "The folder dialog could not be opened."
});

function isFolderName(value) {
  return typeof value === "string" && value.length >= 1 && value.length <= MAX_FOLDER_NAME &&
    value !== "." && value !== ".." && !/[ .]$/u.test(value) && !FOLDER_FORBIDDEN.test(value);
}

const isRoot = (value) => typeof value === "string" && value.length >= 1 && value.length <= MAX_ROOT &&
  !/[\u0000-\u001f]/u.test(value);

// -- payloads (renderer -> main) ------------------------------------------------

const PAYLOADS = Object.freeze({
  "workspaces.list": {},
  "workspaces.scope": {workspace_id: isId},
  "workspaces.select": {workspace_id: isId},
  "workspaces.archive": {workspace_id: isId, expected_revision: isRevision},
  "workspaces.restore": {workspace_id: isId, expected_revision: isRevision},
  "workspaces.request_folder": {mode: (value) => ["open", "create", "rebind"].includes(value), name: nullable(isLabel),
    folder_name: nullable(isFolderName), workspace_id: nullable(isId), expected_revision: nullable(isRevision)}
});

const READ_WORKSPACE_COMMANDS = Object.freeze(["workspaces.list", "workspaces.scope"]);
const WRITE_WORKSPACE_COMMANDS = Object.freeze(["workspaces.select", "workspaces.archive", "workspaces.restore",
  "workspaces.request_folder"]);

function combination(command, payload) {
  if (command !== "workspaces.request_folder") return true;
  // open names the project; create also names the new folder; rebind names the registered workspace.
  if (payload.mode === "open") {
    return payload.name !== null && payload.folder_name === null && payload.workspace_id === null &&
      payload.expected_revision === null;
  }
  if (payload.mode === "create") {
    return payload.name !== null && payload.folder_name !== null && payload.workspace_id === null &&
      payload.expected_revision === null;
  }
  return payload.name === null && payload.folder_name === null && payload.workspace_id !== null &&
    payload.expected_revision !== null;
}

/** A frozen copy of a valid payload for ``command``, or null. */
function readWorkspacePayload(command, value) {
  const schema = PAYLOADS[command];
  if (!schema) return null;
  const fields = Object.keys(schema);
  if (!exact(value, fields)) return null;
  const result = copy(value, fields);
  if (!fields.every((key) => schema[key](result[key])) || !combination(command, result)) return null;
  return Object.freeze(result);
}

// -- results (engine -> renderer) -----------------------------------------------

const WORKSPACE_FIELDS = ["workspace_id", "name", "root", "state", "status", "required_action", "revision", "active"];

function workspaceSnapshot(value) {
  if (!exact(value, WORKSPACE_FIELDS)) return null;
  const result = copy(value, WORKSPACE_FIELDS);
  if (!isId(result.workspace_id) || !isLabel(result.name) || !isRoot(result.root) ||
      !["active", "archived"].includes(result.state) || !STATUSES.has(result.status) ||
      !ACTIONS.has(result.required_action) || !isRevision(result.revision) || typeof result.active !== "boolean" ||
      (result.state === "archived") !== (result.status === "archived")) return null;
  return Object.freeze(result);
}

const ACTIVE_FIELDS = ["workspace_id", "registered", "status", "required_action", "cwd", "selection_revision"];

function activeSnapshot(value) {
  if (!exact(value, ACTIVE_FIELDS)) return null;
  const result = copy(value, ACTIVE_FIELDS);
  if (!isId(result.workspace_id) || typeof result.registered !== "boolean" ||
      !(result.status === null || STATUSES.has(result.status)) || !ACTIONS.has(result.required_action) ||
      !nullable(isRoot)(result.cwd) || !isRevision(result.selection_revision)) return null;
  // Only a ready, registered selection has an execution folder.
  if ((result.cwd !== null) !== (result.registered && result.status === "ready")) return null;
  if (!result.registered && result.status !== null) return null;
  return Object.freeze(result);
}

function listSnapshot(value) {
  if (!exact(value, ["vm", "registry", "active", "workspaces"])) return null;
  const vm = own(value, "vm"), registry = own(value, "registry"), list = own(value, "workspaces");
  if (!["CONFIGURED", "NOT_CONFIGURED"].includes(vm) || !["ABSENT", "READY"].includes(registry) ||
      !plainArray(list, MAX_WORKSPACES) || (registry === "ABSENT" && list.length !== 0)) return null;
  const rawActive = own(value, "active");
  const active = rawActive === null ? null : activeSnapshot(rawActive);
  if (rawActive !== null && active === null) return null;
  const workspaces = [], seen = new Set();
  for (let index = 0; index < list.length; index++) {
    const item = workspaceSnapshot(own(list, String(index)));
    if (!item || seen.has(item.workspace_id) || item.active !== (active?.workspace_id === item.workspace_id)) {
      return null;
    }
    seen.add(item.workspace_id);
    workspaces.push(item);
  }
  return Object.freeze({vm, registry, active, workspaces: Object.freeze(workspaces)});
}

function scopeSnapshot(value) {
  if (!exact(value, ["workspace_id", "scope", "connections"])) return null;
  const workspaceId = own(value, "workspace_id"), scope = own(value, "scope"), list = own(value, "connections");
  if (!isId(workspaceId) || !["ABSENT", "READY"].includes(scope) || !plainArray(list, MAX_CONNECTIONS)) return null;
  const fields = ["connection_id", "provider", "owner_workspace_id", "allowed", "reason"];
  const connections = [], seen = new Set();
  for (let index = 0; index < list.length; index++) {
    const item = own(list, String(index));
    if (!exact(item, fields)) return null;
    const entry = copy(item, fields);
    if (!isId(entry.connection_id) || !isId(entry.provider) || !isId(entry.owner_workspace_id) ||
        typeof entry.allowed !== "boolean" || entry.allowed !== (entry.reason === null) ||
        !nullable(isCode)(entry.reason) || seen.has(entry.connection_id) ||
        (scope === "ABSENT" && entry.reason !== "NO_SCOPE")) return null;
    seen.add(entry.connection_id);
    connections.push(Object.freeze(entry));
  }
  return Object.freeze({workspace_id: workspaceId, scope, connections: Object.freeze(connections)});
}

function folderSnapshot(value) {
  if (!exact(value, ["outcome", "workspace"])) return null;
  const outcome = own(value, "outcome");
  if (outcome === "cancelled") {
    return own(value, "workspace") === null ? Object.freeze({outcome, workspace: null}) : null;
  }
  if (outcome !== "saved") return null;
  const workspace = workspaceSnapshot(own(value, "workspace"));
  return workspace ? Object.freeze({outcome, workspace}) : null;
}

const WORKSPACE_SNAPSHOTS = Object.freeze({
  "workspaces.list": listSnapshot,
  "workspaces.scope": scopeSnapshot,
  "workspaces.select": activeSnapshot,
  "workspaces.archive": workspaceSnapshot,
  "workspaces.restore": workspaceSnapshot,
  "workspaces.request_folder": folderSnapshot
});

module.exports = {WORKSPACE_CODES, PAYLOADS, READ_WORKSPACE_COMMANDS, WRITE_WORKSPACE_COMMANDS,
  WORKSPACE_SNAPSHOTS, readWorkspacePayload, workspaceSnapshot, isFolderName};
