import {useEffect, useRef, useState} from "react";
import type {ReactNode} from "react";
import {createRoot} from "react-dom/client";
import {
  INCOMPATIBLE_CODES,
  SUPPORTED_IPC_VERSION,
  UNAVAILABLE_CODES,
  WORKSPACE_ID,
  fetchEngineInfo,
  fetchReadmodelSources,
  fetchStatus,
  handshake
} from "./api";
import type {
  DirectorState,
  EngineInfo,
  EnvelopeError,
  ReadmodelSourcesResult,
  ReadmodelState,
  SourceName,
  StateRootState,
  StatusResult,
  SystemState
} from "./api";
import {ConnectionsRoute} from "./connections";
import "./style.css";

const POLL_INTERVAL_MS = 5000;
const STALE_AFTER_MS = 15000;

type RouteId = "connections" | "overview" | "goals" | "workers" | "evidence" | "usage" | "settings";
type Route = {id: RouteId; label: string; group: "engine" | "pending"};

const ROUTES: readonly Route[] = [
  {id: "connections", label: "연결 관리", group: "engine"},
  {id: "overview", label: "총괄", group: "engine"},
  {id: "goals", label: "Goal", group: "pending"},
  {id: "workers", label: "worker", group: "pending"},
  {id: "evidence", label: "근거", group: "pending"},
  {id: "usage", label: "사용량", group: "pending"},
  {id: "settings", label: "설정", group: "engine"}
];
const DEFAULT_ROUTE: RouteId = "overview";

const PENDING_ROUTES: Record<Exclude<RouteId, "overview" | "settings" | "connections">, {title: string; description: string; tasks: string}> = {
  goals: {title: "Goal", description: "Goal과 Task 진행 상태를 보여 주는 화면입니다. VM 연결이 필요합니다.", tasks: "AIK-051 (VM 연결 필요)"},
  workers: {title: "worker", description: "worker 실행 상태를 보여 주는 화면입니다.", tasks: "AIK-052"},
  evidence: {title: "근거", description: "검증 근거와 판정 기록을 보여 주는 화면입니다.", tasks: "AIK-054"},
  usage: {title: "사용량", description: "모델 호출 사용량 관측값을 보여 주는 화면입니다.", tasks: "AIK-055"}
};

type ConnectionState = "connecting" | "connected" | "unavailable" | "incompatible" | "stale";

const CONNECTION_TEXT: Record<ConnectionState, {label: string; detail: string}> = {
  connecting: {label: "연결 중", detail: "엔진에 첫 응답을 요청하고 있습니다."},
  connected: {label: "연결됨", detail: "엔진이 응답하고 있습니다. 5초마다 상태를 다시 확인합니다."},
  unavailable: {label: "엔진 연결 불가", detail: "엔진 프로세스가 실행 중이 아니거나 응답하지 않습니다. 앱이 5초마다 다시 시도합니다."},
  incompatible: {label: "버전 불일치", detail: "앱과 엔진의 버전이 서로 다릅니다. 앱을 다시 설치하거나 업데이트해야 합니다."},
  stale: {label: "오래된 상태", detail: "최신 상태를 확인하지 못했거나 아직 반영되지 않은 새 이벤트가 있습니다. 마지막으로 확인한 내용을 표시합니다."}
};

const SYSTEM_STATE_TEXT: Record<SystemState, string> = {READY: "정상", DEGRADED: "저하", BLOCKED: "차단"};
const DIRECTOR_STATE_TEXT: Record<DirectorState, string> = {
  NOT_CONFIGURED: "구성되지 않음",
  STOPPED: "중지됨",
  RUNNING: "실행 중",
  UNAVAILABLE: "사용 불가"
};
const READMODEL_TEXT: Record<ReadmodelState, string> = {
  NOT_CONFIGURED: "read model이 구성되지 않았습니다.",
  ABSENT: "read model이 아직 만들어지지 않았습니다.",
  READY: "read model이 준비되었습니다.",
  BUSY: "read model을 갱신하는 중입니다. 잠시 뒤 다시 확인합니다.",
  INVALID: "read model이 손상되었거나 형식이 올바르지 않습니다.",
  SCOPE_MISMATCH: "read model이 현재 workspace와 다른 범위로 만들어졌습니다."
};
const STATE_ROOT_TEXT: Record<StateRootState, string> = {
  WRITABLE: "쓰기 가능",
  ABSENT: "없음",
  NOT_WRITABLE: "쓰기 불가",
  UNAVAILABLE: "확인 불가"
};
const SOURCE_TEXT: Record<SourceName, string> = {
  vm: "VM",
  aik_runtime: "AIK 런타임",
  dispatch: "작업 배정",
  lifecycle: "수명주기",
  evidence: "근거",
  ags_peer: "AGS 연동"
};
const PLATFORM_TEXT: Record<EngineInfo["platform_family"], string> = {windows: "Windows", linux: "Linux", macos: "macOS"};

function textOf<K extends string>(table: Record<K, string>, key: string): string {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key as K] : key;
}

type QueryState<T> = {data: T | null; error: EnvelopeError | null; answered: boolean; receivedAt: number | null};
const EMPTY_QUERY = {data: null, error: null, answered: false, receivedAt: null};

type Snapshot = {
  connection: ConnectionState;
  lastSuccessAt: number | null;
  lastError: EnvelopeError | null;
  status: QueryState<StatusResult>;
  engine: QueryState<EngineInfo>;
  sources: QueryState<ReadmodelSourcesResult>;
};

const INITIAL_SNAPSHOT: Snapshot = {
  connection: "connecting",
  lastSuccessAt: null,
  lastError: null,
  status: EMPTY_QUERY,
  engine: EMPTY_QUERY,
  sources: EMPTY_QUERY
};

function severity(error: EnvelopeError): number {
  if (INCOMPATIBLE_CODES.has(error.code)) return 2;
  if (UNAVAILABLE_CODES.has(error.code)) return 1;
  return 0;
}

function worstError(errors: EnvelopeError[]): EnvelopeError | null {
  let worst: EnvelopeError | null = null;
  for (const error of errors) {
    if (worst === null || severity(error) > severity(worst)) worst = error;
  }
  return worst;
}

function failedQuery<T>(previous: QueryState<T>, error: EnvelopeError, now: number): QueryState<T> {
  return {data: previous.data, error, answered: true, receivedAt: now};
}

function connectionAfterFailure(error: EnvelopeError, lastSuccessAt: number | null, now: number): ConnectionState {
  if (INCOMPATIBLE_CODES.has(error.code)) return "incompatible";
  if (lastSuccessAt !== null && now - lastSuccessAt > STALE_AFTER_MS) return "stale";
  return "unavailable";
}

function formatClock(timestamp: number | null): string {
  if (timestamp === null) return "—";
  const date = new Date(timestamp);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

/** 엔진 상태를 5초마다 읽는다. handshake는 성공할 때까지만 다시 시도한다. */
function useEnginePolling(): Snapshot {
  const [snapshot, setSnapshot] = useState<Snapshot>(INITIAL_SNAPSHOT);
  const handshakeOk = useRef(false);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll(): Promise<void> {
      if (!handshakeOk.current) {
        const hello = await handshake();
        if (cancelled) return;
        let error: EnvelopeError | null = null;
        if (!hello.ok) {
          error = hello.error;
        } else if (hello.result.ipc_version !== SUPPORTED_IPC_VERSION) {
          error = {code: "INCOMPATIBLE_PROTOCOL", message: `엔진 IPC 버전 ${String(hello.result.ipc_version)}은 앱이 지원하는 버전 ${SUPPORTED_IPC_VERSION}과 다릅니다.`};
        }
        if (error !== null) {
          const failure = error;
          const now = Date.now();
          setSnapshot((previous) => ({
            connection: connectionAfterFailure(failure, previous.lastSuccessAt, now),
            lastSuccessAt: previous.lastSuccessAt,
            lastError: failure,
            status: failedQuery(previous.status, failure, now),
            engine: failedQuery(previous.engine, failure, now),
            sources: failedQuery(previous.sources, failure, now)
          }));
          return;
        }
        handshakeOk.current = true;
      }

      const [status, engine, sources] = await Promise.all([
        fetchStatus(WORKSPACE_ID),
        fetchEngineInfo(WORKSPACE_ID),
        fetchReadmodelSources(WORKSPACE_ID)
      ]);
      if (cancelled) return;
      const now = Date.now();
      const errors: EnvelopeError[] = [];
      if (!status.ok) errors.push(status.error);
      if (!engine.ok) errors.push(engine.error);
      if (!sources.ok) errors.push(sources.error);
      const failure = worstError(errors);
      if (failure !== null && INCOMPATIBLE_CODES.has(failure.code)) {
        // 버전이 다시 맞는지 handshake부터 확인한다.
        handshakeOk.current = false;
      }

      setSnapshot((previous) => {
        const next: Snapshot = {
          connection: previous.connection,
          lastSuccessAt: previous.lastSuccessAt,
          lastError: failure,
          status: status.ok ? {data: status.result, error: null, answered: true, receivedAt: now} : failedQuery(previous.status, status.error, now),
          engine: engine.ok ? {data: engine.result, error: null, answered: true, receivedAt: now} : failedQuery(previous.engine, engine.error, now),
          sources: sources.ok ? {data: sources.result, error: null, answered: true, receivedAt: now} : failedQuery(previous.sources, sources.error, now)
        };
        if (failure === null) {
          next.lastSuccessAt = now;
          const cursorGap = sources.ok && sources.result.sources.some((row) => row.stale);
          next.connection = cursorGap ? "stale" : "connected";
        } else {
          next.connection = connectionAfterFailure(failure, previous.lastSuccessAt, now);
        }
        return next;
      });
    }

    async function loop(): Promise<void> {
      try {
        await poll();
      } catch {
        // helper는 실패를 던지지 않지만, 예외가 있어도 다음 주기에서 다시 시도한다.
      }
      if (!cancelled) timer = setTimeout(() => void loop(), POLL_INTERVAL_MS);
    }

    void loop();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, []);

  return snapshot;
}

function parseRoute(hash: string): RouteId | null {
  const match = /^#\/([a-z]+)$/.exec(hash);
  if (!match) return null;
  const id = match[1];
  return ROUTES.some((route) => route.id === id) ? (id as RouteId) : null;
}

function useHashRoute(): RouteId {
  const [route, setRoute] = useState<RouteId>(() => parseRoute(window.location.hash) ?? DEFAULT_ROUTE);
  useEffect(() => {
    function sync() {
      const parsed = parseRoute(window.location.hash);
      if (parsed === null) {
        window.location.replace("#/" + DEFAULT_ROUTE);
        setRoute(DEFAULT_ROUTE);
      } else {
        setRoute(parsed);
      }
    }
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  return route;
}

function ConnectionBar({snapshot}: {snapshot: Snapshot}) {
  const text = CONNECTION_TEXT[snapshot.connection];
  const system = snapshot.status.data;
  const showError = snapshot.lastError !== null && snapshot.connection !== "connected";
  return <div id="connection-bar" className="connection-bar" data-connection={snapshot.connection} role="status" aria-live="polite">
    <span className="connection-dot" aria-hidden="true" />
    <div className="connection-text">
      <strong className="connection-label">{text.label}</strong>
      <span className="connection-detail">{text.detail}</span>
      {showError && snapshot.lastError && <code className="connection-code">{snapshot.lastError.code}</code>}
    </div>
    <div className="connection-meta">
      <span className="badge" data-system-state={system?.state ?? "UNKNOWN"}>
        시스템 {system ? `${textOf(SYSTEM_STATE_TEXT, system.state)} (${system.state})` : "확인 전"}
      </span>
      <span className="last-check">마지막 확인 {formatClock(snapshot.lastSuccessAt)}</span>
    </div>
  </div>;
}

function Nav({current}: {current: RouteId}) {
  return <nav className="side-nav" aria-label="주 메뉴">
    <div className="brand"><span className="mark">AIK</span><div>AI-Kubernetes<span className="subtitle">로컬 workspace</span></div></div>
    <ul>
      {ROUTES.map((route) => <li key={route.id}>
        <a id={"nav-" + route.id} href={"#/" + route.id} aria-current={route.id === current ? "page" : undefined} className={route.id === current ? "active" : undefined}>
          <span>{route.label}</span>
          {route.group === "pending" && <span className="nav-tag">준비 중</span>}
        </a>
      </li>)}
    </ul>
    <p className="nav-note">API 키 등 비밀 값은 앱 화면에 입력·표시하지 않습니다.</p>
  </nav>;
}

/** 마지막 응답이 실패했지만 이전에 받은 값이 있을 때 표시한다. */
function StaleNotice({query, lastSuccessAt}: {query: QueryState<unknown>; lastSuccessAt: number | null}) {
  if (query.error === null || query.data === null) return null;
  return <p className="stale-notice" data-stale="1">마지막으로 받은 값입니다 · 마지막 성공 {formatClock(lastSuccessAt)} · 현재 응답 오류 <code>{query.error.code}</code></p>;
}

function ErrorPanel({error}: {error: EnvelopeError}) {
  const incompatible = INCOMPATIBLE_CODES.has(error.code);
  const unavailable = UNAVAILABLE_CODES.has(error.code);
  return <div className="panel error-panel" data-error-code={error.code}>
    <strong>{incompatible ? "버전 불일치" : unavailable ? "엔진 연결 불가" : "조회 실패"}</strong>
    <p>{incompatible ? CONNECTION_TEXT.incompatible.detail : unavailable ? CONNECTION_TEXT.unavailable.detail : "엔진이 요청을 처리하지 못했습니다. 5초 뒤 다시 시도합니다."}</p>
    <code>{error.code}{error.message ? " · " + error.message : ""}</code>
  </div>;
}

function Field({label, value, code}: {label: string; value: ReactNode; code?: string}) {
  return <div className="field">
    <dt>{label}</dt>
    <dd>{value}{code !== undefined && <code>{code}</code>}</dd>
  </div>;
}

function RouteHeader({eyebrow, title, children}: {eyebrow: string; title: string; children?: ReactNode}) {
  return <header className="route-header">
    <span className="eyebrow">{eyebrow}</span>
    <h1>{title}</h1>
    {children}
  </header>;
}

function OverviewRoute({snapshot}: {snapshot: Snapshot}) {
  const {status, engine, sources} = snapshot;
  const readmodel = sources.data;
  const dataState = sources.error !== null ? "ERROR" : readmodel ? readmodel.readmodel : "LOADING";
  return <section id="route-overview" data-state={dataState}>
    <RouteHeader eyebrow="OVERVIEW" title="총괄"><p>엔진 상태와 read model 동기화 현황을 보여 줍니다.</p></RouteHeader>

    <div className="grid two">
      <article className="panel">
        <h2>시스템 상태</h2>
        {status.data ? <dl className="fields">
          <Field label="시스템" value={textOf(SYSTEM_STATE_TEXT, status.data.state)} code={status.data.state} />
          <Field label="director" value={textOf(DIRECTOR_STATE_TEXT, status.data.director_state)} code={status.data.director_state} />
        </dl> : status.error ? <ErrorPanel error={status.error} /> : <p className="muted">응답을 기다리는 중입니다.</p>}
        <StaleNotice query={status} lastSuccessAt={snapshot.lastSuccessAt} />
      </article>
      <article className="panel">
        <h2>엔진 정보</h2>
        {engine.data ? <dl className="fields">
          <Field label="엔진 버전" value={engine.data.engine_version} />
          <Field label="프로토콜" value={"v" + String(engine.data.protocol_version)} />
          <Field label="플랫폼" value={textOf(PLATFORM_TEXT, engine.data.platform_family)} />
          <Field label="런타임" value={engine.data.frozen ? "번들 런타임" : "소스 런타임"} />
        </dl> : engine.error ? <ErrorPanel error={engine.error} /> : <p className="muted">응답을 기다리는 중입니다.</p>}
        <StaleNotice query={engine} lastSuccessAt={snapshot.lastSuccessAt} />
      </article>
    </div>

    <article className="panel">
      <div className="panel-title">
        <h2>read model source</h2>
        {readmodel && <span className="badge" data-readmodel={readmodel.readmodel}>{readmodel.readmodel}</span>}
      </div>
      <p className="note" data-authoritative={readmodel ? String(readmodel.authoritative) : "false"}>
        read model은 화면 표시용 사본이며 권위 원장이 아닙니다 (authoritative=false). 판정과 상태 전이는 VM·AGS·AIK 각 원장이 합니다.
      </p>
      {readmodel ? <>
        {readmodel.readmodel !== "READY" && <div className="panel inline-state" data-readmodel-state={readmodel.readmodel}>
          <strong>{readmodel.readmodel}</strong>
          <p>{textOf(READMODEL_TEXT, readmodel.readmodel)}</p>
        </div>}
        {readmodel.sources.length > 0 ? <table className="sources">
          <thead>
            <tr><th scope="col">source</th><th scope="col">applied / max cursor</th><th scope="col">gaps</th><th scope="col">conflicts</th><th scope="col">stale</th></tr>
          </thead>
          <tbody>
            {readmodel.sources.map((row) => <tr key={row.source} data-source={row.source} data-stale={row.stale ? "1" : "0"}>
              <th scope="row">{textOf(SOURCE_TEXT, row.source)} <code>{row.source}</code></th>
              <td className="num">{row.applied_cursor} / {row.max_seen_cursor}</td>
              <td className="num">{row.open_gaps}</td>
              <td className="num">{row.conflicts}</td>
              <td>{row.stale ? <span className="flag warn">새 이벤트 대기</span> : <span className="flag ok">최신</span>}</td>
            </tr>)}
          </tbody>
        </table> : readmodel.readmodel === "READY" ? <p className="muted">엔진이 보고한 source가 없습니다.</p> : null}
      </> : sources.error ? <ErrorPanel error={sources.error} /> : <p className="muted">응답을 기다리는 중입니다.</p>}
      <StaleNotice query={sources} lastSuccessAt={snapshot.lastSuccessAt} />
    </article>
  </section>;
}

function SettingsRoute({snapshot}: {snapshot: Snapshot}) {
  const {engine, status} = snapshot;
  const info = engine.data;
  const dataState = engine.error !== null ? "ERROR" : info ? (status.data?.state ?? "UNKNOWN") : "LOADING";
  return <section id="route-settings" data-state={dataState}>
    <RouteHeader eyebrow="SETTINGS" title="설정"><p>엔진이 보고한 실행 정보입니다. 이 화면에서는 값을 바꿀 수 없습니다.</p></RouteHeader>
    <article className="panel">
      <h2>엔진 정보 (읽기 전용)</h2>
      {info ? <dl className="fields">
        <Field label="엔진 버전" value={info.engine_version} />
        <Field label="프로토콜 버전" value={String(info.protocol_version)} />
        <Field label="플랫폼" value={textOf(PLATFORM_TEXT, info.platform_family)} code={info.platform_family} />
        <Field label="Python 버전" value={info.python_version} />
        <Field label="런타임" value={info.frozen ? "번들 런타임 (frozen)" : "소스 런타임"} />
        <Field label="상태 저장소" value={textOf(STATE_ROOT_TEXT, info.state_root)} code={info.state_root} />
        <Field label="read model" value={textOf(READMODEL_TEXT, info.readmodel)} code={info.readmodel} />
        <Field label="director" value={textOf(DIRECTOR_STATE_TEXT, info.director_state)} code={info.director_state} />
      </dl> : engine.error ? <ErrorPanel error={engine.error} /> : <p className="muted">응답을 기다리는 중입니다.</p>}
      <StaleNotice query={engine} lastSuccessAt={snapshot.lastSuccessAt} />
      <p className="note">파일 경로는 설계상 화면에 표시하지 않습니다.</p>
    </article>
  </section>;
}

function PendingRoute({id}: {id: keyof typeof PENDING_ROUTES}) {
  const info = PENDING_ROUTES[id];
  return <section id={"route-" + id} data-state="NOT_CONNECTED">
    <RouteHeader eyebrow="NOT CONNECTED" title={info.title} />
    <div className="panel pending-panel" data-state="NOT_CONNECTED">
      <strong>미연결 · 준비 중</strong>
      <p>{info.description} 이 화면은 아직 엔진에 연결되지 않아 표시할 데이터가 없습니다.</p>
      <p>연결 예정 작업: <code>{info.tasks}</code></p>
    </div>
  </section>;
}

function App() {
  const route = useHashRoute();
  const snapshot = useEnginePolling();
  const [connectionsLoaded, setConnectionsLoaded] = useState(false);

  // 연결 화면을 떠나면 다음 방문의 첫 응답까지 data-loaded를 다시 0으로 둔다.
  useEffect(() => {
    if (route !== "connections") setConnectionsLoaded(false);
  }, [route]);

  useEffect(() => {
    document.body.dataset.ready = "1";
  }, []);

  let view: ReactNode;
  let loaded: boolean;
  if (route === "overview") {
    view = <OverviewRoute snapshot={snapshot} />;
    loaded = snapshot.sources.answered;
  } else if (route === "connections") {
    view = <ConnectionsRoute onLoadedChange={setConnectionsLoaded} />;
    loaded = connectionsLoaded;
  } else if (route === "settings") {
    view = <SettingsRoute snapshot={snapshot} />;
    loaded = snapshot.engine.answered;
  } else {
    view = <PendingRoute id={route} />;
    loaded = true;
  }

  return <div className="shell">
    <Nav current={route} />
    <div className="content">
      <ConnectionBar snapshot={snapshot} />
      <main id="route-view" data-route={route} data-loaded={loaded ? "1" : "0"}>
        {view}
      </main>
      <footer><span>비밀 값 비표시 · 원장이 권위</span><span>workspace {WORKSPACE_ID}</span></footer>
    </div>
  </div>;
}

createRoot(document.getElementById("root")!).render(<App />);
