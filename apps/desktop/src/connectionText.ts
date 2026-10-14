// AIK-046·047 연결 화면의 한국어 문구와 표시 helper.
// 엔진·main이 보내는 오류 message는 화면에 그대로 쓰지 않는다. 고정 code만 한국어로 옮기고
// code 자체는 작은 tag로만 보여 준다(비밀이나 민감한 오류 원문이 화면에 나오지 않게).
import {INCOMPATIBLE_CODES, UNAVAILABLE_CODES} from "./api";
import type {CheckStateValue, ConnectionStateValue, EnvelopeError, ExecutionKind, LoginStateValue} from "./api";

export const EXECUTION_TEXT: Record<ExecutionKind, string> = {api: "API 키", subscription: "구독 로그인"};

export const CONNECTION_STATE_TEXT: Record<ConnectionStateValue, string> = {
  verified: "확인됨",
  unverified: "확인되지 않음",
  unauthenticated: "인증 실패",
  locked: "잠김",
  account_changed: "다른 계정으로 로그인됨",
  unknown: "확인 결론 없음",
  unsupported: "이 엔진에서 지원하지 않음"
};

export const CONNECTION_STATE_DETAIL: Record<ConnectionStateValue, string> = {
  verified: "저장된 확인 시각이 있습니다. 지금 다시 검사했다는 뜻은 아닙니다.",
  unverified: "아직 확인한 적이 없습니다(새 연결, 키 교체·거절·로그아웃 뒤). 검사를 실행하세요.",
  unauthenticated: "마지막 검사에서 벤더가 키를 거절했거나 공식 호스트가 로그인되어 있지 않았습니다.",
  locked: "마지막 검사에서 자격 증명을 읽지 못했습니다. OS 사용자 세션·비밀 저장소 잠금을 푼 뒤 다시 검사하세요. 잠긴 동안 이 연결을 쓰는 작업은 인증에 실패할 수 있습니다.",
  account_changed: "공식 호스트가 등록한 계정과 다른 계정을 보고했습니다. 원래 계정으로 재인증하거나 새 연결로 등록하세요.",
  unknown: "마지막 검사가 결론을 내지 못했습니다(네트워크, connector 실패 등). 다시 검사하세요.",
  unsupported: "이 엔진에 이 벤더·실행 경로의 connector가 없습니다."
};

export const CHECK_STATE_TEXT: Record<CheckStateValue, string> = {
  verified: "확인됨",
  unauthenticated: "인증 실패",
  locked: "잠김",
  account_changed: "다른 계정",
  unknown: "결론 없음"
};

export const LOGIN_STATE_TEXT: Record<LoginStateValue, string> = {
  idle: "시작한 로그인 없음",
  pending: "공식 로그인 진행 중",
  signed_in: "공식 호스트가 로그인을 마쳤다고 보고함",
  failed: "로그인 실패",
  cancelled: "로그인 취소됨"
};

/** 검사·로그인 결과에 붙는 고정 code(연결 오류 code와 별도). */
const CHECK_CODE_TEXT: Record<string, string> = {
  CHECK_FAILED: "connector가 검사를 끝내지 못했습니다.",
  ACCOUNT_CHANGED: "공식 호스트가 다른 계정을 보고했습니다.",
  SIGNED_OUT: "로그아웃한 뒤입니다.",
  LOGIN_OBSERVE_FAILED: "로그인 상태를 관찰하지 못했습니다."
};

/** CONNECTION_CODES와 bridge·main의 고정 code. */
const ERROR_TEXT: Record<string, string> = {
  CONNECTIONS_NOT_CONFIGURED: "이 엔진에는 연결 관리 서비스가 없습니다. 화면에서 고칠 수 없으며 설치·개발 설정을 확인해야 합니다.",
  UNSUPPORTED_PROVIDER: "이 벤더·실행 경로를 맡는 connector가 없습니다. 목록에 있는 항목을 고르세요.",
  INVALID_SECRET: "키가 형식 검사를 통과하지 못했습니다.",
  KEY_REJECTED: "벤더가 키를 거절했습니다. 저장하지 않았습니다.",
  KEY_REJECTED_CLEANUP_UNCONFIRMED: "벤더가 키를 거절했고 새 연결을 지우지 못했습니다. 목록에서 그 연결을 삭제하세요.",
  INVALID_INPUT: "요청 값이 올바르지 않습니다. 화면을 새로 읽고 다시 시도하세요.",
  INVALID_LABEL: "계정 별칭 규칙에 맞지 않습니다. 앞뒤 공백·제어 문자 없이 128자 이하로 정하세요.",
  VAULT_UNAVAILABLE: "사용자 비밀 저장소를 쓸 수 없습니다. OS 비밀 저장소 상태를 확인하고 다시 시도하세요.",
  VAULT_LOCKED: "비밀 저장소가 잠겼거나 접근이 거부됐습니다. OS 사용자 세션·저장소 잠금을 푼 뒤 다시 시도하세요.",
  VAULT_ERROR: "비밀 저장소가 실패했습니다. 다시 시도하세요.",
  DUPLICATE_CONNECTION: "같은 별칭 또는 같은 계정의 연결이 이미 있습니다. 다른 별칭을 쓰거나 기존 연결을 쓰세요.",
  UNKNOWN_CONNECTION: "연결이 없습니다. 목록을 다시 읽었습니다.",
  REVISION_CONFLICT: "그 사이 연결이 바뀌었습니다. 목록을 다시 읽었으니 다시 시도하세요.",
  CONNECTION_IN_USE: "이 연결은 사용 중입니다. 영향 범위를 확인하세요.",
  STORE_BUSY: "연결 저장소가 사용 중입니다. 잠시 뒤 다시 시도하세요.",
  STORE_UNAVAILABLE: "연결 저장소를 쓸 수 없습니다.",
  SECRET_CLEANUP_UNCONFIRMED: "등록이 실패했고 새로 넣은 키의 삭제를 확인하지 못했습니다. 연결은 저장되지 않았습니다.",
  WRONG_METHOD: "이 동작은 이 연결의 실행 경로에 맞지 않습니다.",
  LOGIN_IN_PROGRESS: "다른 공식 로그인이 진행 중입니다. 그 로그인을 끝내거나 취소한 뒤 다시 시도하세요.",
  LOGIN_START_FAILED: "공식 로그인을 시작하지 못했습니다. 공식 호스트 CLI 설치·실행 여부를 확인하세요.",
  NO_LOGIN: "시작한 공식 로그인이 없습니다. 로그인을 다시 시작하세요.",
  LOGIN_PENDING: "공식 로그인이 아직 끝나지 않았습니다. 공식 로그인 창을 마친 뒤 다시 누르세요.",
  NOT_SIGNED_IN: "공식 호스트가 로그인되어 있지 않습니다. 공식 로그인을 마친 뒤 다시 확인하세요.",
  ACCOUNT_CHANGED: "공식 호스트가 다른 계정으로 로그인되어 있습니다. 원래 계정으로 다시 로그인하거나 새 연결로 등록하세요.",
  LOGOUT_FAILED: "공식 호스트 로그아웃이 실패했습니다. 공식 호스트에서 로그아웃 상태를 직접 확인하세요.",
  KEY_ENTRY_BUSY: "키 입력 창이 이미 열려 있습니다. 열린 창을 마치거나 닫으세요.",
  KEY_ENTRY_UNAVAILABLE: "키 입력 창을 열지 못했습니다. 앱을 다시 시작하세요.",
  BACKEND_UNAVAILABLE: "엔진에 연결할 수 없습니다. 엔진이 실행 중인지 확인하고 다시 시도하세요.",
  BACKEND_TIMEOUT: "엔진 응답 시간이 초과됐습니다. 다시 시도하세요.",
  BACKEND_INCOMPATIBLE: "앱과 엔진의 버전이 서로 다릅니다. 앱을 다시 설치하거나 업데이트해야 합니다.",
  INCOMPATIBLE_PROTOCOL: "앱과 엔진의 프로토콜 버전이 서로 다릅니다.",
  INVALID_RESPONSE: "엔진 응답 형식이 올바르지 않습니다. 다시 시도하세요.",
  APP_CONNECTION_FAILED: "앱과 엔진 사이 연결에 실패했습니다. 다시 시도하세요."
};

const CODE_PATTERN = /^[A-Z][A-Z0-9_]{1,63}$/;

/** 화면에 표시해도 되는 고정 code만 돌려준다. 형식이 다르면 표시하지 않는다. */
export function safeCode(code: string | null | undefined): string | null {
  return typeof code === "string" && CODE_PATTERN.test(code) ? code : null;
}

export function errorText(error: EnvelopeError | null | undefined): string {
  if (!error) return "";
  const code = safeCode(error.code);
  if (code !== null && Object.prototype.hasOwnProperty.call(ERROR_TEXT, code)) return ERROR_TEXT[code];
  return "요청을 처리하지 못했습니다. 다시 시도하세요.";
}

export function checkCodeText(code: string | null): string | null {
  const safe = safeCode(code);
  if (safe === null) return null;
  if (Object.prototype.hasOwnProperty.call(CHECK_CODE_TEXT, safe)) return CHECK_CODE_TEXT[safe];
  if (Object.prototype.hasOwnProperty.call(ERROR_TEXT, safe)) return ERROR_TEXT[safe];
  return null;
}

export function isUnavailable(error: EnvelopeError): boolean {
  return UNAVAILABLE_CODES.has(error.code) || INCOMPATIBLE_CODES.has(error.code) || error.code === "INVALID_RESPONSE";
}

/** ISO UTC 시각을 사용자 로컬 시각 문자열로 바꾼다. */
export function formatTimestamp(value: string | null): string {
  if (value === null) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** connections.cjs의 isLabel과 같은 규칙. 어기면 한국어 이유를, 맞으면 null을 돌려준다. */
export function labelProblem(value: string): string | null {
  if (value.length === 0) return "계정 별칭을 입력하세요.";
  if (value.length > 128) return "계정 별칭은 128자 이하여야 합니다.";
  if (value !== value.trim()) return "계정 별칭 앞뒤에 공백을 둘 수 없습니다.";
  if ([...value].some((char) => /\p{C}/u.test(char))) return "계정 별칭에 제어 문자나 보이지 않는 문자를 쓸 수 없습니다.";
  return null;
}
