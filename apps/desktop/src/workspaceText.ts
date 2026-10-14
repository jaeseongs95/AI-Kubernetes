// AIK-087 프로젝트 폴더 화면의 한국어 문구와 입력 검사 helper.
// 엔진·main이 보내는 오류 message는 화면에 그대로 쓰지 않는다. 고정 code만 한국어로 옮기고
// code 자체는 작은 tag로만 보여 준다(connectionText.ts와 같은 규칙).
import type {EnvelopeError, WorkspaceAction, WorkspaceStatus} from "./api";
import {errorText, safeCode} from "./connectionText";

export const WORKSPACE_STATUS_TEXT: Record<WorkspaceStatus, string> = {
  ready: "실행 가능",
  archived: "보관됨",
  root_missing: "폴더가 옮겨졌거나 삭제됨",
  root_replaced: "다른 폴더로 바뀜",
  root_unsafe: "안전하지 않은 경로",
  access_denied: "접근 불가",
  unsupported_host: "이 컴퓨터에서 실행 폴더로 쓸 수 없는 위치",
  vm_project_missing: "VM project 없음",
  vm_project_mismatch: "VM project가 다른 폴더를 가리킴",
  vm_project_unknown: "VM project 상태 확인 불가"
};

export const WORKSPACE_ACTION_TEXT: Record<WorkspaceAction, string> = {
  none: "조치 필요 없음",
  restore_workspace: "복원 필요",
  reselect_folder: "폴더를 다시 선택하세요",
  confirm_rebind_root: "같은 폴더인지 확인한 뒤 다시 연결하세요",
  restore_access_or_reselect: "접근 권한을 복구하거나 폴더를 다시 선택하세요",
  not_executable: "실행할 수 없는 위치입니다",
  rebind_vm_project: "VM project를 다시 연결해야 합니다(VM 필요)",
  retry_vm_query: "VM 연결 뒤 다시 확인하세요"
};

/** 이 조치가 필요하면 목록에서 "폴더 다시 선택…"을 제공한다. */
export const REBIND_ACTIONS: ReadonlySet<WorkspaceAction> = new Set<WorkspaceAction>(
  ["reselect_folder", "confirm_rebind_root", "restore_access_or_reselect"]);

/** 연결 사용 범위에서 거부된 이유(workspaces.scope의 reason). */
const SCOPE_REASON_TEXT: Record<string, string> = {
  NO_SCOPE: "저장된 사용 범위가 없어 거부됨",
  NOT_ALLOWED: "이 프로젝트에 허용되지 않음",
  RECONNECTION_REQUIRED: "연결을 다시 해야 함",
  UNKNOWN_CONNECTION: "등록되지 않은 연결",
  NOT_SHARED: "다른 범위의 연결이며 공유되지 않음"
};

/** WORKSPACE_CODES·folder picker의 고정 code와, 프로젝트 폴더 문맥으로 다시 쓴 공용 code. */
const WORKSPACE_ERROR_TEXT: Record<string, string> = {
  WORKSPACES_NOT_CONFIGURED: "이 엔진에는 프로젝트 폴더 관리 기능이 없습니다. 화면에서 고칠 수 없으며 설치·개발 설정을 확인해야 합니다.",
  INVALID_NAME: "프로젝트 이름 규칙에 맞지 않습니다. 앞뒤 공백·제어 문자 없이 128자 이하로 정하세요.",
  INVALID_FOLDER_NAME: "새 폴더 이름을 여러 운영체제에서 쓸 수 없습니다. 다른 이름을 정하세요.",
  ROOT_INVALID: "이 폴더는 프로젝트 폴더로 쓸 수 없습니다(시스템·홈 폴더 자체 등). 다른 폴더를 고르세요.",
  ROOT_UNAVAILABLE: "폴더가 없거나 확인할 수 없습니다. 폴더가 있는지, 접근할 수 있는지 확인하세요.",
  ROOT_CONFLICT: "이미 등록된 프로젝트 폴더와 같거나 그 안팎에 겹치는 폴더입니다. 겹치지 않는 폴더를 고르세요.",
  UNKNOWN_WORKSPACE: "등록되지 않은 프로젝트 폴더입니다. 목록을 다시 읽었습니다.",
  VM_NOT_CONFIGURED: "VM이 연결되지 않아 프로젝트 폴더를 등록할 수 없습니다. VM을 연결한 뒤 다시 시도하세요.",
  VM_PROJECT_UNAVAILABLE: "VM project를 만들거나 확인하지 못했습니다. VM 상태를 확인하고 다시 시도하세요.",
  BINDING_MISMATCH: "저장된 선택이 확인된 프로젝트 연결과 다릅니다. 목록을 다시 읽고 프로젝트를 다시 선택하세요.",
  WORKSPACE_ERROR: "프로젝트 폴더 작업이 실패했습니다. 다시 시도하세요.",
  WORKSPACE_ARCHIVED: "보관된 프로젝트 폴더입니다. 먼저 복원하세요.",
  WORKSPACE_ROOT_MISSING: "프로젝트 폴더가 옮겨졌거나 삭제됐습니다. 폴더를 다시 선택하세요.",
  WORKSPACE_ROOT_REPLACED: "프로젝트 폴더가 다른 폴더로 바뀌었습니다. 같은 폴더인지 확인한 뒤 다시 연결하세요.",
  WORKSPACE_ROOT_UNSAFE: "프로젝트 폴더 경로가 더는 안전하지 않습니다. 폴더를 다시 선택하세요.",
  WORKSPACE_ACCESS_DENIED: "프로젝트 폴더에 접근할 수 없습니다. 접근 권한을 복구하거나 폴더를 다시 선택하세요.",
  WORKSPACE_UNSUPPORTED_HOST: "이 위치는 이 컴퓨터에서 실행 폴더로 쓸 수 없습니다.",
  WORKSPACE_VM_PROJECT_MISSING: "이 폴더의 VM project가 더는 없습니다. VM project를 다시 연결해야 합니다.",
  WORKSPACE_VM_PROJECT_MISMATCH: "VM project가 다른 폴더를 가리킵니다. VM project를 다시 연결해야 합니다.",
  WORKSPACE_VM_PROJECT_UNKNOWN: "VM project 상태를 확인하지 못했습니다. VM 연결 뒤 다시 확인하세요.",
  FOLDER_PICKER_BUSY: "폴더 선택 창이 이미 열려 있습니다. 열린 창을 마치거나 닫으세요.",
  FOLDER_PICKER_UNAVAILABLE: "폴더 선택 창을 열지 못했거나 고른 폴더를 받을 수 없습니다. 다시 시도하세요.",
  REVISION_CONFLICT: "그 사이 프로젝트 폴더 정보가 바뀌었습니다. 목록을 다시 읽었으니 확인한 뒤 다시 시도하세요.",
  INVALID_INPUT: "요청 값이 올바르지 않습니다. 화면을 새로 읽고 다시 시도하세요.",
  STORE_BUSY: "프로젝트 폴더 저장소가 사용 중입니다. 잠시 뒤 다시 시도하세요.",
  STORE_UNAVAILABLE: "프로젝트 폴더 저장소를 쓸 수 없습니다."
};

/** 프로젝트 폴더 오류의 한국어 설명. 모르는 code는 연결 화면의 공용 문구(BACKEND_* 등)로 넘긴다. */
export function workspaceErrorText(error: EnvelopeError | null | undefined): string {
  if (!error) return "";
  const code = safeCode(error.code);
  if (code !== null && Object.prototype.hasOwnProperty.call(WORKSPACE_ERROR_TEXT, code)) return WORKSPACE_ERROR_TEXT[code];
  return errorText(error);
}

export function scopeReasonText(reason: string | null): string {
  if (reason === null) return "";
  return Object.prototype.hasOwnProperty.call(SCOPE_REASON_TEXT, reason) ? SCOPE_REASON_TEXT[reason] : "거부됨";
}

/** connections.cjs의 isLabel과 같은 규칙. 어기면 한국어 이유를, 맞으면 null을 돌려준다. */
export function projectNameProblem(value: string): string | null {
  if (value.length === 0) return "프로젝트 이름을 입력하세요.";
  if (value.length > 128) return "프로젝트 이름은 128자 이하여야 합니다.";
  if (value !== value.trim()) return "프로젝트 이름 앞뒤에 공백을 둘 수 없습니다.";
  if ([...value].some((char) => /\p{C}/u.test(char))) return "프로젝트 이름에 제어 문자나 보이지 않는 문자를 쓸 수 없습니다.";
  return null;
}

const FOLDER_FORBIDDEN = /[<>:"/\\|?*\u0000-\u001f]/u;

/** workspaces.cjs의 isFolderName과 같은 규칙. 어기면 한국어 이유를, 맞으면 null을 돌려준다. */
export function folderNameProblem(value: string): string | null {
  if (value.length === 0) return "새 폴더 이름을 입력하세요.";
  if (value.length > 255) return "새 폴더 이름은 255자 이하여야 합니다.";
  if (value === "." || value === "..") return "\".\"이나 \"..\"은 폴더 이름으로 쓸 수 없습니다.";
  if (FOLDER_FORBIDDEN.test(value)) return "폴더 이름에 / \\ : * ? \" < > | 와 제어 문자를 쓸 수 없습니다.";
  if (/[ .]$/u.test(value)) return "폴더 이름은 공백이나 마침표(.)로 끝날 수 없습니다.";
  return null;
}
