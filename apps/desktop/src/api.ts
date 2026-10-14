// AIK-045 렌더러 계약: preload(src/aik_ipc/preload.cjs)가 노출하는 읽기 전용 bridge의 타입과
// 실패를 던지지 않는 호출 helper. 렌더러는 엔진 상태를 바꾸는 명령을 갖지 않는다.

/** 단일 로컬 workspace (AIK-087 전까지 고정). */
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
  boundary: "RENDERER_READ_ONLY";
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
