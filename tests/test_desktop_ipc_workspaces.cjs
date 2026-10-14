"use strict";
// AIK-087: project folder commands behind the AIK-004 router. Synthetic DTOs and paths only.
const test = require("node:test");
const assert = require("node:assert/strict");
const {randomUUID} = require("node:crypto");
const {VERSION, READ_COMMANDS, WRITE_COMMANDS, BackendError, DesktopRouter, makeRendererHandler} =
  require("../src/aik_ipc/desktop.cjs");
const {PAYLOADS, WORKSPACE_CODES, readWorkspacePayload} = require("../src/aik_ipc/workspaces.cjs");

const ROOT = "C:\\Users\\사용자\\프로젝트 A";
const WORKSPACE = Object.freeze({workspace_id: "ws-0123456789abcdef", name: "프로젝트 A", root: ROOT,
  state: "active", status: "ready", required_action: "none", revision: 1, active: true});
const ACTIVE = Object.freeze({workspace_id: "ws-0123456789abcdef", registered: true, status: "ready",
  required_action: "none", cwd: ROOT, selection_revision: 2});
const LIST = Object.freeze({vm: "CONFIGURED", registry: "READY", active: ACTIVE, workspaces: [WORKSPACE]});

function fixture(readers) {
  const frame = {url: "aik://app/index.html", isDestroyed: () => false};
  const webContents = {mainFrame: frame, isDestroyed: () => false};
  const router = new DesktopRouter({readers});
  const handler = makeRendererHandler({router, getWindow: () => ({webContents}),
    getWorkspaceId: () => "local", appUrl: "aik://app/index.html"});
  const event = {sender: webContents, senderFrame: frame};
  const ask = (command, payload = {}) => handler(event, {protocol_version: VERSION, request_id: randomUUID(),
    workspace_id: "local", command, payload});
  return {ask};
}

const VALID = {
  "workspaces.list": {},
  "workspaces.scope": {workspace_id: "ws-1"},
  "workspaces.select": {workspace_id: "ws-1"},
  "workspaces.archive": {workspace_id: "ws-1", expected_revision: 2},
  "workspaces.restore": {workspace_id: "ws-1", expected_revision: 3},
  "workspaces.request_folder": {mode: "create", name: "새 프로젝트", folder_name: "새 폴더", workspace_id: null,
    expected_revision: null}
};

test("renderer workspace commands never carry a folder path", () => {
  const all = [...READ_COMMANDS, ...WRITE_COMMANDS].filter((command) => command.startsWith("workspaces."));
  assert.deepEqual(all.sort(), Object.keys(PAYLOADS).sort());
  assert.equal(all.some((command) => /\.(open|create|rebind_root)$/.test(command)), false);
  for (const [command, schema] of Object.entries(PAYLOADS)) {
    assert.equal(Object.keys(schema).some((field) => /root|path|parent|cwd/.test(field)), false, command);
  }
});

test("payloads are exact, typed and follow the per-mode combination", () => {
  for (const [command, payload] of Object.entries(VALID)) {
    const copy = readWorkspacePayload(command, payload);
    assert.ok(copy, command);
    assert.equal(Object.isFrozen(copy), true);
    assert.deepEqual(copy, payload);
  }
  const folder = (mode, name, folderName, id, revision) =>
    ({mode, name, folder_name: folderName, workspace_id: id, expected_revision: revision});
  assert.ok(readWorkspacePayload("workspaces.request_folder", folder("open", "기존", null, null, null)));
  assert.ok(readWorkspacePayload("workspaces.request_folder", folder("rebind", null, null, "ws-1", 4)));
  const bad = [
    ["workspaces.request_folder", folder("open", "기존", "폴더", null, null)],
    ["workspaces.request_folder", folder("create", "새", null, null, null)],
    ["workspaces.request_folder", folder("rebind", null, null, "ws-1", null)],
    ["workspaces.request_folder", folder("rebind", "이름", null, "ws-1", 1)],
    ["workspaces.request_folder", folder("delete", null, null, "ws-1", 1)],
    ["workspaces.request_folder", folder("create", "새", "a/b", null, null)],
    ["workspaces.request_folder", folder("create", "새", "..", null, null)],
    ["workspaces.request_folder", folder("create", "새", "끝 점.", null, null)],
    ["workspaces.request_folder", folder("open", " 앞 공백", null, null, null)],
    ["workspaces.request_folder", {...folder("open", "기존", null, null, null), root: ROOT}],
    ["workspaces.select", {workspace_id: "../ws"}],
    ["workspaces.archive", {workspace_id: "ws-1", expected_revision: 0}],
    ["workspaces.select", new Proxy({workspace_id: "ws-1"}, {})]
  ];
  for (const [command, payload] of bad) assert.equal(readWorkspacePayload(command, payload), null, JSON.stringify(payload));
});

test("results are copied into frozen DTOs and inconsistent results are rejected", async () => {
  const backend = {...LIST, workspaces: [{...WORKSPACE}]};
  const {ask} = fixture({"workspaces.list": async () => backend});
  const answer = await ask("workspaces.list");
  assert.deepEqual(answer.result, LIST);
  assert.ok(Object.isFrozen(answer.result.workspaces[0]) && Object.isFrozen(answer.result.active));
  backend.workspaces[0].name = "바뀜";
  assert.equal(answer.result.workspaces[0].name, "프로젝트 A");
  const bad = [
    {...LIST, active: {...ACTIVE, cwd: null}},                       // ready selection without its folder
    {...LIST, active: {...ACTIVE, status: "root_missing"}},          // folder reported for a missing root
    {...LIST, workspaces: [{...WORKSPACE, active: false}]},          // active flag disagrees with the selection
    {...LIST, workspaces: [{...WORKSPACE, state: "archived"}]},      // archived but status says ready
    {...LIST, registry: "ABSENT"},                                   // absent registry with entries
    {...LIST, workspaces: [{...WORKSPACE, status: "gone"}]},
    {...LIST, workspaces: [{...WORKSPACE, extra: 1}]}
  ];
  for (const value of bad) {
    const {ask: badAsk} = fixture({"workspaces.list": async () => value});
    assert.equal((await badAsk("workspaces.list")).error.code, "INVALID_RESPONSE", JSON.stringify(value));
  }
});

test("scope rows: allowed rows have no reason and an absent scope denies everything", async () => {
  const scope = {workspace_id: "ws-1", scope: "READY", connections: [
    {connection_id: "conn-a", provider: "vendor-a", owner_workspace_id: "local", allowed: true, reason: null},
    {connection_id: "conn-b", provider: "vendor-b", owner_workspace_id: "local", allowed: false, reason: "NOT_SHARED"}]};
  const {ask} = fixture({"workspaces.scope": async () => scope});
  assert.deepEqual((await ask("workspaces.scope", {workspace_id: "ws-1"})).result, scope);
  for (const value of [
    {...scope, connections: [{...scope.connections[0], reason: "NO_SCOPE"}]},
    {...scope, scope: "ABSENT"}]) {
    const {ask: badAsk} = fixture({"workspaces.scope": async () => value});
    assert.equal((await badAsk("workspaces.scope", {workspace_id: "ws-1"})).error.code, "INVALID_RESPONSE");
  }
});

test("folder dialog outcomes and fixed workspace codes reach the renderer", async () => {
  const {ask} = fixture({
    "workspaces.request_folder": async (_workspace, context) => context.payload.mode === "open"
      ? {outcome: "cancelled", workspace: null} : {outcome: "saved", workspace: {...WORKSPACE, active: false}},
    "workspaces.select": async () => { throw new BackendError("WORKSPACE_ROOT_MISSING"); },
    "workspaces.archive": async () => { throw new BackendError("VM_NOT_CONFIGURED"); }
  });
  const open = {mode: "open", name: "기존", folder_name: null, workspace_id: null, expected_revision: null};
  assert.deepEqual((await ask("workspaces.request_folder", open)).result, {outcome: "cancelled", workspace: null});
  const saved = await ask("workspaces.request_folder", VALID["workspaces.request_folder"]);
  assert.equal(saved.result.workspace.root, ROOT);
  const missing = await ask("workspaces.select", {workspace_id: "ws-1"});
  assert.deepEqual([missing.error.code, missing.error.message],
    ["WORKSPACE_ROOT_MISSING", WORKSPACE_CODES.WORKSPACE_ROOT_MISSING]);
  assert.equal((await ask("workspaces.archive", {workspace_id: "ws-1", expected_revision: 1})).error.code,
    "VM_NOT_CONFIGURED");
});
