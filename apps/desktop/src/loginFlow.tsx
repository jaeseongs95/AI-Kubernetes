// AIK-046·047 공식 로그인(구독) 흐름과 연결 화면 공용 표시 조각.
// 로그인 상태는 엔진이 소유한다. 화면은 시작 → 1.5초 간격 관찰 → 사용자가 "로그인 완료 확인"을
// 누르면 completeLogin 순서로만 진행하고, 끝나지 않은 로그인을 성공으로 꾸미지 않는다.
import {useEffect, useRef, useState} from "react";
import {cancelLogin, completeLogin, fetchLoginState, startNewLogin, startReauthentication} from "./api";
import type {ConnectionWithCheck, EnvelopeError, LoginState} from "./api";
import {LOGIN_STATE_TEXT, checkCodeText, errorText, safeCode} from "./connectionText";

const LOGIN_POLL_MS = 1500;
/** completeLogin이 이 code로 실패하면 로그인을 마친 뒤 같은 자리에서 다시 확인할 수 있다. */
const COMPLETE_RETRY_CODES = new Set(["LOGIN_PENDING", "NOT_SIGNED_IN", "ACCOUNT_CHANGED"]);

export type NoticeTone = "ok" | "warn" | "error" | "info";
export type Notice = {tone: NoticeTone; text: string; code?: string | null};

export function CodeTag({code}: {code: string | null | undefined}) {
  const safe = safeCode(code);
  return safe === null ? null : <code className="code-tag">{safe}</code>;
}

export function NoticeBox({notice, id}: {notice: Notice | null; id?: string}) {
  return <div id={id} className="notice-slot" role="status" aria-live="polite">
    {notice && <p className={"notice " + notice.tone} data-tone={notice.tone}>
      <span>{notice.text}</span> <CodeTag code={notice.code} />
    </p>}
  </div>;
}

export function errorNotice(error: EnvelopeError): Notice {
  return {tone: "error", text: errorText(error), code: error.code};
}

export type LoginTarget =
  | {kind: "new"; provider: string; accountLabel: string}
  | {kind: "reauth"; connectionId: string; accountLabel: string};

export type LoginEnd =
  | {kind: "cancelled"}
  | {kind: "failed"; error: EnvelopeError | null; code: string | null};

type Busy = "start" | "complete" | "cancel" | null;

export function LoginFlow({target, onDone, onEnded, autoStart = false}: {
  target: LoginTarget;
  onDone: (result: ConnectionWithCheck) => void;
  onEnded: (end: LoginEnd) => void;
  autoStart?: boolean;
}) {
  const [login, setLogin] = useState<LoginState | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const started = useRef(false);
  const ended = useRef(false);
  const callbacks = useRef({onDone, onEnded});
  callbacks.current = {onDone, onEnded};

  function end(result: LoginEnd) {
    if (ended.current) return;
    ended.current = true;
    callbacks.current.onEnded(result);
  }

  function observe(state: LoginState) {
    setLogin(state);
    if (state.state === "cancelled") end({kind: "cancelled"});
    else if (state.state === "failed") end({kind: "failed", error: null, code: state.code});
    else if (state.state === "idle") end({kind: "failed", error: {code: "NO_LOGIN", message: ""}, code: "NO_LOGIN"});
  }

  async function start() {
    started.current = true;
    setBusy("start");
    setNotice(null);
    const answer = target.kind === "new" ? await startNewLogin(target.provider, target.accountLabel)
      : await startReauthentication(target.connectionId);
    setBusy(null);
    if (answer.ok) observe(answer.result);
    else end({kind: "failed", error: answer.error, code: answer.error.code});
  }

  useEffect(() => {
    if (autoStart && !started.current) void start();
    // 처음 한 번만 자동 시작한다.
  }, []);

  // 진행 중인 동안 엔진이 보고하는 로그인 상태를 1.5초마다 다시 읽는다.
  useEffect(() => {
    if (login?.state !== "pending") return;
    let cancelled = false;
    const timer = setTimeout(async () => {
      const answer = await fetchLoginState();
      if (cancelled || ended.current) return;
      if (answer.ok) observe(answer.result);
      else setNotice(errorNotice(answer.error));
    }, LOGIN_POLL_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [login]);

  async function complete() {
    setBusy("complete");
    setNotice(null);
    const answer = await completeLogin();
    setBusy(null);
    if (answer.ok) {
      ended.current = true;
      callbacks.current.onDone(answer.result);
    } else if (COMPLETE_RETRY_CODES.has(answer.error.code)) {
      setNotice({tone: "warn", text: errorText(answer.error), code: answer.error.code});
    } else {
      end({kind: "failed", error: answer.error, code: answer.error.code});
    }
  }

  async function cancel() {
    setBusy("cancel");
    const answer = await cancelLogin();
    setBusy(null);
    if (!answer.ok) {
      setNotice(errorNotice(answer.error));
      return;
    }
    if (answer.result.cancelled) end({kind: "cancelled"});
    else {
      setLogin(answer.result.login);
      setNotice({tone: "warn", text: "로그인을 취소하지 못했습니다. 공식 로그인 창을 직접 닫거나 마치세요.", code: answer.result.login.code});
    }
  }

  const loginCode = login ? safeCode(login.code) : null;
  return <div className="login-flow" data-login-state={login?.state ?? "not_started"}>
    {login === null ? <>
      <p>{target.kind === "new"
        ? "벤더의 공식 로그인을 시작합니다. 공식 호스트가 로그인 창이나 브라우저를 엽니다. 이 앱은 비밀번호나 토큰을 받지 않습니다."
        : `"${target.accountLabel}" 연결의 공식 로그인을 다시 시작합니다. 등록한 것과 같은 계정으로 로그인하세요.`}</p>
      <div className="actions">
        <button type="button" className="primary" onClick={() => void start()} disabled={busy !== null}>
          {busy === "start" ? "로그인 시작 중…" : target.kind === "new" ? "공식 로그인 시작" : "재인증 시작"}
        </button>
      </div>
    </> : <>
      <p className="login-status">
        <span className="state-text">{LOGIN_STATE_TEXT[login.state]}</span> <CodeTag code={login.code} />
        {loginCode !== null && checkCodeText(loginCode) && <span className="muted"> · {checkCodeText(loginCode)}</span>}
      </p>
      <p>공식 로그인 창에서 로그인을 마친 뒤 <strong>로그인 완료 확인</strong>을 누르세요. 진행 상태는 1.5초마다 다시 읽습니다.</p>
      <div className="actions">
        <button type="button" className="primary" onClick={() => void complete()} disabled={busy !== null}>
          {busy === "complete" ? "확인 중…" : "로그인 완료 확인"}
        </button>
        {login.state === "pending" && login.cancel_supported &&
          <button type="button" onClick={() => void cancel()} disabled={busy !== null}>{busy === "cancel" ? "취소 중…" : "로그인 취소"}</button>}
      </div>
      {login.state === "pending" && !login.cancel_supported &&
        <p className="muted">이 공식 로그인은 앱에서 취소할 수 없습니다. 그만두려면 공식 로그인 창을 닫으세요.</p>}
    </>}
    <NoticeBox notice={notice} />
  </div>;
}

/** LOGIN_IN_PROGRESS일 때 진행 중인 다른 로그인을 보여 주고, 지원되면 취소할 수 있게 한다. */
export function OtherLoginControl({onCleared}: {onCleared?: () => void}) {
  const [login, setLogin] = useState<LoginState | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    void fetchLoginState().then((answer) => {
      if (!alive) return;
      if (answer.ok) setLogin(answer.result);
      else setNotice(errorNotice(answer.error));
    });
    return () => {
      alive = false;
    };
  }, []);

  async function cancel() {
    setBusy(true);
    const answer = await cancelLogin();
    setBusy(false);
    if (!answer.ok) {
      setNotice(errorNotice(answer.error));
      return;
    }
    setLogin(answer.result.login);
    if (answer.result.cancelled) {
      setNotice({tone: "ok", text: "진행 중이던 로그인을 취소했습니다. 다시 시도하세요."});
      onCleared?.();
    } else {
      setNotice({tone: "warn", text: "진행 중인 로그인을 취소하지 못했습니다."});
    }
  }

  return <div className="other-login">
    {login && <p>현재 로그인 상태: <span className="state-text">{LOGIN_STATE_TEXT[login.state]}</span> <CodeTag code={login.code} /></p>}
    {login?.state === "pending" && (login.cancel_supported
      ? <button type="button" onClick={() => void cancel()} disabled={busy}>{busy ? "취소 중…" : "진행 중인 로그인 취소"}</button>
      : <p className="muted">진행 중인 로그인은 앱에서 취소할 수 없습니다. 공식 로그인 창을 마치거나 닫으세요.</p>)}
    <NoticeBox notice={notice} />
  </div>;
}
