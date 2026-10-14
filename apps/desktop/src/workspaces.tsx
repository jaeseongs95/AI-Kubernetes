// AIK-087 프로젝트 폴더 패널: 현재 선택한 프로젝트 폴더와 등록된 폴더 목록을 보여 주고
// 등록(기존 폴더 연결·새 폴더 만들기)·전환·폴더 다시 선택·보관·복원·연결 사용 범위 조회를 GUI에서 한다.
// 폴더 경로는 main이 여는 OS 폴더 선택 창에서만 고르며 렌더러는 경로를 보내거나 입력받지 않는다.
// 표시하는 경로는 사용자가 고른 프로젝트 폴더뿐이다(엔진 내부 파일 경로는 표시하지 않음).
// data-state: LOADING(첫 응답 전) · NOT_CONFIGURED · UNAVAILABLE · EMPTY · READY
import {useCallback, useEffect, useRef, useState} from "react";
import {
  archiveWorkspace,
  fetchWorkspaceScope,
  fetchWorkspaces,
  requestFolderCreate,
  requestFolderOpen,
  requestFolderRebind,
  restoreWorkspace,
  selectWorkspace
} from "./api";
import type {Envelope, EnvelopeError, FolderResult, Workspace, WorkspaceList, WorkspaceScope} from "./api";
import {isUnavailable} from "./connectionText";
import {CodeTag, NoticeBox} from "./loginFlow";
import type {Notice} from "./loginFlow";
import {
  REBIND_ACTIONS,
  WORKSPACE_ACTION_TEXT,
  WORKSPACE_STATUS_TEXT,
  folderNameProblem,
  projectNameProblem,
  scopeReasonText,
  workspaceErrorText
} from "./workspaceText";

const RETRY_UNAVAILABLE_MS = 5000;
/** 목록을 다시 읽어야 하는 오류: 그 사이 바뀌었거나 사라졌거나 상태가 달라진 프로젝트 폴더. */
const RELOAD_CODES = new Set(["REVISION_CONFLICT", "UNKNOWN_WORKSPACE", "BINDING_MISMATCH", "WORKSPACE_ARCHIVED",
  "WORKSPACE_ROOT_MISSING", "WORKSPACE_ROOT_REPLACED", "WORKSPACE_ROOT_UNSAFE", "WORKSPACE_ACCESS_DENIED",
  "WORKSPACE_UNSUPPORTED_HOST", "WORKSPACE_VM_PROJECT_MISSING", "WORKSPACE_VM_PROJECT_MISMATCH",
  "WORKSPACE_VM_PROJECT_UNKNOWN"]);

export type WorkspacesDataState = "LOADING" | "NOT_CONFIGURED" | "UNAVAILABLE" | "EMPTY" | "READY";

/** App이 한 번 만들어 Nav와 설정 화면 패널이 함께 쓰는 프로젝트 폴더 목록 상태. */
export type WorkspacesStore = {
  list: WorkspaceList | null;
  error: EnvelopeError | null;
  loaded: boolean;
  loading: boolean;
  reload: () => Promise<void>;
};

/** 앱 시작 때와 변경 뒤에만 목록을 읽는다. 엔진에 닿지 못해 목록이 없을 때만 5초마다 다시 읽는다. */
export function useWorkspaces(): WorkspacesStore {
  const [list, setList] = useState<WorkspaceList | null>(null);
  const [error, setError] = useState<EnvelopeError | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const alive = useRef(true);
  const sequence = useRef(0);

  const reload = useCallback(async (): Promise<void> => {
    const ticket = ++sequence.current;
    setLoading(true);
    const answer = await fetchWorkspaces();
    // 늦게 도착한 이전 요청의 답은 버린다.
    if (!alive.current || ticket !== sequence.current) return;
    setLoading(false);
    if (answer.ok) {
      setList(answer.result);
      setError(null);
    } else {
      setError(answer.error);
    }
    setLoaded(true);
  }, []);

  useEffect(() => {
    alive.current = true;
    void reload();
    return () => {
      alive.current = false;
    };
  }, [reload]);

  useEffect(() => {
    if (list !== null || error === null || !isUnavailable(error)) return;
    const timer = setTimeout(() => void reload(), RETRY_UNAVAILABLE_MS);
    return () => clearTimeout(timer);
  }, [list, error, reload]);

  return {list, error, loaded, loading, reload};
}

/** 현재 선택이 등록된 프로젝트 폴더이면 그 이름, 아니면 null. */
export function currentProjectName(list: WorkspaceList | null): string | null {
  const active = list?.active;
  if (!list || !active || !active.registered) return null;
  return list.workspaces.find((workspace) => workspace.workspace_id === active.workspace_id)?.name ?? null;
}

function workspacesDataState(store: WorkspacesStore): WorkspacesDataState {
  if (!store.loaded) return "LOADING";
  if (store.list === null) return store.error?.code === "WORKSPACES_NOT_CONFIGURED" ? "NOT_CONFIGURED" : "UNAVAILABLE";
  return store.list.workspaces.length === 0 ? "EMPTY" : "READY";
}

function failureNotice(prefix: string, error: EnvelopeError): Notice {
  return {tone: "error", text: `${prefix} ${workspaceErrorText(error)}`, code: error.code};
}

function domIdOf(workspaceId: string): string {
  return "ws-" + workspaceId.replace(/[^A-Za-z0-9_-]/g, "-");
}

/** 진행 중인 동작. 한 번에 하나만 실행한다(폴더 선택 창도 한 번에 하나다). */
type Busy = {kind: "open" | "create" | "rebind" | "select" | "archive" | "restore"; workspaceId: string | null} | null;

export function WorkspacesPanel({store}: {store: WorkspacesStore}) {
  const {list, error, loading, reload} = store;
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const alive = useRef(true);

  // 설정 화면을 열 때마다 최신 목록을 읽는다(첫 읽기는 App이 이미 시작했다).
  useEffect(() => {
    alive.current = true;
    if (store.loaded) void reload();
    return () => {
      alive.current = false;
    };
  }, []);

  /** 동작 하나를 실행하고 결과를 알림 영역에 남긴다. 성공하면 목록을 다시 읽는다. */
  async function run<T>(next: Busy, start: Notice | null, invoke: () => Promise<Envelope<T>>,
    failPrefix: string, done: (result: T) => Notice | null): Promise<boolean> {
    setBusy(next);
    setNotice(start);
    const answer = await invoke();
    if (!alive.current) return false;
    setBusy(null);
    if (!answer.ok) {
      setNotice(failureNotice(failPrefix, answer.error));
      if (RELOAD_CODES.has(answer.error.code)) void reload();
      return false;
    }
    const message = done(answer.result);
    setNotice(message);
    void reload();
    return message === null || message.tone !== "info";
  }

  const pickingNotice: Notice = {tone: "info", text: "폴더 선택 창에서 고르는 중… 그 창에서 폴더를 고르거나 취소하세요."};

  function folderDone(verb: string) {
    return (result: FolderResult): Notice => {
      if (result.outcome === "cancelled") return {tone: "info", text: "취소했습니다. 바뀐 것은 없습니다."};
      const workspace = result.workspace;
      const ready = workspace.status === "ready";
      const status = ready ? "" : ` 상태: ${WORKSPACE_STATUS_TEXT[workspace.status]} (${workspace.status}) · ${WORKSPACE_ACTION_TEXT[workspace.required_action]}.`;
      const next = workspace.active ? " 현재 프로젝트로 선택돼 있습니다."
        : ready ? " 쓰려면 목록에서 \"이 폴더로 전환\"을 누르세요." : "";
      return {tone: ready ? "ok" : "warn", text: `"${workspace.name}" 프로젝트 폴더를 ${verb}.${status}${next}`};
    };
  }

  const register = {
    open: (name: string) => run({kind: "open", workspaceId: null}, pickingNotice, () => requestFolderOpen(name),
      "기존 폴더를 연결하지 못했습니다.", folderDone("등록했습니다")),
    create: (name: string, folderName: string) => run({kind: "create", workspaceId: null}, pickingNotice,
      () => requestFolderCreate(name, folderName), "새 폴더를 만들지 못했습니다.", folderDone("만들어 등록했습니다"))
  };

  const actions: RowActions = {
    select: (workspace) => void run({kind: "select", workspaceId: workspace.workspace_id}, null,
      () => selectWorkspace(workspace.workspace_id), `"${workspace.name}"(으)로 전환하지 못했습니다.`,
      () => ({tone: "ok", text: `"${workspace.name}" 프로젝트로 전환했습니다.`})),
    rebind: (workspace) => void run({kind: "rebind", workspaceId: workspace.workspace_id}, pickingNotice,
      () => requestFolderRebind(workspace.workspace_id, workspace.revision), `"${workspace.name}" 폴더를 다시 연결하지 못했습니다.`,
      folderDone("다시 연결했습니다")),
    archive: (workspace) => void run({kind: "archive", workspaceId: workspace.workspace_id}, null,
      () => archiveWorkspace(workspace.workspace_id, workspace.revision), `"${workspace.name}"을(를) 보관하지 못했습니다.`,
      () => ({tone: "ok", text: `"${workspace.name}"을(를) 보관했습니다. 폴더 내용은 그대로이며 복원하면 다시 쓸 수 있습니다.`})),
    restore: (workspace) => void run({kind: "restore", workspaceId: workspace.workspace_id}, null,
      () => restoreWorkspace(workspace.workspace_id, workspace.revision), `"${workspace.name}"을(를) 복원하지 못했습니다.`,
      (result) => ({tone: result.status === "ready" ? "ok" : "warn",
        text: `"${workspace.name}"을(를) 복원했습니다.${result.status === "ready" ? "" : ` 상태: ${WORKSPACE_STATUS_TEXT[result.status]} (${result.status}) · ${WORKSPACE_ACTION_TEXT[result.required_action]}`}`}))
  };

  const dataState = workspacesDataState(store);
  const workspaces = list?.workspaces ?? [];
  const vmMissing = list?.vm === "NOT_CONFIGURED";

  return <article id="workspaces-panel" className="panel workspaces-panel" data-state={dataState}
    data-vm={list?.vm} aria-busy={loading || busy !== null} aria-labelledby="workspaces-title">
    <div className="panel-title">
      <h2 id="workspaces-title">프로젝트 폴더</h2>
      {dataState !== "LOADING" && <button type="button" onClick={() => void reload()} disabled={loading}>
        {loading ? "읽는 중…" : "새로고침"}
      </button>}
    </div>
    <p className="muted small">작업할 프로젝트 폴더를 등록하고 전환합니다. 폴더는 앱이 여는 폴더 선택 창에서만 고르며, 이 화면에 경로를 입력하지 않습니다.</p>

    {dataState === "LOADING" && <p className="muted">엔진에서 프로젝트 폴더 목록을 읽는 중입니다.</p>}

    {dataState === "NOT_CONFIGURED" && <div className="panel inline-state" data-error-code="WORKSPACES_NOT_CONFIGURED">
      <strong>프로젝트 폴더 관리 기능 없음 <CodeTag code="WORKSPACES_NOT_CONFIGURED" /></strong>
      <p>{workspaceErrorText(error)}</p>
      <div className="actions"><button type="button" onClick={() => void reload()} disabled={loading}>다시 확인</button></div>
    </div>}

    {dataState === "UNAVAILABLE" && error && <div className="panel error-panel" data-error-code={error.code}>
      <strong>프로젝트 폴더 목록을 읽지 못했습니다 <CodeTag code={error.code} /></strong>
      <p>{workspaceErrorText(error)} {isUnavailable(error) ? "5초마다 다시 시도합니다." : ""}</p>
      <div className="actions"><button type="button" onClick={() => void reload()} disabled={loading}>다시 시도</button></div>
    </div>}

    {list !== null && error !== null && <p className="stale-notice" data-stale="1">
      마지막으로 받은 목록입니다 · 최근 새로 읽기 실패: {workspaceErrorText(error)} <CodeTag code={error.code} />
    </p>}

    {vmMissing && <div className="panel inline-state vm-notice" data-vm="NOT_CONFIGURED" role="note">
      <strong>VM이 연결되지 않았습니다</strong>
      <p>VM이 없으면 VM project를 만들 수단이 없어 새 프로젝트 폴더를 등록할 수 없습니다. 이미 등록된 폴더도 지금 실행할 수 있는지 확인할 수 없습니다.
        아래 등록 버튼은 그대로 눌러 볼 수 있으며, 엔진의 실제 답(<code>VM_NOT_CONFIGURED</code> 등)을 그대로 보여 줍니다.</p>
    </div>}

    {list !== null && <CurrentProject list={list} />}

    <NoticeBox notice={notice} id="workspaces-notice" />

    {list !== null && <RegisterForm busy={busy} onOpen={register.open} onCreate={register.create} />}

    {dataState === "EMPTY" && <div className="panel pending-panel" data-empty="1">
      <strong>등록된 프로젝트 폴더가 없습니다</strong>
      <p>{list?.registry === "ABSENT" ? "아직 프로젝트 폴더 저장소가 만들어지지 않은 첫 실행 상태입니다." : "모든 프로젝트 폴더가 목록에서 빠졌습니다."} 위에서 프로젝트 이름을 정하고 폴더를 연결하세요.</p>
    </div>}

    {workspaces.length > 0 && <>
      <h3 className="sub-title workspace-list-title" id="workspace-list-title">등록된 프로젝트 폴더 {workspaces.length}개</h3>
      <ul className="workspace-list" aria-labelledby="workspace-list-title">
        {workspaces.map((workspace) => <WorkspaceRow key={workspace.workspace_id} workspace={workspace}
          workspaces={workspaces} busy={busy} actions={actions} />)}
      </ul>
    </>}
  </article>;
}

function CurrentProject({list}: {list: WorkspaceList}) {
  const active = list.active;
  if (active === null) {
    return <div className="current-project" data-current="NONE">
      <h3 className="sub-title">현재 프로젝트</h3>
      <p>선택된 프로젝트 폴더가 없습니다. {list.workspaces.some((workspace) => workspace.state === "active")
        ? "아래 목록에서 \"이 폴더로 전환\"을 누르세요." : "아래에서 프로젝트 폴더를 등록하세요."}</p>
    </div>;
  }
  if (!active.registered) {
    return <div className="current-project" data-current="UNREGISTERED">
      <h3 className="sub-title">현재 프로젝트</h3>
      <p>지금 선택(<code>{active.workspace_id}</code>)은 등록된 프로젝트 폴더가 아닙니다. 그래서 실행 폴더가 없습니다.
        아래 목록에서 등록된 폴더로 전환하거나, 프로젝트 폴더를 새로 등록하세요.</p>
    </div>;
  }
  const workspace = list.workspaces.find((item) => item.workspace_id === active.workspace_id) ?? null;
  const status = active.status;
  return <div className="current-project" data-current="REGISTERED" data-status={status ?? "unknown"}>
    <h3 className="sub-title">현재 프로젝트</h3>
    <dl className="fields">
      <div className="field"><dt>이름</dt><dd>{workspace ? workspace.name : <code>{active.workspace_id}</code>}</dd></div>
      <div className="field"><dt>상태</dt><dd>
        {status ? <><span className="state-text">{WORKSPACE_STATUS_TEXT[status]}</span> <code>{status}</code></> : <span className="muted">확인 불가</span>}
      </dd></div>
      <div className="field wide"><dt>프로젝트 폴더</dt><dd>
        {workspace ? <span className="path">{workspace.root}</span> : <span className="muted">목록에 없음</span>}
      </dd></div>
      <div className="field wide"><dt>실행 폴더(cwd)</dt><dd>
        {active.cwd !== null ? <span className="path">{active.cwd}</span>
          : <span className="muted">없음 — 실행 가능한 상태가 아닙니다</span>}
      </dd></div>
      {active.required_action !== "none" && <div className="field wide"><dt>필요한 조치</dt><dd>
        {WORKSPACE_ACTION_TEXT[active.required_action]} <code>{active.required_action}</code>
      </dd></div>}
    </dl>
  </div>;
}

function RegisterForm({busy, onOpen, onCreate}: {
  busy: Busy;
  onOpen: (name: string) => Promise<boolean>;
  onCreate: (name: string, folderName: string) => Promise<boolean>;
}) {
  const [name, setName] = useState("");
  const [folderName, setFolderName] = useState("");
  const [nameIssue, setNameIssue] = useState<string | null>(null);
  const [folderIssue, setFolderIssue] = useState<string | null>(null);
  const picking = busy !== null && (busy.kind === "open" || busy.kind === "create");

  async function open() {
    const issue = projectNameProblem(name);
    setNameIssue(issue);
    setFolderIssue(null);
    if (issue !== null) return;
    if (await onOpen(name)) setName("");
  }

  async function create() {
    const issue = projectNameProblem(name);
    const folder = folderNameProblem(folderName);
    setNameIssue(issue);
    setFolderIssue(folder);
    if (issue !== null || folder !== null) return;
    if (await onCreate(name, folderName)) {
      setName("");
      setFolderName("");
    }
  }

  return <form className="sub-panel register-form" aria-labelledby="workspace-register-title" data-picking={picking ? "1" : "0"}
    onSubmit={(event) => event.preventDefault()}>
    <h3 className="sub-title" id="workspace-register-title">프로젝트 폴더 등록</h3>
    <label htmlFor="workspace-name" className="field-label">프로젝트 이름 (필수)</label>
    <input id="workspace-name" type="text" value={name} maxLength={128} autoComplete="off" spellCheck={false}
      aria-required="true" aria-invalid={nameIssue !== null} aria-describedby={nameIssue ? "workspace-name-error" : undefined}
      onChange={(event) => setName(event.target.value)} />
    {nameIssue && <p id="workspace-name-error" className="field-error">{nameIssue}</p>}

    <div className="register-option">
      <button type="button" className="primary" onClick={() => void open()} disabled={busy !== null}>
        {busy?.kind === "open" ? "폴더 선택 창에서 고르는 중…" : "기존 폴더 연결…"}
      </button>
      <p className="muted small">이미 있는 폴더를 고르면 그 폴더를 이 이름의 프로젝트로 등록합니다.</p>
    </div>

    <div className="register-option">
      <label htmlFor="workspace-folder-name" className="field-label">새 폴더 이름 (새 폴더 만들기에만 필요)</label>
      <input id="workspace-folder-name" type="text" value={folderName} maxLength={255} autoComplete="off" spellCheck={false}
        aria-invalid={folderIssue !== null} aria-describedby={"workspace-folder-hint" + (folderIssue ? " workspace-folder-error" : "")}
        onChange={(event) => setFolderName(event.target.value)} />
      <p id="workspace-folder-hint" className="muted small">/ \ : * ? " &lt; &gt; | 는 쓸 수 없고, 공백이나 마침표로 끝날 수 없습니다.</p>
      {folderIssue && <p id="workspace-folder-error" className="field-error">{folderIssue}</p>}
      <div className="actions">
        <button type="button" onClick={() => void create()} disabled={busy !== null}>
          {busy?.kind === "create" ? "폴더 선택 창에서 고르는 중…" : "새 폴더 만들기…"}
        </button>
      </div>
      <p className="muted small">폴더 선택 창에서 새 폴더를 만들 위치를 고르면, 그 안에 이 이름의 폴더를 만들어 등록합니다.</p>
    </div>
  </form>;
}

type RowActions = {
  select: (workspace: Workspace) => void;
  rebind: (workspace: Workspace) => void;
  archive: (workspace: Workspace) => void;
  restore: (workspace: Workspace) => void;
};

type ScopeView = {loading: boolean; data: WorkspaceScope | null; error: EnvelopeError | null};

function WorkspaceRow({workspace, workspaces, busy, actions}: {
  workspace: Workspace;
  workspaces: Workspace[];
  busy: Busy;
  actions: RowActions;
}) {
  const [scope, setScope] = useState<ScopeView | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const id = workspace.workspace_id;
  const domId = domIdOf(id);
  const archived = workspace.state === "archived";
  const ready = workspace.status === "ready";
  const mine = busy !== null && busy.workspaceId === id ? busy.kind : null;
  const disabled = busy !== null;

  async function loadScope() {
    setScope({loading: true, data: scope?.data ?? null, error: null});
    const answer = await fetchWorkspaceScope(id);
    if (!alive.current) return;
    setScope(answer.ok ? {loading: false, data: answer.result, error: null} : {loading: false, data: null, error: answer.error});
  }

  function toggleScope() {
    if (scope !== null) setScope(null);
    else void loadScope();
  }

  const ownerName = (ownerId: string) => workspaces.find((item) => item.workspace_id === ownerId)?.name ?? null;

  return <li className="panel workspace-card" id={domId} data-workspace-id={id} data-state={workspace.state}
    data-status={workspace.status} data-active={workspace.active ? "1" : "0"} aria-labelledby={domId + "-title"}>
    <div className="card-head">
      <div>
        <h4 id={domId + "-title"} className="workspace-name">{workspace.name}</h4>
        <p className="path">{workspace.root}</p>
      </div>
      <div className="card-badges">
        {workspace.active && <span className="badge" data-current="1">현재 프로젝트</span>}
        <span className="badge" data-workspace-status={workspace.status}>{WORKSPACE_STATUS_TEXT[workspace.status]}</span>
      </div>
    </div>

    <dl className="fields">
      <div className="field"><dt>상태</dt><dd>
        <span className="state-text">{WORKSPACE_STATUS_TEXT[workspace.status]}</span> <code>{workspace.status}</code>
      </dd></div>
      <div className="field"><dt>필요한 조치</dt><dd>
        {WORKSPACE_ACTION_TEXT[workspace.required_action]} <code>{workspace.required_action}</code>
      </dd></div>
    </dl>

    <div className="actions card-actions" role="group" aria-label={`${workspace.name} 프로젝트 폴더 동작`}>
      {!archived && !workspace.active && <button type="button" onClick={() => actions.select(workspace)}
        disabled={disabled || !ready} aria-label={`${workspace.name} 프로젝트로 전환`}
        aria-describedby={!ready ? domId + "-select-reason" : undefined}>
        {mine === "select" ? "전환 중…" : "이 폴더로 전환"}
      </button>}
      {REBIND_ACTIONS.has(workspace.required_action) && <button type="button" onClick={() => actions.rebind(workspace)}
        disabled={disabled} aria-label={`${workspace.name} 폴더 다시 선택`}>
        {mine === "rebind" ? "폴더 선택 창에서 고르는 중…" : "폴더 다시 선택…"}
      </button>}
      {!archived && !workspace.active && <button type="button" onClick={() => actions.archive(workspace)}
        disabled={disabled} aria-label={`${workspace.name} 보관`}>
        {mine === "archive" ? "보관 중…" : "보관"}
      </button>}
      {archived && <button type="button" onClick={() => actions.restore(workspace)}
        disabled={disabled} aria-label={`${workspace.name} 복원`}>
        {mine === "restore" ? "복원 중…" : "복원"}
      </button>}
      <button type="button" className="ghost" onClick={toggleScope} aria-expanded={scope !== null}
        aria-controls={domId + "-scope"} aria-label={`${workspace.name} 연결 사용 범위 ${scope !== null ? "닫기" : "보기"}`}>
        연결 사용 범위
      </button>
    </div>
    {!archived && !workspace.active && !ready && <p id={domId + "-select-reason"} className="muted small">
      지금은 전환할 수 없습니다: {WORKSPACE_STATUS_TEXT[workspace.status]} — {WORKSPACE_ACTION_TEXT[workspace.required_action]}.
    </p>}
    {workspace.active && <p className="muted small">현재 선택한 프로젝트는 보관할 수 없습니다. 다른 폴더로 전환한 뒤 보관하세요.</p>}

    {scope !== null && <div className="sub-panel" id={domId + "-scope"} data-scope={scope.data?.scope ?? (scope.error ? "ERROR" : "LOADING")}>
      <h5 className="sub-title">연결 사용 범위</h5>
      {scope.loading && <p className="muted">사용 범위를 읽는 중입니다.</p>}
      {scope.error && <div className="inline-error">
        <p>사용 범위를 읽지 못했습니다. {workspaceErrorText(scope.error)} <CodeTag code={scope.error.code} /></p>
        <div><button type="button" onClick={() => void loadScope()}>다시 읽기</button></div>
      </div>}
      {scope.data && <>
        {scope.data.scope === "ABSENT" && <p className="note">이 프로젝트에는 저장된 연결 사용 범위가 없어 모든 연결이 거부됩니다. 사용 범위 편집은 아직 이 화면에서 할 수 없습니다(AIK-088 예정).</p>}
        {scope.data.connections.length === 0 ? <p className="muted">등록된 연결이 없습니다.</p> : <table className="sources scope-table">
          <thead>
            <tr><th scope="col">연결</th><th scope="col">벤더</th><th scope="col">소유 범위</th><th scope="col">사용</th></tr>
          </thead>
          <tbody>
            {scope.data.connections.map((row) => {
              const owner = ownerName(row.owner_workspace_id);
              return <tr key={row.connection_id} data-allowed={row.allowed ? "1" : "0"}>
                <th scope="row"><code>{row.connection_id}</code></th>
                <td><code>{row.provider}</code></td>
                <td>{owner !== null ? <>{owner} </> : null}<code>{row.owner_workspace_id}</code></td>
                <td>{row.allowed ? <span className="flag ok">허용</span>
                  : <><span className="flag warn">거부</span> {scopeReasonText(row.reason)} <CodeTag code={row.reason} /></>}</td>
              </tr>;
            })}
          </tbody>
        </table>}
      </>}
    </div>}
  </li>;
}
