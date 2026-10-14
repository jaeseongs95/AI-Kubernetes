// AIK-047 연결 관리 화면: 계정 별칭·벤더·인증 방식·연결 상태·마지막 확인·사용 범위를 보여 주고
// 검사·키 교체·이름 변경·로그아웃·재인증·삭제를 GUI에서 한다. 비밀 값과 엔진 오류 원문은 표시하지 않는다.
// data-state: LOADING(첫 응답 전) · NOT_CONFIGURED · UNAVAILABLE · EMPTY · READY
import {useEffect, useRef, useState} from "react";
import {
  checkConnection,
  deleteConnection,
  fetchConnectionImpact,
  fetchConnections,
  logoutConnection,
  renameConnection,
  requestKeyRotate
} from "./api";
import type {CheckSnapshot, Connection, ConnectionImpact, ConnectionList, DeleteResult, EnvelopeError} from "./api";
import {
  CHECK_STATE_TEXT,
  CONNECTION_STATE_DETAIL,
  CONNECTION_STATE_TEXT,
  EXECUTION_TEXT,
  checkCodeText,
  errorText,
  formatTimestamp,
  isUnavailable,
  labelProblem
} from "./connectionText";
import {CodeTag, LoginFlow, NoticeBox, OtherLoginControl, errorNotice} from "./loginFlow";
import type {LoginEnd, Notice} from "./loginFlow";
import {Onboarding} from "./onboarding";

const RETRY_UNAVAILABLE_MS = 5000;
/** 목록을 다시 읽어야 하는 오류: 그 사이 바뀌었거나 사라진 연결. */
const RELOAD_CODES = new Set(["REVISION_CONFLICT", "UNKNOWN_CONNECTION", "KEY_REJECTED_CLEANUP_UNCONFIRMED"]);

export type ConnectionsDataState = "LOADING" | "NOT_CONFIGURED" | "UNAVAILABLE" | "EMPTY" | "READY";

function checkNotice(prefix: string, check: CheckSnapshot): Notice {
  const verified = check.state === "verified";
  const detail = checkCodeText(check.code);
  return {
    tone: verified ? "ok" : "warn",
    text: `${prefix ? prefix + " " : ""}검사 결과: ${CHECK_STATE_TEXT[check.state]}${verified ? "" : " (저장됐지만 확인되지 않음)"}${detail ? " · " + detail : ""}`,
    code: check.code
  };
}

function usageText(connection: Connection): string {
  const {references, blocking} = connection.usage;
  if (references === null) return "확인 불가";
  if (references === 0) return "사용처 없음";
  return `참조 ${references}건` + (blocking !== null && blocking > 0 ? ` · 삭제를 막는 참조 ${blocking}건` : "");
}

function runningImpactText(connection: Connection): string {
  return connection.usage.external_checked ? "확인함 (VM 실행 작업까지 사용 범위에 포함)" : "확인 불가(VM 미연결)";
}

export function ConnectionsRoute({onLoadedChange}: {onLoadedChange: (loaded: boolean) => void}) {
  const [list, setList] = useState<ConnectionList | null>(null);
  const [error, setError] = useState<EnvelopeError | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [pageNotice, setPageNotice] = useState<Notice | null>(null);
  const firstLoad = useRef(true);
  const alive = useRef(true);
  const loadedRef = useRef(onLoadedChange);
  loadedRef.current = onLoadedChange;

  async function reload(): Promise<void> {
    setLoading(true);
    const answer = await fetchConnections();
    if (!alive.current) return;
    setLoading(false);
    if (answer.ok) {
      setList(answer.result);
      setError(null);
      if (firstLoad.current && answer.result.connections.length === 0) setOnboardingOpen(true);
    } else {
      setError(answer.error);
    }
    if (firstLoad.current) {
      firstLoad.current = false;
      setLoaded(true);
      loadedRef.current(true);
    }
  }

  useEffect(() => {
    alive.current = true;
    loadedRef.current(false);
    void reload();
    return () => {
      alive.current = false;
    };
  }, []);

  // 엔진에 닿지 못해 목록이 없으면 5초마다 다시 읽는다.
  useEffect(() => {
    if (list !== null || error === null || !isUnavailable(error)) return;
    const timer = setTimeout(() => void reload(), RETRY_UNAVAILABLE_MS);
    return () => clearTimeout(timer);
  }, [list, error]);

  function deleted(result: DeleteResult, label: string) {
    const parts = [`"${label}" 연결을 삭제했습니다.`];
    if (result.secret_deleted === true) parts.push("저장된 키도 비밀 저장소에서 지웠습니다.");
    if (result.removed_references > 0) parts.push(`사용처 참조 ${result.removed_references}건을 정리했습니다.`);
    if (result.secret_deleted === false) parts.push("저장된 키 삭제는 확인하지 못했습니다. OS 비밀 저장소를 확인하세요.");
    setPageNotice({tone: result.secret_deleted === false ? "warn" : "ok", text: parts.join(" ")});
    void reload();
  }

  const connections = list?.connections ?? [];
  let dataState: ConnectionsDataState;
  if (!loaded) dataState = "LOADING";
  else if (list === null) dataState = error?.code === "CONNECTIONS_NOT_CONFIGURED" ? "NOT_CONFIGURED" : "UNAVAILABLE";
  else dataState = connections.length === 0 ? "EMPTY" : "READY";
  const canManage = list !== null;

  return <section id="route-connections" data-state={dataState} aria-busy={loading}>
    <header className="route-header">
      <span className="eyebrow">CONNECTIONS</span>
      <h1>연결 관리</h1>
      <p>벤더 계정 연결을 추가하고 상태를 확인합니다. API 키는 이 화면에 입력하거나 표시하지 않으며, 앱이 따로 여는 보안 입력 창에서만 입력합니다.</p>
      {canManage && <div className="actions header-actions">
        <button type="button" className="primary" onClick={() => setOnboardingOpen(true)} disabled={onboardingOpen}>연결 추가</button>
        <button type="button" onClick={() => void reload()} disabled={loading}>{loading ? "읽는 중…" : "새로고침"}</button>
      </div>}
    </header>

    {dataState === "LOADING" && <p className="muted">엔진에서 연결 목록을 읽는 중입니다.</p>}

    {dataState === "NOT_CONFIGURED" && <div className="panel inline-state" data-error-code="CONNECTIONS_NOT_CONFIGURED">
      <strong>연결 관리 서비스 없음 <CodeTag code="CONNECTIONS_NOT_CONFIGURED" /></strong>
      <p>{errorText(error)} 엔진이 연결 서비스(connector)를 조립하지 못했거나 이 빌드에 포함되지 않았습니다.</p>
      <div className="actions"><button type="button" onClick={() => void reload()} disabled={loading}>다시 확인</button></div>
    </div>}

    {dataState === "UNAVAILABLE" && error && <div className="panel error-panel" data-error-code={error.code}>
      <strong>연결 목록을 읽지 못했습니다 <CodeTag code={error.code} /></strong>
      <p>{errorText(error)} {isUnavailable(error) ? "5초마다 다시 시도합니다." : ""}</p>
      <div className="actions"><button type="button" onClick={() => void reload()} disabled={loading}>다시 시도</button></div>
    </div>}

    {list !== null && error !== null && <p className="stale-notice" data-stale="1">
      마지막으로 받은 목록입니다 · 최근 새로 읽기 실패: {errorText(error)} <CodeTag code={error.code} />
    </p>}

    <NoticeBox notice={pageNotice} id="connections-notice" />

    {canManage && onboardingOpen && <Onboarding onChanged={() => void reload()}
      onClose={connections.length > 0 ? () => setOnboardingOpen(false) : undefined} />}

    {dataState === "EMPTY" && !onboardingOpen && <div className="panel pending-panel">
      <strong>등록된 연결이 없습니다</strong>
      <p>{list?.store === "ABSENT" ? "아직 연결 저장소가 만들어지지 않은 첫 실행 상태입니다." : "모든 연결이 삭제됐습니다."} 연결 추가로 벤더 계정을 연결하세요.</p>
    </div>}

    {connections.length > 0 && <div className="connection-list" aria-label="연결 목록">
      {connections.map((connection) => <ConnectionCard key={connection.connection_id} connection={connection}
        reload={reload} onDeleted={deleted} />)}
    </div>}
  </section>;
}

type Panel =
  | {kind: "rename"}
  | {kind: "logout"}
  | {kind: "reauth"; attempt: number}
  | {kind: "delete"; impact: ConnectionImpact | null; impactError: EnvelopeError | null; acknowledged: boolean};

type Busy = "check" | "rotate" | "rename" | "logout" | "delete" | "impact" | null;

function ConnectionCard({connection, reload, onDeleted}: {
  connection: Connection;
  reload: () => Promise<void>;
  onDeleted: (result: DeleteResult, label: string) => void;
}) {
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [panel, setPanel] = useState<Panel | null>(null);
  const [lastLoginCode, setLastLoginCode] = useState<string | null>(null);
  const id = connection.connection_id;
  const domId = "conn-" + id.replace(/[^A-Za-z0-9_-]/g, "-");
  const api = connection.execution_kind === "api";
  const locked = connection.state === "locked";

  function failed(error: EnvelopeError) {
    setNotice(errorNotice(error));
    if (RELOAD_CODES.has(error.code)) void reload();
  }

  async function runCheck() {
    setBusy("check");
    setNotice(null);
    const answer = await checkConnection(id);
    setBusy(null);
    if (!answer.ok) return failed(answer.error);
    setNotice(checkNotice("", answer.result.check));
    void reload();
  }

  async function rotateKey() {
    setBusy("rotate");
    setNotice({tone: "info", text: "키 입력 창이 열렸습니다. 그 창에서 새 키를 저장하거나 취소하세요."});
    const answer = await requestKeyRotate(id, connection.revision);
    setBusy(null);
    if (!answer.ok) return failed(answer.error);
    if (answer.result.outcome === "saved") {
      setNotice(checkNotice("키를 교체했습니다.", answer.result.check));
      void reload();
    } else {
      setNotice({tone: "info", text: "키 교체를 취소했습니다. 기존 키는 그대로입니다."});
    }
  }

  async function rename(label: string) {
    setBusy("rename");
    setNotice(null);
    const answer = await renameConnection(id, connection.revision, label);
    setBusy(null);
    if (!answer.ok) return failed(answer.error);
    setPanel(null);
    setNotice({tone: "ok", text: `별칭을 "${answer.result.connection.account_label}"(으)로 바꿨습니다.`});
    void reload();
  }

  async function logout() {
    setBusy("logout");
    setNotice(null);
    const answer = await logoutConnection(id, connection.revision);
    setBusy(null);
    setPanel(null);
    if (!answer.ok) return failed(answer.error);
    setNotice({tone: "ok", text: "공식 호스트에서 로그아웃했습니다. 다시 쓰려면 재인증하세요."});
    void reload();
  }

  async function openDelete() {
    setNotice(null);
    setPanel({kind: "delete", impact: null, impactError: null, acknowledged: false});
    setBusy("impact");
    const answer = await fetchConnectionImpact(id);
    setBusy(null);
    setPanel({kind: "delete", impact: answer.ok ? answer.result : null, impactError: answer.ok ? null : answer.error, acknowledged: false});
    if (!answer.ok && RELOAD_CODES.has(answer.error.code)) void reload();
  }

  async function confirmDelete(acknowledged: boolean) {
    setBusy("delete");
    setNotice(null);
    const answer = await deleteConnection(id, connection.revision, acknowledged);
    setBusy(null);
    if (answer.ok) {
      setPanel(null);
      onDeleted(answer.result, connection.account_label);
      return;
    }
    if (answer.error.code === "CONNECTION_IN_USE") {
      // 엔진이 사용 중이라고 답했다. 최신 영향 범위를 다시 보여 주고 사용자 확인을 다시 받는다.
      await openDelete();
      setNotice({tone: "warn", text: "이 연결은 사용 중입니다. 아래 영향 범위를 확인하고, 그래도 삭제하려면 확인란을 선택한 뒤 다시 누르세요.", code: answer.error.code});
      return;
    }
    failed(answer.error);
  }

  function reauthDone(result: {connection: Connection; check: CheckSnapshot}) {
    setPanel(null);
    setLastLoginCode(null);
    setNotice(checkNotice("재인증을 마쳤습니다.", result.check));
    void reload();
  }

  function reauthEnded(end: LoginEnd) {
    setPanel(null);
    if (end.kind === "cancelled") {
      setLastLoginCode(null);
      setNotice({tone: "info", text: "재인증이 취소됐습니다. 연결은 바뀌지 않았습니다. 재인증을 다시 누르면 다시 시작합니다."});
      return;
    }
    setLastLoginCode(end.code);
    const text = end.error ? errorText(end.error) : checkCodeText(end.code) ?? "공식 로그인이 실패했습니다.";
    setNotice({tone: "error", text: text + " 재인증을 다시 누르면 다시 시작합니다.", code: end.code});
    if (end.error && RELOAD_CODES.has(end.error.code)) void reload();
  }

  const lastCheck = connection.last_check;
  const disabled = busy !== null || (panel !== null && panel.kind === "reauth");
  return <article className="panel connection-card" id={domId} data-connection-id={id} data-state={connection.state}
    data-kind={connection.execution_kind} data-locked={locked ? "1" : "0"} aria-labelledby={domId + "-title"}>
    <div className="card-head">
      <div>
        <h2 id={domId + "-title"}>{connection.account_label}</h2>
        <p className="muted">{connection.display_name} <span className="provider-id">{connection.provider}</span></p>
      </div>
      <div className="card-badges">
        <span className="badge" data-kind={connection.execution_kind}>{EXECUTION_TEXT[connection.execution_kind]}</span>
        <span className="badge" data-connection-state={connection.state}>
          {CONNECTION_STATE_TEXT[connection.state]}
        </span>
      </div>
    </div>

    <dl className="fields">
      <div className="field"><dt>계정 별칭</dt><dd>{connection.account_label}</dd></div>
      <div className="field"><dt>벤더</dt><dd>{connection.display_name}</dd></div>
      <div className="field"><dt>인증 방식</dt><dd>{EXECUTION_TEXT[connection.execution_kind]}</dd></div>
      <div className="field"><dt>연결 상태</dt><dd>
        <span className="state-text">{CONNECTION_STATE_TEXT[connection.state]}</span>
        {locked && <span className="flag warn">잠금</span>}
      </dd></div>
      <div className="field"><dt>마지막 확인</dt><dd>
        {lastCheck ? <>
          <span>{formatTimestamp(lastCheck.checked_at)}</span>
          <span className="state-text">{CHECK_STATE_TEXT[lastCheck.state]}</span> <CodeTag code={lastCheck.code} />
        </> : <span className="muted">검사 기록 없음</span>}
      </dd></div>
      <div className="field"><dt>확인된 시각</dt><dd>{connection.verified_at ? formatTimestamp(connection.verified_at) : <span className="muted">없음</span>}</dd></div>
      <div className="field"><dt>사용 범위</dt><dd data-usage={connection.usage.references === null ? "unknown" : String(connection.usage.references)}>{usageText(connection)}</dd></div>
      <div className="field"><dt>실행 중 작업 영향</dt><dd data-external-checked={connection.usage.external_checked ? "1" : "0"}>{runningImpactText(connection)}</dd></div>
    </dl>
    <p className="state-detail">{CONNECTION_STATE_DETAIL[connection.state]}</p>
    {api && <p className="muted small">API 키 연결은 따로 로그아웃하지 않습니다. 저장된 키를 지우려면 연결을 삭제하세요(삭제하면 키도 함께 지웁니다).</p>}

    <div className="actions card-actions" role="group" aria-label={`${connection.account_label} 연결 동작`}>
      <button type="button" onClick={() => void runCheck()} disabled={disabled}>{busy === "check" ? "검사 중…" : "검사"}</button>
      {api && <button type="button" onClick={() => void rotateKey()} disabled={disabled}>{busy === "rotate" ? "키 입력 창 열림…" : "키 교체"}</button>}
      <button type="button" onClick={() => setPanel({kind: "rename"})} disabled={disabled} aria-expanded={panel?.kind === "rename"}>이름 변경</button>
      {!api && <button type="button" onClick={() => setPanel({kind: "reauth", attempt: Date.now()})} disabled={disabled}>재인증</button>}
      {!api && <button type="button" onClick={() => setPanel({kind: "logout"})} disabled={disabled} aria-expanded={panel?.kind === "logout"}>로그아웃</button>}
      <button type="button" className="danger" onClick={() => void openDelete()} disabled={disabled} aria-expanded={panel?.kind === "delete"}>삭제</button>
    </div>

    {panel?.kind === "rename" && <RenamePanel domId={domId} current={connection.account_label} busy={busy === "rename"}
      onSave={(label) => void rename(label)} onCancel={() => setPanel(null)} />}

    {panel?.kind === "logout" && <div className="sub-panel" role="group" aria-label="로그아웃 확인">
      <p>로그아웃하면 공식 호스트에서 이 계정이 로그아웃되고 연결은 "확인되지 않음"이 됩니다. 이 연결을 쓰는 작업은 다음 호출부터 인증에 실패할 수 있습니다.</p>
      <p className="muted">사용 범위: {usageText(connection)} · 실행 중 작업 영향: {runningImpactText(connection)}</p>
      <div className="actions">
        <button type="button" className="danger" onClick={() => void logout()} disabled={busy !== null}>{busy === "logout" ? "로그아웃 중…" : "로그아웃"}</button>
        <button type="button" onClick={() => setPanel(null)} disabled={busy !== null}>취소</button>
      </div>
    </div>}

    {panel?.kind === "reauth" && <div className="sub-panel">
      <LoginFlow key={panel.attempt} target={{kind: "reauth", connectionId: id, accountLabel: connection.account_label}} autoStart
        onDone={reauthDone} onEnded={reauthEnded} />
    </div>}

    {panel?.kind === "delete" && <DeletePanel domId={domId} connection={connection} panel={panel} busy={busy}
      onAcknowledge={(value) => setPanel({...panel, acknowledged: value})}
      onConfirm={(acknowledged) => void confirmDelete(acknowledged)} onRetryImpact={() => void openDelete()}
      onCancel={() => setPanel(null)} />}

    <NoticeBox notice={notice} />
    {lastLoginCode === "LOGIN_IN_PROGRESS" && <OtherLoginControl />}
  </article>;
}

function RenamePanel({domId, current, busy, onSave, onCancel}: {
  domId: string; current: string; busy: boolean; onSave: (label: string) => void; onCancel: () => void;
}) {
  const [value, setValue] = useState(current);
  const [problem, setProblem] = useState<string | null>(null);
  const inputId = domId + "-rename";
  return <form className="sub-panel" onSubmit={(event) => {
    event.preventDefault();
    const issue = labelProblem(value) ?? (value === current ? "지금과 같은 별칭입니다." : null);
    setProblem(issue);
    if (issue === null) onSave(value);
  }} onKeyDown={(event) => {
    if (event.key === "Escape") onCancel();
  }}>
    <label htmlFor={inputId} className="field-label">새 계정 별칭</label>
    <input id={inputId} type="text" value={value} maxLength={128} autoComplete="off" spellCheck={false} autoFocus
      aria-invalid={problem !== null} aria-describedby={problem ? inputId + "-error" : undefined}
      onChange={(event) => setValue(event.target.value)} />
    {problem && <p id={inputId + "-error"} className="field-error">{problem}</p>}
    <div className="actions">
      <button type="submit" className="primary" disabled={busy}>{busy ? "저장 중…" : "저장"}</button>
      <button type="button" onClick={onCancel} disabled={busy}>취소</button>
    </div>
  </form>;
}

function DeletePanel({domId, connection, panel, busy, onAcknowledge, onConfirm, onRetryImpact, onCancel}: {
  domId: string;
  connection: Connection;
  panel: Extract<Panel, {kind: "delete"}>;
  busy: Busy;
  onAcknowledge: (value: boolean) => void;
  onConfirm: (acknowledged: boolean) => void;
  onRetryImpact: () => void;
  onCancel: () => void;
}) {
  const {impact, impactError, acknowledged} = panel;
  const blocking = impact ? impact.references.filter((reference) => !reference.removable) : [];
  const needsAck = impact !== null && (impact.references.length > 0 || !impact.external_checked);
  const ackId = domId + "-ack";
  return <div className="sub-panel delete-panel" role="group" aria-label="삭제 확인" data-impact-loaded={impact ? "1" : "0"}>
    <strong>"{connection.account_label}" 연결 삭제</strong>
    <p>{connection.execution_kind === "api"
      ? "삭제하면 이 연결과 비밀 저장소에 저장된 API 키를 함께 지웁니다. 되돌릴 수 없습니다."
      : "삭제하면 이 연결 기록을 지웁니다. 공식 호스트의 로그인 상태는 따로 바뀌지 않을 수 있습니다. 되돌릴 수 없습니다."}</p>
    {busy === "impact" && <p className="muted">영향 범위를 확인하는 중입니다.</p>}
    {impactError && <div className="inline-error">
      <p>영향 범위를 확인하지 못했습니다. {errorText(impactError)} <CodeTag code={impactError.code} /></p>
      <button type="button" onClick={onRetryImpact}>다시 확인</button>
    </div>}
    {impact && <>
      <h3 className="sub-title">영향 범위</h3>
      {impact.references.length === 0 ? <p>이 연결을 쓰는 참조가 없습니다.</p> : <ul className="impact-list">
        {impact.references.map((reference) => <li key={reference.kind + ":" + reference.id} data-removable={reference.removable ? "1" : "0"}>
          <code>{reference.kind}</code> <code>{reference.id}</code>{" "}
          {reference.removable ? <span className="flag ok">삭제 시 함께 정리</span> : <span className="flag warn">제거 불가 — 먼저 정리해야 함</span>}
        </li>)}
      </ul>}
      <p>실행 중 작업 영향: {impact.external_checked ? "VM 실행 작업까지 확인했습니다." : "확인 불가(VM 미연결). 실행 중인 작업이 이 연결을 쓰고 있을 수 있습니다."}</p>
      {blocking.length > 0 && <p className="field-error">제거할 수 없는 참조 {blocking.length}건이 있어 지금은 삭제할 수 없습니다. 그 참조를 먼저 정리하세요.</p>}
      {needsAck && blocking.length === 0 && <label htmlFor={ackId} className="choice">
        <input id={ackId} type="checkbox" checked={acknowledged} onChange={(event) => onAcknowledge(event.target.checked)} />
        <span>위 영향 범위를 확인했고, 사용 중일 수 있는 연결을 삭제합니다.</span>
      </label>}
    </>}
    <div className="actions">
      <button type="button" className="danger" onClick={() => onConfirm(needsAck && acknowledged)}
        disabled={busy !== null || impact === null || blocking.length > 0 || (needsAck && !acknowledged)}>
        {busy === "delete" ? "삭제 중…" : "삭제"}
      </button>
      <button type="button" onClick={onCancel} disabled={busy === "delete"}>취소</button>
    </div>
  </div>;
}
