"use strict";
// AIK-046·047 main 소유 키 입력 창. window.aikKeyEntry(context/submit/cancel)만 쓴다.
// 입력 값은 제출 직전에 한 번 읽고 곧바로 입력 칸을 비운다. 값을 기록·표시·보관하지 않는다.
(function () {
  const bridge = window.aikKeyEntry;
  const form = document.getElementById("key-form");
  const input = document.getElementById("key-input");
  const save = document.getElementById("key-save");
  const cancelButton = document.getElementById("key-cancel");
  const status = document.getElementById("key-status");
  const title = document.getElementById("key-title");

  const MESSAGES = {
    INVALID_SECRET: "키 형식이 올바르지 않습니다. 앞뒤 공백 없이 다시 붙여 넣으세요.",
    KEY_REJECTED: "벤더가 키를 거절했습니다. 저장하지 않았습니다. 키를 확인해 다시 입력하세요.",
    KEY_REJECTED_CLEANUP_UNCONFIRMED: "벤더가 키를 거절했고 새 연결을 지우지 못했습니다. 창을 닫은 뒤 연결 목록에서 그 연결을 삭제하세요.",
    VAULT_LOCKED: "비밀 저장소가 잠겼거나 접근이 거부됐습니다. OS 사용자 세션·저장소 잠금을 푼 뒤 다시 시도하세요.",
    VAULT_UNAVAILABLE: "사용자 비밀 저장소를 쓸 수 없습니다. OS 비밀 저장소 상태를 확인하고 다시 시도하세요.",
    VAULT_ERROR: "비밀 저장소가 실패했습니다. 다시 시도하세요.",
    DUPLICATE_CONNECTION: "같은 별칭 또는 같은 계정의 연결이 이미 있습니다. 창을 닫고 다른 별칭을 정하세요.",
    REVISION_CONFLICT: "그 사이 연결이 바뀌었습니다. 창을 닫고 연결 목록을 새로 읽은 뒤 다시 시도하세요.",
    BACKEND_TIMEOUT: "엔진 응답 시간이 초과됐습니다. 다시 시도하세요.",
    BACKEND_UNAVAILABLE: "엔진에 연결할 수 없습니다. 다시 시도하세요.",
    STORE_BUSY: "연결 저장소가 사용 중입니다. 잠시 뒤 다시 시도하세요.",
    KEY_ENTRY_BUSY: "이미 저장을 처리하는 중입니다."
  };
  const GENERIC = "키를 저장하지 못했습니다. 다시 시도하세요.";
  let busy = false;
  let ready = false;

  function setEnabled(enabled) {
    input.disabled = !enabled;
    save.disabled = !enabled;
    cancelButton.disabled = !enabled;
  }

  function show(message, tone) {
    status.textContent = message;
    status.dataset.tone = tone || "";
  }

  function codeOf(answer) {
    const code = answer && typeof answer.code === "string" ? answer.code : "";
    return /^[A-Z][A-Z0-9_]{1,63}$/.test(code) ? code : "";
  }

  function fail(code) {
    const message = Object.prototype.hasOwnProperty.call(MESSAGES, code) ? MESSAGES[code] : GENERIC;
    show(code ? message + " (" + code + ")" : message, "error");
  }

  async function submit() {
    if (!ready || busy) return;
    let secret = input.value;
    input.value = "";
    if (secret.length === 0) {
      secret = null;
      show("API 키를 입력하세요.", "error");
      input.focus();
      return;
    }
    busy = true;
    setEnabled(false);
    show("키를 확인하고 저장하는 중입니다…", "info");
    let answer = null;
    try {
      answer = await bridge.submit(secret);
    } catch (error) {
      answer = null;
    } finally {
      secret = null;
    }
    busy = false;
    if (answer && answer.ok === true) {
      // 저장이 끝나면 main이 창을 닫는다.
      show("저장했습니다.", "ok");
      return;
    }
    fail(codeOf(answer));
    setEnabled(true);
    input.focus();
  }

  function cancel() {
    if (busy) return;
    input.value = "";
    setEnabled(false);
    Promise.resolve().then(() => bridge.cancel()).catch(() => window.close());
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void submit();
  });
  cancelButton.addEventListener("click", cancel);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      cancel();
    }
  });

  async function init() {
    if (!bridge) {
      show("키 입력 창을 준비하지 못했습니다. 창을 닫고 다시 여세요.", "error");
      document.body.dataset.ready = "1";
      return;
    }
    let answer = null;
    try {
      answer = await bridge.context();
    } catch (error) {
      answer = null;
    }
    const context = answer && answer.ok === true ? answer.context : null;
    if (context && (context.mode === "add" || context.mode === "rotate")) {
      const heading = context.mode === "add"
        ? "API 키 등록 — " + String(context.display_name) + " · " + String(context.account_label)
        : "API 키 교체 — " + String(context.account_label);
      title.textContent = heading;
      document.title = heading;
      ready = true;
      setEnabled(true);
      input.focus();
    } else {
      show("키 입력 문맥을 받지 못했습니다. 창을 닫고 다시 여세요.", "error");
      cancelButton.disabled = false;
    }
    document.body.dataset.ready = "1";
  }

  void init();
})();
