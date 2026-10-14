"use strict";
// AIK-087: main-owned folder dialog with a fake dialog and engine. No real folder is registered.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const {DIALOGS, FolderPicker, FolderPickerError} = require("../electron/folder-picker.cjs");

const FOLDER = path.resolve("/synthetic", "사용자 폴더", "프로젝트 A");
const WORKSPACE = {workspace_id: "ws-1", name: "프로젝트 A", root: FOLDER, state: "active", status: "ready",
  required_action: "none", revision: 1, active: false};

function setup(chosen, answer = async () => WORKSPACE) {
  const dialogs = [], requests = [];
  const picker = new FolderPicker({
    showDialog: async (options) => { dialogs.push(options); return typeof chosen === "function" ? chosen() : chosen; },
    request: async (command, payload) => { requests.push([command, payload]); return answer(command, payload); }
  });
  return {picker, dialogs, requests};
}

const request = (mode, extra = {}) => ({mode, name: null, folder_name: null, workspace_id: null,
  expected_revision: null, ...extra});

test("each mode sends the chosen folder to its own engine command", async () => {
  const cases = [
    [request("open", {name: "프로젝트 A"}), "workspaces.open", {name: "프로젝트 A", root: FOLDER}],
    [request("create", {name: "새 프로젝트", folder_name: "새 폴더"}), "workspaces.create",
      {name: "새 프로젝트", parent: FOLDER, folder_name: "새 폴더"}],
    [request("rebind", {workspace_id: "ws-1", expected_revision: 3}), "workspaces.rebind_root",
      {workspace_id: "ws-1", root: FOLDER, expected_revision: 3}]
  ];
  for (const [input, command, payload] of cases) {
    const {picker, dialogs, requests} = setup({canceled: false, filePaths: [FOLDER]});
    assert.deepEqual(await picker.pick(input), {outcome: "saved", workspace: WORKSPACE});
    assert.deepEqual(requests, [[command, payload]]);
    assert.deepEqual(dialogs, [{...DIALOGS[input.mode], properties: ["openDirectory", "dontAddToRecent"]}]);
  }
});

test("a cancelled dialog or a timed-out request sends nothing to the engine", async () => {
  for (const chosen of [{canceled: true, filePaths: []}, {canceled: false, filePaths: []}, undefined]) {
    const {picker, requests} = setup(chosen);
    assert.deepEqual(await picker.pick(request("open", {name: "A"})), {outcome: "cancelled", workspace: null});
    assert.equal(requests.length, 0);
  }
  const controller = new AbortController();
  const {picker, requests} = setup(() => { controller.abort(); return {canceled: false, filePaths: [FOLDER]}; });
  assert.deepEqual(await picker.pick(request("open", {name: "A"}), controller.signal),
    {outcome: "cancelled", workspace: null});
  assert.equal(requests.length, 0);
});

test("one dialog at a time, and broken dialogs or relative paths are refused", async () => {
  let release;
  const {picker} = setup(() => new Promise((resolve) => { release = resolve; }));
  const first = picker.pick(request("open", {name: "A"}));
  await assert.rejects(picker.pick(request("open", {name: "B"})), (error) =>
    error instanceof FolderPickerError && error.code === "FOLDER_PICKER_BUSY");
  release({canceled: true, filePaths: []});
  assert.equal((await first).outcome, "cancelled");
  for (const chosen of [() => { throw new Error("no display"); }, {canceled: false, filePaths: ["relative/folder"]},
    {canceled: false, filePaths: [FOLDER, FOLDER]}]) {
    const {picker: broken, requests} = setup(chosen);
    await assert.rejects(broken.pick(request("open", {name: "A"})), (error) => error.code === "FOLDER_PICKER_UNAVAILABLE");
    assert.equal(requests.length, 0);
  }
  // The busy flag is released after a failure, so the next dialog can open.
  const {picker: again} = setup({canceled: true, filePaths: []});
  assert.equal((await again.pick(request("open", {name: "A"}))).outcome, "cancelled");
});

test("engine failures pass through unchanged", async () => {
  const failure = Object.assign(new Error("ENGINE_REJECTED"), {code: "ENGINE_REJECTED", detail: "VM_NOT_CONFIGURED"});
  const {picker} = setup({canceled: false, filePaths: [FOLDER]}, async () => { throw failure; });
  await assert.rejects(picker.pick(request("open", {name: "A"})), (error) => error === failure);
});
