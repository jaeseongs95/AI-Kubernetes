"use strict";

// AIK-087: main-owned project folder dialog. The renderer asks for a dialog with
// workspaces.request_folder and never sends a path; the folder the user picks in
// the native dialog goes from here straight to the engine. One dialog at a time.
const path = require("node:path");

const DIALOGS = Object.freeze({
  open: {title: "프로젝트 폴더 선택", buttonLabel: "이 폴더 사용"},
  create: {title: "새 프로젝트 폴더를 만들 위치 선택", buttonLabel: "여기에 만들기"},
  rebind: {title: "옮긴 프로젝트 폴더 다시 선택", buttonLabel: "이 폴더로 다시 연결"}
});

class FolderPickerError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

function enginePayload(request, folder) {
  if (request.mode === "open") return ["workspaces.open", {name: request.name, root: folder}];
  if (request.mode === "create") {
    return ["workspaces.create", {name: request.name, parent: folder, folder_name: request.folder_name}];
  }
  return ["workspaces.rebind_root", {workspace_id: request.workspace_id, root: folder,
    expected_revision: request.expected_revision}];
}

class FolderPicker {
  /**
   * @param {{showDialog: (options: object) => Promise<{canceled: boolean, filePaths: string[]}>,
   *          request: (command: string, payload: object) => Promise<object>}} options
   */
  constructor({showDialog, request}) {
    this.showDialog = showDialog;
    this.request = request;
    this.open = false;
  }

  async pick(request, signal) {
    if (this.open) throw new FolderPickerError("FOLDER_PICKER_BUSY");
    if (!Object.hasOwn(DIALOGS, request.mode)) throw new FolderPickerError("FOLDER_PICKER_UNAVAILABLE");
    this.open = true;
    let chosen;
    try {
      try {
        chosen = await this.showDialog({...DIALOGS[request.mode], properties: ["openDirectory", "dontAddToRecent"]});
      } catch {
        throw new FolderPickerError("FOLDER_PICKER_UNAVAILABLE");
      }
    } finally {
      this.open = false;
    }
    const files = chosen && Array.isArray(chosen.filePaths) ? chosen.filePaths : [];
    // A request that timed out meanwhile is not acted on: nobody is waiting for its answer.
    if (signal?.aborted || !chosen || chosen.canceled === true || files.length === 0) {
      return {outcome: "cancelled", workspace: null};
    }
    const folder = files[0];
    if (files.length !== 1 || typeof folder !== "string" || !path.isAbsolute(folder)) {
      throw new FolderPickerError("FOLDER_PICKER_UNAVAILABLE");
    }
    const [command, payload] = enginePayload(request, folder);
    return {outcome: "saved", workspace: await this.request(command, payload)};
  }
}

module.exports = {DIALOGS, FolderPicker, FolderPickerError};
