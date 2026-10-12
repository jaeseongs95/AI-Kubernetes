import {useEffect, useRef, useState} from "react";
import {invoke} from "@tauri-apps/api/core";
import {createRoot} from "react-dom/client";
import type {Command, ProbeResult, Response} from "./contracts";
import "./style.css";

const cards: {command: Command; id: string; label: string; title: string; description: string; button: string}[] = [
  {command: "health", id: "health", label: "01 / 연결", title: "앱과 엔진", description: "별도 Python 프로세스가 실행되어 앱의 요청을 받는지 확인합니다.", button: "엔진 연결 확인"},
  {command: "worker_probe", id: "worker", label: "02 / 실행", title: "테스트 워커", description: "별도 워커가 임시 파일을 만들고 다시 읽어 검증한 뒤 정리합니다.", button: "테스트 워커 실행"},
  {command: "vault_probe", id: "vault", label: "03 / 보관", title: "Windows 시크릿 보관", description: "합성 시험 값을 현재 Windows 사용자 범위로 보호하고 복원한 뒤 삭제합니다.", button: "Windows 보관 확인"}
];

function App() {
  const [busy, setBusy] = useState<Command | null>(null);
  const [results, setResults] = useState<Partial<Record<Command, ProbeResult>>>({});
  const [lastRequest, setLastRequest] = useState<string>("");
  async function run(command: Command) {
    setBusy(command);
    try {
      const result = await invoke<Response>("run_check", {check: command});
      setLastRequest(result.request_id);
      setResults((previous) => ({...previous, [command]: result.ok ? result.result : {status: "FAIL", code: result.error.code}}));
    } catch {
      setResults((previous) => ({...previous, [command]: {status: "FAIL", code: "APP_CONNECTION_FAILED"}}));
    } finally {
      setBusy(null);
    }
  }
  const smokeStarted = useRef(false);
  const [smokeSummary, setSmokeSummary] = useState("");
  useEffect(() => {
    if (smokeStarted.current) return;
    smokeStarted.current = true;
    async function waitFor(predicate: () => boolean): Promise<void> {
      const deadline = Date.now() + 20000;
      while (!predicate()) {
        if (Date.now() > deadline) throw new Error("RENDERER_CHECK_TIMEOUT");
        await new Promise((resolve) => setTimeout(resolve, 25));
      }
    }
    void invoke<boolean>("is_smoke").then(async (enabled) => {
      if (!enabled) return;
      for (const card of cards) {
        await waitFor(() => {
          const button = document.getElementById(card.id + "-check") as HTMLButtonElement | null;
          return Boolean(button && !button.disabled);
        });
        (document.getElementById(card.id + "-check") as HTMLButtonElement).click();
        await waitFor(() => {
          const element = document.getElementById(card.id + "-result");
          return Boolean(element && ["PASS", "FAIL", "NOT_RUN"].includes(element.dataset.status ?? ""));
        });
      }
      if (!cards.every((card) => document.getElementById(card.id + "-result")?.dataset.status === "PASS")) {
        throw new Error("RENDERER_CHECKS_DID_NOT_PASS");
      }
      await invoke("finish_smoke");
      setSmokeSummary("Tauri 비교 검사 결과를 저장했습니다.");
    }).catch(() => setSmokeSummary("Tauri 비교 검사를 완료하지 못했습니다."));
  }, []);
  const passed = Object.values(results).filter((result) => result?.status === "PASS").length;
  return <main>
    <header>
      <div className="brand"><span className="mark">AIK</span><div>AI-Kubernetes<span className="subtitle">Tauri 2 Comparison</span></div></div>
      <span className="development">비교용 · AIK-002</span>
    </header>
    <section className="intro">
      <span className="eyebrow">TAURI 2 기반 연결 검증</span>
      <h1>앱에서 실제 프로세스까지.</h1>
      <p>GUI, 엔진, 워커와 Windows 보관 기능의 연결을 직접 확인합니다.</p>
      <div className="progress"><span>{passed} / 3 확인 완료</span><span>모든 시험은 합성 데이터로 실행됩니다.</span></div>
    </section>
    <section className="cards" aria-label="기반 기능 확인">
      {cards.map((card) => {
        const result = results[card.command];
        const label = result?.status === "PASS" ? "확인 완료" : result?.status === "NOT_RUN" ? "Windows에서 실행 필요" : result?.status === "FAIL" ? "확인 실패" : "대기";
        return <article key={card.command}>
          <span className="eyebrow">{card.label}</span>
          <h2>{card.title}</h2><p>{card.description}</p>
          <button id={card.id + "-check"} disabled={busy !== null} onClick={() => void run(card.command)}>{busy === card.command ? "확인 중…" : card.button}</button>
          <div id={card.id + "-result"} className={"result " + (result?.status ?? "WAIT")} data-status={result?.status ?? "WAIT"} role="status" aria-live="polite">
            <strong>{label}</strong>
            {result && <code>{result.code}</code>}
            {result?.pid && <span>프로세스 {result.pid}</span>}
            {result?.python_version && <span>Python {result.python_version} · {result.frozen ? "번들 런타임" : "소스 런타임"}</span>}
            {result?.artifact_bytes && <span>{result.artifact_bytes} bytes 기록·검증·정리</span>}
          </div>
        </article>;
      })}
    </section>
    <section className="scope"><h2>현재 개발 범위</h2><p>이 앱은 Windows 기반 연결을 검증하는 개발용 실행본입니다. Electron 후보와 같은 세 가지 합성 작업을 실행합니다. 실제 벤더 모델 및 AGS·VM 연결은 이 비교 범위에 포함되지 않습니다.</p></section>
    <footer><span>{smokeSummary || "동일 합성 작업 · 다른 데스크톱 셸"}</span><span>{lastRequest ? "요청 " + lastRequest : "로컬 기반 검증"}</span></footer>
  </main>;
}

createRoot(document.getElementById("root")!).render(<App />);
