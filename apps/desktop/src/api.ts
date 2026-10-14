// AIK-045·046·047·087 렌더러 계약: preload(src/aik_ipc/preload.cjs)가 노출하는 bridge의 타입과
// 실패를 던지지 않는 호출 helper. 엔진 상태 조회와 고정 payload의 연결·프로젝트 폴더 관리 명령만 있으며,
// 어떤 명령도 비밀 값이나 폴더 경로를 싣지 않는다. API 키는 main이 여는 별도 키 입력 창에만 입력하고
// 렌더러는 그 창을 요청(requestKeyEntry)해 비밀 없는 결과만 받는다. 프로젝트 폴더도 main이 여는
// OS 폴더 선택 창(requestFolder)에서만 고르며, 렌더러는 경로를 보내거나 입력받지 않는다.

/**
 * 이 앱 창의 IPC 라우팅 범위(고정). 사용자가 고른 프로젝트 폴더(AIK-087)가 아니다:
 * 선택한 프로젝트는 엔진 데이터(workspaces.select)이며 모든 호출은 이 범위로 보낸다.
 */
export const WORKSPACE_ID = "local";

export const SUPPORTED_IPC_VERSION = 1;

export type SystemState = "READY" | "DEGRADED" | "BLOCKED";
export type DirectorState = "NOT_CONFIGURED" | "STOPPED" | "RUNNING" | "UNAVAILABLE";
export type ReadmodelState = "NOT_CONFIGURED" | "ABSENT" | "READY" | "BUSY" | "INVALID" | "SCOPE_MISMATCH";
export type StateRootState = "WRITABLE" | "ABSENT" | "NOT_WRITABLE" | "UNAVAILABLE";
export type PlatformFamily = "windows" | "linux" | "macos";
export type SourceName = "vm" | "aik_runtime" | "dispatch" | "lifecycle" | "evidence" | "ags_peer";

export type HandshakeResult = {
  ipc_version: number;
  boundary: "RENDERER_NO_SECRETS";
  backend_state: string;
};

export type StatusResult = {
  state: SystemState;
  director_state: DirectorState;
};

export type EngineInfo = {
  engine_version: string;
  protocol_version: 1;
  platform_family: PlatformFamily;
  python_version: string;
  frozen: boolean;
  state_root: StateRootState;
  readmodel: ReadmodelState;
  director_state: DirectorState;
};

export type SourceRow = {
  source: SourceName;
  applied_cursor: number;
  max_seen_cursor: number;
  open_gaps: number;
  conflicts: number;
  stale: boolean;
};

export type ReadmodelSourcesResult = {
  readmodel: ReadmodelState;
  authoritative: false;
  sources: SourceRow[];
};

// -- AIK-046·047 연결 관리 DTO (src/aik_ipc/connections.cjs가 검증·고정한 형태) --------------------

export type ExecutionKind = "api" | "subscription";
export type AuthMethod = "api_key" | "host_login";
export type ConnectionStateValue = "verified" | "unverified" | "unauthenticated" | "locked" | "account_changed" | "unknown" | "unsupported";
export type CheckStateValue = "verified" | "unauthenticated" | "locked" | "account_changed" | "unknown";
export type LoginStateValue = "idle" | "pending" | "signed_in" | "failed" | "cancelled";

export type Connector = {provider: string; execution_kind: ExecutionKind; method: AuthMethod; display_name: string};
export type ConnectionCatalog = {connectors: Connector[]};

export type CheckSnapshot = {state: CheckStateValue; code: string | null};
export type LastCheck = {state: CheckStateValue; code: string | null; checked_at: string};
/** references가 null이면 사용처를 확인하지 못한 것이다. external_checked는 VM 실행 작업까지 확인했는지 뜻한다. */
export type ConnectionUsage = {references: number | null; blocking: number | null; external_checked: boolean};

export type Connection = {
  connection_id: string;
  account_label: string;
  provider: string;
  display_name: string;
  execution_kind: ExecutionKind;
  method: AuthMethod;
  state: ConnectionStateValue;
  verified_at: string | null;
  revision: number;
  binding_revision: number;
  last_check: LastCheck | null;
  usage: ConnectionUsage;
};

export type ConnectionList = {store: "ABSENT" | "READY"; connections: Connection[]};
export type ImpactReference = {kind: string; id: string; removable: boolean};
export type ConnectionImpact = {connection_id: string; references: ImpactReference[]; external_checked: boolean};
export type LoginState = {state: LoginStateValue; provider: string | null; cancel_supported: boolean; code: string | null; connection_id: string | null};
export type CancelLoginResult = {cancelled: boolean; login: LoginState};
export type ConnectionWithCheck = {connection: Connection; check: CheckSnapshot};
export type ConnectionOnly = {connection: Connection};
export type DeleteResult = {connection_id: string; removed_references: number; secret_deleted: boolean | null};
export type KeyEntryResult =
  | {outcome: "saved"; connection: Connection; check: CheckSnapshot}
  | {outcome: "cancelled"; connection: null; check: null};

// -- AIK-087 프로젝트 폴더 DTO (src/aik_ipc/workspaces.cjs가 검증·고정한 형태) -------------------

export type WorkspaceStatus = "ready" | "archived" | "root_missing" | "root_replaced" | "root_unsafe" | "access_denied" |
  "unsupported_host" | "vm_project_missing" | "vm_project_mismatch" | "vm_project_unknown";
export type WorkspaceAction = "none" | "restore_workspace" | "reselect_folder" | "confirm_rebind_root" |
  "restore_access_or_reselect" | "not_executable" | "rebind_vm_project" | "retry_vm_query";

/** root는 사용자가 고른 프로젝트 폴더의 절대 경로다. active는 현재 선택된 프로젝트인지 뜻한다. */
export type Workspace = {
  workspace_id: string;
  name: string;
  root: string;
  state: "active" | "archived";
  status: WorkspaceStatus;
  required_action: WorkspaceAction;
  revision: number;
  active: boolean;
};

/** 현재 선택. cwd는 등록된 선택이 ready일 때만 있다. registered=false면 status는 null이다. */
export type ActiveWorkspace = {
  workspace_id: string;
  registered: boolean;
  status: WorkspaceStatus | null;
  required_action: WorkspaceAction;
  cwd: string | null;
  selection_revision: number;
};

export type WorkspaceList = {
  vm: "CONFIGURED" | "NOT_CONFIGURED";
  registry: "ABSENT" | "READY";
  active: ActiveWorkspace | null;
  workspaces: Workspace[];
};

export type ScopeReason = "NO_SCOPE" | "NOT_ALLOWED" | "RECONNECTION_REQUIRED" | "UNKNOWN_CONNECTION" | "NOT_SHARED";
export type ScopeConnection = {
  connection_id: string;
  provider: string;
  owner_workspace_id: string;
  allowed: boolean;
  reason: string | null;
};
export type WorkspaceScope = {workspace_id: string; scope: "ABSENT" | "READY"; connections: ScopeConnection[]};

export type FolderMode = "open" | "create" | "rebind";
export type FolderResult =
  | {outcome: "cancelled"; workspace: null}
  | {outcome: "saved"; workspace: Workspace};

export type EnvelopeError = {
  code: string;
  message: string;
  supported_versions?: number[];
};

export type OkEnvelope<T> = {
  protocol_version: 1;
  request_id: string;
  workspace_id: string;
  command: string;
  ok: true;
  result: T;
};

export type ErrorEnvelope = {
  protocol_version: 1;
  request_id: string | null;
  ok: false;
  error: EnvelopeError;
};

export type Envelope<T> = OkEnvelope<T> | ErrorEnvelope;

export interface AikDesktopBridge {
  handshake(): Promise<Envelope<HandshakeResult>>;
  status(workspaceId: string): Promise<Envelope<StatusResult>>;
  engineInfo(workspaceId: string): Promise<Envelope<EngineInfo>>;
  readmodelSources(workspaceId: string): Promise<Envelope<ReadmodelSourcesResult>>;
  connectionCatalog(workspaceId: string): Promise<Envelope<ConnectionCatalog>>;
  connections(workspaceId: string): Promise<Envelope<ConnectionList>>;
  connectionImpact(workspaceId: string, connectionId: string): Promise<Envelope<ConnectionImpact>>;
  loginState(workspaceId: string): Promise<Envelope<LoginState>>;
  checkConnection(workspaceId: string, connectionId: string): Promise<Envelope<ConnectionWithCheck>>;
  renameConnection(workspaceId: string, connectionId: string, expectedRevision: number, accountLabel: string): Promise<Envelope<ConnectionOnly>>;
  deleteConnection(workspaceId: string, connectionId: string, expectedRevision: number, acknowledgeImpact: boolean): Promise<Envelope<DeleteResult>>;
  logoutConnection(workspaceId: string, connectionId: string, expectedRevision: number): Promise<Envelope<ConnectionOnly>>;
  startLogin(workspaceId: string, provider: string | null, accountLabel: string | null, connectionId: string | null): Promise<Envelope<LoginState>>;
  cancelLogin(workspaceId: string): Promise<Envelope<CancelLoginResult>>;
  completeLogin(workspaceId: string): Promise<Envelope<ConnectionWithCheck>>;
  requestKeyEntry(workspaceId: string, mode: "add" | "rotate", provider: string | null, accountLabel: string | null,
    connectionId: string | null, expectedRevision: number | null): Promise<Envelope<KeyEntryResult>>;
  workspaces(workspaceId: string): Promise<Envelope<WorkspaceList>>;
  workspaceScope(workspaceId: string, targetId: string): Promise<Envelope<WorkspaceScope>>;
  selectWorkspace(workspaceId: string, targetId: string): Promise<Envelope<ActiveWorkspace>>;
  archiveWorkspace(workspaceId: string, targetId: string, expectedRevision: number): Promise<Envelope<Workspace>>;
  restoreWorkspace(workspaceId: string, targetId: string, expectedRevision: number): Promise<Envelope<Workspace>>;
  requestFolder(workspaceId: string, mode: FolderMode, name: string | null, folderName: string | null,
    targetId: string | null, expectedRevision: number | null): Promise<Envelope<FolderResult>>;
}

declare global {
  interface Window {
    readonly aikDesktop?: AikDesktopBridge;
  }
}

export const APP_CONNECTION_FAILED = "APP_CONNECTION_FAILED";

/** 엔진에 닿지 못한 경우: 프로세스 미실행·무응답·bridge 부재. */
export const UNAVAILABLE_CODES: ReadonlySet<string> = new Set(["BACKEND_UNAVAILABLE", "BACKEND_TIMEOUT", APP_CONNECTION_FAILED]);
/** 앱과 엔진의 프로토콜 버전이 맞지 않는 경우. */
export const INCOMPATIBLE_CODES: ReadonlySet<string> = new Set(["BACKEND_INCOMPATIBLE", "INCOMPATIBLE_PROTOCOL"]);

function failed(code: string, message: string): ErrorEnvelope {
  return {protocol_version: 1, request_id: null, ok: false, error: {code, message}};
}

function isEnvelope(value: unknown): value is {ok: boolean} {
  return typeof value === "object" && value !== null && typeof (value as {ok?: unknown}).ok === "boolean";
}

async function call<T>(invoke: (bridge: AikDesktopBridge) => Promise<Envelope<T>>): Promise<Envelope<T>> {
  const bridge = window.aikDesktop;
  if (!bridge) {
    return failed(APP_CONNECTION_FAILED, "데스크톱 bridge(window.aikDesktop)를 찾을 수 없습니다.");
  }
  try {
    const envelope: unknown = await invoke(bridge);
    if (!isEnvelope(envelope)) {
      return failed(APP_CONNECTION_FAILED, "엔진 응답 형식이 올바르지 않습니다.");
    }
    const typed = envelope as Envelope<T>;
    if (typed.ok === false && (typeof typed.error !== "object" || typed.error === null || typeof typed.error.code !== "string")) {
      return failed(APP_CONNECTION_FAILED, "엔진 오류 응답 형식이 올바르지 않습니다.");
    }
    return typed;
  } catch (error) {
    return failed(APP_CONNECTION_FAILED, error instanceof Error ? error.message : "앱과 엔진 사이 연결에 실패했습니다.");
  }
}

export function handshake(): Promise<Envelope<HandshakeResult>> {
  return call((bridge) => bridge.handshake());
}

export function fetchStatus(workspaceId: string = WORKSPACE_ID): Promise<Envelope<StatusResult>> {
  return call((bridge) => bridge.status(workspaceId));
}

export function fetchEngineInfo(workspaceId: string = WORKSPACE_ID): Promise<Envelope<EngineInfo>> {
  return call((bridge) => bridge.engineInfo(workspaceId));
}

export function fetchReadmodelSources(workspaceId: string = WORKSPACE_ID): Promise<Envelope<ReadmodelSourcesResult>> {
  return call((bridge) => bridge.readmodelSources(workspaceId));
}

// -- 연결 관리 (AIK-046·047) ---------------------------------------------------------------

export function fetchConnectionCatalog(workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionCatalog>> {
  return call((bridge) => bridge.connectionCatalog(workspaceId));
}

export function fetchConnections(workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionList>> {
  return call((bridge) => bridge.connections(workspaceId));
}

export function fetchConnectionImpact(connectionId: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionImpact>> {
  return call((bridge) => bridge.connectionImpact(workspaceId, connectionId));
}

export function fetchLoginState(workspaceId: string = WORKSPACE_ID): Promise<Envelope<LoginState>> {
  return call((bridge) => bridge.loginState(workspaceId));
}

export function checkConnection(connectionId: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionWithCheck>> {
  return call((bridge) => bridge.checkConnection(workspaceId, connectionId));
}

export function renameConnection(connectionId: string, expectedRevision: number, accountLabel: string,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionOnly>> {
  return call((bridge) => bridge.renameConnection(workspaceId, connectionId, expectedRevision, accountLabel));
}

/** acknowledgeImpact는 사용자가 영향 범위를 보고 확인했을 때만 true로 보낸다. */
export function deleteConnection(connectionId: string, expectedRevision: number, acknowledgeImpact: boolean,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<DeleteResult>> {
  return call((bridge) => bridge.deleteConnection(workspaceId, connectionId, expectedRevision, acknowledgeImpact));
}

/** 구독 로그인 연결만 해당한다. API 키 연결은 삭제가 곧 키 제거다. */
export function logoutConnection(connectionId: string, expectedRevision: number,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionOnly>> {
  return call((bridge) => bridge.logoutConnection(workspaceId, connectionId, expectedRevision));
}

/** 새 구독 연결의 공식 로그인을 시작한다. */
export function startNewLogin(provider: string, accountLabel: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<LoginState>> {
  return call((bridge) => bridge.startLogin(workspaceId, provider, accountLabel, null));
}

/** 기존 구독 연결의 재인증을 시작한다. */
export function startReauthentication(connectionId: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<LoginState>> {
  return call((bridge) => bridge.startLogin(workspaceId, null, null, connectionId));
}

export function cancelLogin(workspaceId: string = WORKSPACE_ID): Promise<Envelope<CancelLoginResult>> {
  return call((bridge) => bridge.cancelLogin(workspaceId));
}

export function completeLogin(workspaceId: string = WORKSPACE_ID): Promise<Envelope<ConnectionWithCheck>> {
  return call((bridge) => bridge.completeLogin(workspaceId));
}

/** main이 소유한 키 입력 창을 열어 새 API 키 연결을 등록한다. 창이 닫히면 비밀 없는 결과로 끝난다. */
export function requestKeyAdd(provider: string, accountLabel: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<KeyEntryResult>> {
  return call((bridge) => bridge.requestKeyEntry(workspaceId, "add", provider, accountLabel, null, null));
}

/** main이 소유한 키 입력 창을 열어 기존 API 키 연결의 키를 교체한다. */
export function requestKeyRotate(connectionId: string, expectedRevision: number,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<KeyEntryResult>> {
  return call((bridge) => bridge.requestKeyEntry(workspaceId, "rotate", null, null, connectionId, expectedRevision));
}

// -- 프로젝트 폴더 (AIK-087) ---------------------------------------------------------------
// workspaceId 인자는 IPC 라우팅 범위(WORKSPACE_ID)이고, targetId가 대상 프로젝트 폴더다.

export function fetchWorkspaces(workspaceId: string = WORKSPACE_ID): Promise<Envelope<WorkspaceList>> {
  return call((bridge) => bridge.workspaces(workspaceId));
}

export function fetchWorkspaceScope(targetId: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<WorkspaceScope>> {
  return call((bridge) => bridge.workspaceScope(workspaceId, targetId));
}

export function selectWorkspace(targetId: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<ActiveWorkspace>> {
  return call((bridge) => bridge.selectWorkspace(workspaceId, targetId));
}

export function archiveWorkspace(targetId: string, expectedRevision: number,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<Workspace>> {
  return call((bridge) => bridge.archiveWorkspace(workspaceId, targetId, expectedRevision));
}

export function restoreWorkspace(targetId: string, expectedRevision: number,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<Workspace>> {
  return call((bridge) => bridge.restoreWorkspace(workspaceId, targetId, expectedRevision));
}

/** main이 OS 폴더 선택 창을 열어 기존 폴더를 새 프로젝트로 등록한다. 경로는 렌더러를 거치지 않는다. */
export function requestFolderOpen(name: string, workspaceId: string = WORKSPACE_ID): Promise<Envelope<FolderResult>> {
  return call((bridge) => bridge.requestFolder(workspaceId, "open", name, null, null, null));
}

/** main이 OS 폴더 선택 창에서 고른 위치 아래에 folderName 폴더를 만들어 등록한다. */
export function requestFolderCreate(name: string, folderName: string,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<FolderResult>> {
  return call((bridge) => bridge.requestFolder(workspaceId, "create", name, folderName, null, null));
}

/** 옮겼거나 바뀐 프로젝트 폴더를 OS 폴더 선택 창에서 다시 골라 연결한다. */
export function requestFolderRebind(targetId: string, expectedRevision: number,
  workspaceId: string = WORKSPACE_ID): Promise<Envelope<FolderResult>> {
  return call((bridge) => bridge.requestFolder(workspaceId, "rebind", null, null, targetId, expectedRevision));
}
