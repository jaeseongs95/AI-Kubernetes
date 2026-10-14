// AIK-046 첫 실행 온보딩: 커넥터 선택 → 계정 별칭 → 인증(키 입력 창 또는 공식 로그인) → 결과.
// API 키는 이 화면에 입력하지 않는다. main이 소유한 별도 키 입력 창을 열고 비밀 없는 결과만 받는다.
import {useEffect, useRef, useState} from "react";
import {checkConnection, fetchConnectionCatalog, requestKeyAdd, requestKeyRotate} from "./api";
import type {CheckSnapshot, Connection, Connector, EnvelopeError, ExecutionKind} from "./api";
import {CHECK_STATE_TEXT, EXECUTION_TEXT, checkCodeText, errorText, labelProblem} from "./connectionText";
import {CodeTag, LoginFlow, NoticeBox, OtherLoginControl, errorNotice} from "./loginFlow";
import type {LoginEnd, Notice} from "./loginFlow";

type Step = 1 | 2 | 3 | 4;
type Outcome =
  | {kind: "saved"; connection: Connection; check: CheckSnapshot}
  | {kind: "cancelled"; via: ExecutionKind}
  | {kind: "failed"; via: ExecutionKind; error: EnvelopeError | null; code: string | null};

const STEPS: readonly {step: Step; label: string}[] = [
  {step: 1, label: "커넥터 선택"},
  {step: 2, label: "계정 별칭"},
  {step: 3, label: "인증"},
  {step: 4, label: "결과"}
];

const KIND_HINT: Record<ExecutionKind, string> = {
  api: "API 키를 앱과 분리된 보안 입력 창에만 입력합니다.",
  subscription: "벤더의 공식 로그인으로 구독 계정을 연결합니다."
};

const LABEL_STEP_CODES = new Set(["DUPLICATE_CONNECTION", "INVALID_LABEL"]);
const CONNECTOR_STEP_CODES = new Set(["UNSUPPORTED_PROVIDER", "WRONG_METHOD"]);

function connectorKey(connector: Connector): string {
  return connector.provider + ":" + connector.execution_kind;
}

function groupByVendor(connectors: Connector[]): {provider: string; name: string; items: Connector[]}[] {
  const groups = new Map<string, {provider: string; name: string; items: Connector[]}>();
  for (const connector of connectors) {
    const group = groups.get(connector.provider) ?? {provider: connector.provider, name: connector.provider, items: []};
    group.items.push(connector);
    groups.set(connector.provider, group);
  }
  for (const group of groups.values()) group.items.sort((a, b) => (a.execution_kind === b.execution_kind ? 0 : a.execution_kind === "api" ? -1 : 1));
  return [...groups.values()];
}

export function Onboarding({onChanged, onClose}: {onChanged: () => void; onClose?: () => void}) {
  const [step, setStep] = useState<Step>(1);
  const [catalog, setCatalog] = useState<Connector[] | null>(null);
  const [catalogError, setCatalogError] = useState<EnvelopeError | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [label, setLabel] = useState("");
  const [labelError, setLabelError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [busy, setBusy] = useState<"key" | "check" | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [loginAttempt, setLoginAttempt] = useState(0);
  const [reauthId, setReauthId] = useState<string | null>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const firstRender = useRef(true);

  async function loadCatalog() {
    setCatalogError(null);
    const answer = await fetchConnectionCatalog();
    if (answer.ok) setCatalog(answer.result.connectors);
    else setCatalogError(answer.error);
  }

  useEffect(() => {
    void loadCatalog();
  }, []);

  // 단계가 바뀌면 새 단계 제목으로 초점을 옮긴다(키보드·스크린 리더 사용자).
  useEffect(() => {
    if (firstRender.current) {
      firstRender.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [step]);

  const connector = catalog?.find((item) => connectorKey(item) === selected) ?? null;

  function goTo(next: Step) {
    setNotice(null);
    if (next < 4) setOutcome(null);
    if (next === 3) setLoginAttempt((value) => value + 1);
    setStep(next);
  }

  function restart() {
    setSelected("");
    setLabel("");
    setLabelError(null);
    setReauthId(null);
    goTo(1);
  }

  function finish(result: Outcome) {
    setOutcome(result);
    if (result.kind === "saved") {
      setReauthId(null);
      onChanged();
    }
    setStep(4);
  }

  async function openKeyWindow() {
    if (!connector) return;
    setBusy("key");
    setNotice({tone: "info", text: "키 입력 창이 열렸습니다. 그 창에서 키를 저장하거나 취소하세요."});
    const answer = await requestKeyAdd(connector.provider, label);
    setBusy(null);
    setNotice(null);
    if (!answer.ok) finish({kind: "failed", via: "api", error: answer.error, code: answer.error.code});
    else if (answer.result.outcome === "saved") finish({kind: "saved", connection: answer.result.connection, check: answer.result.check});
    else finish({kind: "cancelled", via: "api"});
  }

  function loginEnded(end: LoginEnd) {
    if (end.kind === "cancelled") finish({kind: "cancelled", via: "subscription"});
    else finish({kind: "failed", via: "subscription", error: end.error, code: end.code});
  }

  async function recheck(connection: Connection) {
    setBusy("check");
    setNotice(null);
    const answer = await checkConnection(connection.connection_id);
    setBusy(null);
    if (answer.ok) {
      setOutcome({kind: "saved", connection: answer.result.connection, check: answer.result.check});
      onChanged();
    } else {
      setNotice(errorNotice(answer.error));
    }
  }

  async function reenterKey(connection: Connection) {
    setBusy("key");
    setNotice({tone: "info", text: "키 입력 창이 열렸습니다. 그 창에서 새 키를 저장하거나 취소하세요."});
    const answer = await requestKeyRotate(connection.connection_id, connection.revision);
    setBusy(null);
    if (!answer.ok) {
      setNotice(errorNotice(answer.error));
      if (answer.error.code === "REVISION_CONFLICT") onChanged();
    } else if (answer.result.outcome === "saved") {
      setNotice(null);
      setOutcome({kind: "saved", connection: answer.result.connection, check: answer.result.check});
      onChanged();
    } else {
      setNotice({tone: "info", text: "키 입력을 취소했습니다. 기존에 저장한 키는 그대로입니다."});
    }
  }

  function submitLabel() {
    const problem = labelProblem(label);
    setLabelError(problem);
    if (problem === null) goTo(3);
  }

  const groups = catalog ? groupByVendor(catalog) : [];
  return <section id="onboarding" className="panel onboarding" data-step={step} data-outcome={outcome?.kind ?? ""} aria-labelledby="onboarding-title">
    <div className="panel-title">
      <h2 id="onboarding-title">연결 추가</h2>
      {onClose && <button type="button" className="ghost" onClick={onClose}>닫기</button>}
    </div>
    <ol className="stepper" aria-label="연결 추가 단계">
      {STEPS.map((item) => <li key={item.step} aria-current={item.step === step ? "step" : undefined}
        data-done={item.step < step ? "1" : "0"} className={item.step === step ? "current" : item.step < step ? "done" : undefined}>
        <span className="step-no" aria-hidden="true">{item.step}</span>
        <span>{item.label}</span>
        {item.step < step && <span className="sr-only"> (완료)</span>}
      </li>)}
    </ol>

    {step === 1 && <form className="step-body" onSubmit={(event) => {
      event.preventDefault();
      if (connector) goTo(2);
    }}>
      <h3 ref={headingRef} tabIndex={-1}>1. 벤더와 실행 경로 고르기</h3>
      {catalog === null && catalogError === null && <p className="muted">엔진에서 커넥터 목록을 읽는 중입니다.</p>}
      {catalogError && <div className="inline-error">
        <p>{errorText(catalogError)} <CodeTag code={catalogError.code} /></p>
        <button type="button" onClick={() => void loadCatalog()}>다시 읽기</button>
      </div>}
      {catalog !== null && catalog.length === 0 && <p className="muted">이 엔진이 제공하는 커넥터가 없습니다. 설치·개발 설정을 확인하세요.</p>}
      {groups.map((group) => <fieldset key={group.provider} className="vendor-group">
        <legend>벤더 <span className="provider-id">{group.name}</span></legend>
        {group.items.map((item) => {
          const id = "connector-" + connectorKey(item).replace(/[^A-Za-z0-9_-]/g, "-");
          return <label key={connectorKey(item)} htmlFor={id} className="choice">
            <input type="radio" id={id} name="aik-connector" value={connectorKey(item)} checked={selected === connectorKey(item)}
              onChange={() => setSelected(connectorKey(item))} />
            <span className="choice-text">
              <strong>{EXECUTION_TEXT[item.execution_kind]}</strong>
              {item.display_name !== group.name && <span> · {item.display_name}</span>}
              <span className="muted choice-hint">{KIND_HINT[item.execution_kind]}</span>
            </span>
          </label>;
        })}
      </fieldset>)}
      <div className="actions">
        <button type="submit" className="primary" disabled={!connector}>다음</button>
      </div>
    </form>}

    {step === 2 && connector && <form className="step-body" onSubmit={(event) => {
      event.preventDefault();
      submitLabel();
    }}>
      <h3 ref={headingRef} tabIndex={-1}>2. 계정 별칭 정하기</h3>
      <p>{connector.display_name} · {EXECUTION_TEXT[connector.execution_kind]}. 여러 계정을 구분할 이름을 정하세요. 비밀 값은 넣지 마세요.</p>
      <label htmlFor="onboarding-label" className="field-label">계정 별칭</label>
      <input id="onboarding-label" type="text" value={label} maxLength={128} autoComplete="off" spellCheck={false}
        aria-invalid={labelError !== null} aria-describedby={labelError ? "onboarding-label-error" : undefined}
        onChange={(event) => setLabel(event.target.value)} />
      {labelError && <p id="onboarding-label-error" className="field-error">{labelError}</p>}
      <div className="actions">
        <button type="button" onClick={() => goTo(1)}>이전</button>
        <button type="submit" className="primary">다음</button>
      </div>
    </form>}

    {step === 3 && connector && <div className="step-body">
      <h3 ref={headingRef} tabIndex={-1}>3. {connector.execution_kind === "api" ? "API 키 입력" : "공식 로그인"}</h3>
      <dl className="fields compact">
        <div className="field"><dt>벤더</dt><dd>{connector.display_name}</dd></div>
        <div className="field"><dt>인증 방식</dt><dd>{EXECUTION_TEXT[connector.execution_kind]}</dd></div>
        <div className="field"><dt>계정 별칭</dt><dd>{label}</dd></div>
      </dl>
      {connector.execution_kind === "api" ? <>
        <p className="note">키는 이 화면이 아니라 앱이 따로 여는 보안 입력 창에만 입력합니다. 이 화면은 키를 받거나 보관하지 않으며, 저장한 뒤에도 키를 다시 보여 주지 않습니다.</p>
        <div className="actions">
          <button type="button" onClick={() => goTo(2)} disabled={busy !== null}>이전</button>
          <button type="button" className="primary" onClick={() => void openKeyWindow()} disabled={busy !== null}>
            {busy === "key" ? "키 입력 창 열림…" : "키 입력 창 열기"}
          </button>
        </div>
      </> : <>
        <LoginFlow key={loginAttempt}
          target={reauthId ? {kind: "reauth", connectionId: reauthId, accountLabel: label} : {kind: "new", provider: connector.provider, accountLabel: label}}
          onDone={(result) => finish({kind: "saved", connection: result.connection, check: result.check})}
          onEnded={loginEnded} />
        <div className="actions">
          <button type="button" onClick={() => goTo(2)}>이전</button>
        </div>
      </>}
      <NoticeBox notice={notice} />
    </div>}

    {step === 4 && outcome && <div className="step-body" data-result={outcome.kind}>
      <h3 ref={headingRef} tabIndex={-1}>4. 결과</h3>
      {outcome.kind === "saved" && <SavedResult outcome={outcome} busy={busy}
        onRecheck={() => void recheck(outcome.connection)}
        onReauth={() => {
          if (outcome.connection.execution_kind === "api") void reenterKey(outcome.connection);
          else {
            setReauthId(outcome.connection.connection_id);
            goTo(3);
          }
        }} />}
      {outcome.kind === "cancelled" && <div className="result-box warn">
        <strong>{outcome.via === "api" ? "키 입력을 취소했습니다" : "공식 로그인이 취소됐습니다"}</strong>
        <p>저장된 연결은 없습니다. 같은 단계부터 다시 시도할 수 있습니다.</p>
      </div>}
      {outcome.kind === "failed" && <div className="result-box error">
        <strong>{outcome.via === "api" ? "키 등록을 마치지 못했습니다" : "공식 로그인을 마치지 못했습니다"}</strong>
        <p>{outcome.error ? errorText(outcome.error) : checkCodeText(outcome.code) ?? "공식 로그인이 실패했습니다. 다시 시도하세요."} <CodeTag code={outcome.code} /></p>
        {outcome.code === "LOGIN_IN_PROGRESS" && <OtherLoginControl />}
      </div>}
      {outcome.kind !== "saved" && <div className="actions">
        <button type="button" className={!LABEL_STEP_CODES.has(outcome.kind === "failed" ? outcome.code ?? "" : "") &&
          !CONNECTOR_STEP_CODES.has(outcome.kind === "failed" ? outcome.code ?? "" : "") ? "primary" : undefined}
          onClick={() => goTo(3)}>{outcome.via === "api" ? "키 입력 다시 시도" : "로그인 다시 시도"}</button>
        <button type="button" className={outcome.kind === "failed" && LABEL_STEP_CODES.has(outcome.code ?? "") ? "primary" : undefined}
          onClick={() => goTo(2)}>별칭 다시 정하기</button>
        <button type="button" className={outcome.kind === "failed" && CONNECTOR_STEP_CODES.has(outcome.code ?? "") ? "primary" : undefined}
          onClick={() => goTo(1)}>커넥터 다시 고르기</button>
      </div>}
      {outcome.kind === "saved" && <div className="actions">
        {onClose && <button type="button" onClick={onClose}>연결 목록 보기</button>}
        <button type="button" onClick={restart}>다른 연결 추가</button>
      </div>}
      <NoticeBox notice={notice} />
    </div>}
  </section>;
}

function SavedResult({outcome, busy, onRecheck, onReauth}: {
  outcome: {connection: Connection; check: CheckSnapshot};
  busy: "key" | "check" | null;
  onRecheck: () => void;
  onReauth: () => void;
}) {
  const {connection, check} = outcome;
  const verified = check.state === "verified";
  return <>
    <div className={"result-box " + (verified ? "ok" : "warn")} data-check-state={check.state}>
      <strong>{verified ? "연결을 확인했습니다" : "저장됐지만 확인되지 않음"}</strong>
      <p>
        {connection.account_label} · {connection.display_name} · {EXECUTION_TEXT[connection.execution_kind]} — 검사 결과{" "}
        <span className="state-text">{CHECK_STATE_TEXT[check.state]}</span> <CodeTag code={check.code} />
      </p>
      {!verified && <p>{checkCodeText(check.code) ?? "연결은 저장됐지만 벤더 확인을 통과하지 못했습니다."} 다시 검사하거나 인증을 다시 하세요.</p>}
      {!verified && <div className="actions">
        <button type="button" className="primary" onClick={onRecheck} disabled={busy !== null}>{busy === "check" ? "검사 중…" : "다시 검사"}</button>
        <button type="button" onClick={onReauth} disabled={busy !== null}>
          {connection.execution_kind === "api" ? (busy === "key" ? "키 입력 창 열림…" : "키 다시 입력") : "공식 로그인 다시 하기"}
        </button>
      </div>}
    </div>
    <div className="next-step" data-next-step="director-model" data-available="0">
      <span className="eyebrow">다음 단계</span>
      <h4>총괄 모델 설정 <span className="nav-tag">AIK-049 · 준비 중</span></h4>
      <p id="next-step-note">{verified
        ? "연결이 확인됐습니다. 다음은 이 연결로 총괄(director) 모델을 고르는 단계입니다. 이 화면(AIK-049)은 아직 만들어지지 않아 지금은 진행할 수 없습니다."
        : "연결이 확인된 뒤 총괄(director) 모델을 고르는 단계로 넘어갑니다. 이 화면(AIK-049)은 아직 만들어지지 않았습니다."}</p>
      <button type="button" disabled aria-describedby="next-step-note">총괄 모델 설정으로 이동 (준비 중)</button>
    </div>
  </>;
}
