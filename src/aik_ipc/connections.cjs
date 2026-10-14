"use strict";

// AIK-046/047: renderer-facing connection commands of the desktop IPC.
// Payloads are validated field by field; results are copied into frozen DTOs.
// Commands that carry an API key (connections.add_key / rotate_key) are NOT
// renderer commands: only the main-process key entry window sends them to the
// engine. The renderer asks for that window with connections.request_key_entry.
const {types} = require("node:util");

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CODE = /^[A-Z][A-Z0-9_]{1,63}$/;
const TIMESTAMP = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$/;
const MAX_LABEL = 128;
const MAX_CONNECTIONS = 256;
const MAX_REFERENCES = 1024;
const MAX_CONNECTORS = 32;

const CONNECTION_STATES = new Set(["verified", "unverified", "unauthenticated", "locked", "account_changed",
  "unknown", "unsupported"]);
const CHECK_STATES = new Set(["verified", "unauthenticated", "locked", "account_changed", "unknown"]);
const LOGIN_STATES = new Set(["idle", "pending", "signed_in", "failed", "cancelled"]);
const EXECUTION = Object.freeze({api: "api_key", subscription: "host_login"});

// Fixed codes the engine's connection service may answer with (see aik_connect.service).
const CONNECTION_CODES = Object.freeze({
  CONNECTIONS_NOT_CONFIGURED: "Connection management is not configured in this engine.",
  UNSUPPORTED_PROVIDER: "No connector serves this vendor and execution path.",
  INVALID_SECRET: "The API key was not accepted.",
  INVALID_INPUT: "The request values are invalid.",
  INVALID_LABEL: "The account label is invalid.",
  VAULT_UNAVAILABLE: "The per-user secret store is unavailable.",
  VAULT_LOCKED: "The per-user secret store is locked or denied access.",
  VAULT_ERROR: "The per-user secret store failed.",
  DUPLICATE_CONNECTION: "A connection with this label or account already exists.",
  UNKNOWN_CONNECTION: "The connection does not exist.",
  REVISION_CONFLICT: "The connection changed meanwhile; reload and retry.",
  CONNECTION_IN_USE: "The connection is in use; review the impact first.",
  STORE_BUSY: "Connection storage is busy; retry.",
  STORE_UNAVAILABLE: "Connection storage is unavailable.",
  SECRET_CLEANUP_UNCONFIRMED: "Saving failed and removing the new key could not be confirmed.",
  WRONG_METHOD: "This action does not apply to the connection's execution path.",
  LOGIN_IN_PROGRESS: "Another official login is still running.",
  LOGIN_START_FAILED: "The official login could not be started.",
  NO_LOGIN: "No official login was started.",
  LOGIN_PENDING: "The official login has not finished yet.",
  NOT_SIGNED_IN: "The official host is not signed in.",
  ACCOUNT_CHANGED: "The official host is signed in to a different account.",
  LOGOUT_FAILED: "The official host sign-out failed.",
  KEY_ENTRY_BUSY: "A key entry window is already open.",
  KEY_ENTRY_UNAVAILABLE: "The key entry window could not be opened."
});

function plainObject(value) {
  return value !== null && typeof value === "object" && !types.isProxy(value) &&
    Object.getPrototypeOf(value) === Object.prototype;
}

function plainArray(value, max) {
  return Array.isArray(value) && !types.isProxy(value) && Object.getPrototypeOf(value) === Array.prototype &&
    value.length <= max;
}

function own(object, key) {
  const descriptor = Object.getOwnPropertyDescriptor(object, key);
  return descriptor && Object.hasOwn(descriptor, "value") ? descriptor.value : undefined;
}

function exact(value, fields) {
  if (!plainObject(value)) return false;
  const keys = Reflect.ownKeys(value);
  return keys.length === fields.length && keys.every((key) => typeof key === "string" && fields.includes(key) &&
    Object.hasOwn(Object.getOwnPropertyDescriptor(value, key), "value"));
}

function copy(value, fields) {
  return Object.fromEntries(fields.map((key) => [key, own(value, key)]));
}

const isId = (value) => typeof value === "string" && ID.test(value);
const isCode = (value) => typeof value === "string" && CODE.test(value);
const isRevision = (value) => Number.isSafeInteger(value) && value >= 1;
const isCount = (value) => Number.isSafeInteger(value) && value >= 0;
const isTime = (value) => typeof value === "string" && TIMESTAMP.test(value);
const nullable = (check) => (value) => value === null || check(value);

function isLabel(value) {
  if (typeof value !== "string" || value.length < 1 || value.length > MAX_LABEL || value !== value.trim()) return false;
  return ![...value].some((char) => /\p{C}/u.test(char));
}

function isDisplayName(value) {
  return typeof value === "string" && value.length >= 1 && value.length <= 64 && value === value.trim() &&
    ![...value].some((char) => /\p{Cc}/u.test(char));
}

// -- payloads (renderer -> main) ------------------------------------------------

const PAYLOADS = Object.freeze({
  "connections.catalog": {},
  "connections.list": {},
  "connections.login_state": {},
  "connections.cancel_login": {},
  "connections.complete_login": {},
  "connections.impact": {connection_id: isId},
  "connections.check": {connection_id: isId},
  "connections.rename": {connection_id: isId, expected_revision: isRevision, account_label: isLabel},
  "connections.delete": {connection_id: isId, expected_revision: isRevision,
    acknowledge_impact: (value) => typeof value === "boolean"},
  "connections.logout": {connection_id: isId, expected_revision: isRevision},
  "connections.start_login": {provider: nullable(isId), account_label: nullable(isLabel), connection_id: nullable(isId)},
  "connections.request_key_entry": {mode: (value) => value === "add" || value === "rotate", provider: nullable(isId),
    account_label: nullable(isLabel), connection_id: nullable(isId), expected_revision: nullable(isRevision)}
});

const READ_CONNECTION_COMMANDS = Object.freeze(["connections.catalog", "connections.list", "connections.impact",
  "connections.login_state"]);
const WRITE_CONNECTION_COMMANDS = Object.freeze(["connections.check", "connections.rename", "connections.delete",
  "connections.start_login", "connections.cancel_login", "connections.complete_login", "connections.logout",
  "connections.request_key_entry"]);

function combination(command, payload) {
  // Cross-field rules: a new login names a vendor and label, a re-login names a connection.
  if (command === "connections.start_login") {
    return payload.connection_id === null ? payload.provider !== null && payload.account_label !== null
      : payload.provider === null && payload.account_label === null;
  }
  if (command === "connections.request_key_entry") {
    return payload.mode === "add"
      ? payload.provider !== null && payload.account_label !== null && payload.connection_id === null &&
        payload.expected_revision === null
      : payload.provider === null && payload.account_label === null && payload.connection_id !== null &&
        payload.expected_revision !== null;
  }
  return true;
}

/** A frozen copy of a valid payload for ``command``, or null. */
function readPayload(command, value) {
  const schema = PAYLOADS[command];
  if (!schema) return null;
  const fields = Object.keys(schema);
  if (!exact(value, fields)) return null;
  const result = copy(value, fields);
  if (!fields.every((key) => schema[key](result[key])) || !combination(command, result)) return null;
  return Object.freeze(result);
}

// -- results (engine -> renderer) -----------------------------------------------

function checkSnapshot(value) {
  if (!exact(value, ["state", "code"])) return null;
  const result = copy(value, ["state", "code"]);
  return CHECK_STATES.has(result.state) && nullable(isCode)(result.code) ? Object.freeze(result) : null;
}

function usageSnapshot(value) {
  const fields = ["references", "blocking", "external_checked"];
  if (!exact(value, fields)) return null;
  const result = copy(value, fields);
  const known = result.references !== null;
  if (known ? !(isCount(result.references) && isCount(result.blocking) && result.blocking <= result.references)
    : result.blocking !== null) return null;
  return typeof result.external_checked === "boolean" ? Object.freeze(result) : null;
}

function lastCheckSnapshot(value) {
  if (value === null) return null;
  const fields = ["state", "code", "checked_at"];
  if (!exact(value, fields)) return undefined;
  const result = copy(value, fields);
  return CHECK_STATES.has(result.state) && nullable(isCode)(result.code) && isTime(result.checked_at)
    ? Object.freeze(result) : undefined;
}

const CONNECTION_FIELDS = ["connection_id", "account_label", "provider", "display_name", "execution_kind", "method",
  "state", "verified_at", "revision", "binding_revision", "last_check", "usage"];

function connectionSnapshot(value) {
  if (!exact(value, CONNECTION_FIELDS)) return null;
  const result = copy(value, CONNECTION_FIELDS);
  if (!isId(result.connection_id) || !isLabel(result.account_label) || !isId(result.provider) ||
      !isDisplayName(result.display_name) || !Object.hasOwn(EXECUTION, result.execution_kind) ||
      EXECUTION[result.execution_kind] !== result.method || !CONNECTION_STATES.has(result.state) ||
      !nullable(isTime)(result.verified_at) || !isRevision(result.revision) || !isRevision(result.binding_revision)) {
    return null;
  }
  const lastCheck = lastCheckSnapshot(result.last_check);
  const usage = usageSnapshot(result.usage);
  if (lastCheck === undefined || usage === null) return null;
  return Object.freeze({...result, last_check: lastCheck, usage});
}

function connectionWithCheck(value) {
  if (!exact(value, ["connection", "check"])) return null;
  const connection = connectionSnapshot(own(value, "connection"));
  const check = checkSnapshot(own(value, "check"));
  return connection && check ? Object.freeze({connection, check}) : null;
}

function connectionOnly(value) {
  if (!exact(value, ["connection"])) return null;
  const connection = connectionSnapshot(own(value, "connection"));
  return connection ? Object.freeze({connection}) : null;
}

function listSnapshot(value) {
  if (!exact(value, ["store", "connections"])) return null;
  const store = own(value, "store"), list = own(value, "connections");
  if (!["ABSENT", "READY"].includes(store) || !plainArray(list, MAX_CONNECTIONS) ||
      (store === "ABSENT" && list.length !== 0)) return null;
  const connections = [], seen = new Set();
  for (let index = 0; index < list.length; index++) {
    const item = connectionSnapshot(own(list, String(index)));
    if (!item || seen.has(item.connection_id)) return null;
    seen.add(item.connection_id);
    connections.push(item);
  }
  return Object.freeze({store, connections: Object.freeze(connections)});
}

function catalogSnapshot(value) {
  if (!exact(value, ["connectors"])) return null;
  const list = own(value, "connectors");
  if (!plainArray(list, MAX_CONNECTORS)) return null;
  const connectors = [], seen = new Set();
  const fields = ["provider", "execution_kind", "method", "display_name"];
  for (let index = 0; index < list.length; index++) {
    const item = own(list, String(index));
    if (!exact(item, fields)) return null;
    const entry = copy(item, fields);
    const key = entry.provider + "\u0000" + entry.execution_kind;
    if (!isId(entry.provider) || !Object.hasOwn(EXECUTION, entry.execution_kind) ||
        EXECUTION[entry.execution_kind] !== entry.method || !isDisplayName(entry.display_name) || seen.has(key)) {
      return null;
    }
    seen.add(key);
    connectors.push(Object.freeze(entry));
  }
  return Object.freeze({connectors: Object.freeze(connectors)});
}

function impactSnapshot(value) {
  if (!exact(value, ["connection_id", "references", "external_checked"])) return null;
  const connectionId = own(value, "connection_id"), list = own(value, "references");
  const externalChecked = own(value, "external_checked");
  if (!isId(connectionId) || !plainArray(list, MAX_REFERENCES) || typeof externalChecked !== "boolean") return null;
  const references = [];
  for (let index = 0; index < list.length; index++) {
    const item = own(list, String(index));
    if (!exact(item, ["kind", "id", "removable"])) return null;
    const entry = copy(item, ["kind", "id", "removable"]);
    if (!isId(entry.kind) || !isId(entry.id) || typeof entry.removable !== "boolean") return null;
    references.push(Object.freeze(entry));
  }
  return Object.freeze({connection_id: connectionId, references: Object.freeze(references),
    external_checked: externalChecked});
}

function loginSnapshot(value) {
  const fields = ["state", "provider", "cancel_supported", "code", "connection_id"];
  if (!exact(value, fields)) return null;
  const result = copy(value, fields);
  if (!LOGIN_STATES.has(result.state) || !nullable(isId)(result.provider) ||
      typeof result.cancel_supported !== "boolean" || !nullable(isCode)(result.code) ||
      !nullable(isId)(result.connection_id) || (result.state === "idle" && result.provider !== null)) return null;
  return Object.freeze(result);
}

function cancelSnapshot(value) {
  if (!exact(value, ["cancelled", "login"])) return null;
  const cancelled = own(value, "cancelled"), login = loginSnapshot(own(value, "login"));
  return typeof cancelled === "boolean" && login ? Object.freeze({cancelled, login}) : null;
}

function deleteSnapshot(value) {
  const fields = ["connection_id", "removed_references", "secret_deleted"];
  if (!exact(value, fields)) return null;
  const result = copy(value, fields);
  return isId(result.connection_id) && isCount(result.removed_references) &&
    (result.secret_deleted === null || typeof result.secret_deleted === "boolean") ? Object.freeze(result) : null;
}

function keyEntrySnapshot(value) {
  if (!exact(value, ["outcome", "connection", "check"])) return null;
  const outcome = own(value, "outcome");
  if (outcome === "cancelled") {
    return own(value, "connection") === null && own(value, "check") === null
      ? Object.freeze({outcome, connection: null, check: null}) : null;
  }
  if (outcome !== "saved") return null;
  const connection = connectionSnapshot(own(value, "connection"));
  const check = checkSnapshot(own(value, "check"));
  return connection && check ? Object.freeze({outcome, connection, check}) : null;
}

const CONNECTION_SNAPSHOTS = Object.freeze({
  "connections.catalog": catalogSnapshot,
  "connections.list": listSnapshot,
  "connections.impact": impactSnapshot,
  "connections.login_state": loginSnapshot,
  "connections.check": connectionWithCheck,
  "connections.complete_login": connectionWithCheck,
  "connections.rename": connectionOnly,
  "connections.logout": connectionOnly,
  "connections.delete": deleteSnapshot,
  "connections.start_login": loginSnapshot,
  "connections.cancel_login": cancelSnapshot,
  "connections.request_key_entry": keyEntrySnapshot
});

module.exports = {CONNECTION_CODES, PAYLOADS, READ_CONNECTION_COMMANDS, WRITE_CONNECTION_COMMANDS,
  CONNECTION_SNAPSHOTS, readPayload, connectionWithCheck, isLabel, isId, isRevision};
