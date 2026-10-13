# AIK-017 — 출력 시크릿 제거 정책 및 구현 범위

## 상태와 책임

`src/aik_security/redaction.py`는 **신뢰할 수 있는 백엔드에서 출력 경계 전에 호출하는** Python 표준 라이브러리 기반 데이터 정제 모듈이다. GUI/PEER/감사 로그/증거/지원 번들로 내보내기 전에 호출하는 것을 전제로 하며, 이번 변경은 실제 OS vault·멀티벤더 adapter와 모든 제품 출력 지점의 통합까지 구현하지 않는다. `planning/backlog.sqlite3`의 AIK-017 상태와 AIK-004·009·011 의존성은 변경하지 않는다.

`OutputRedactor(secrets=[...])`에는 trusted credential broker가 **실제로 알고 있는 비밀 값**만 제공한다. 민감 값은 프로세스 내부의 RAM에 보관되므로 객체 자체를 `repr`·직렬화·로그로 노출하지 않는다. 짧은 비밀(12글자 미만)은 부분 노출 방지 정확도가 낮아 등록을 거부한다. 이 정책은 임의의 미지 비밀·인코딩·이미 별도 경로로 빠져나간 원문을 소급 제거할 수 있다는 보장이 아니다.

## 정제 계약

| 출력 경로 | 호출 방법 | 데이터 처리 |
|---|---|---|
| 프롬프트 미리보기 | `output("prompt_preview", raw_prompt)` | RAW는 실행 경로에서 원본 유지. 미리보기는 SHA-256·UTF-8 byte 길이와 고정 마스킹만 반환 |
| IPC 응답 | `output("ipc_response", payload)` | 중첩 객체를 정제하며 민감 키를 전부 마스킹 |
| PEER 이벤트 | `output("peer", payload)` | 토큰·인증 헤더·원시 URL·중첩 오류 객체 정제 |
| 근거/감사 로그 | `output("evidence", payload)`, `output("log", payload)` | 동일 규칙, 바이너리 생략 |
| 크래시 지원 번들/내보내기 | `output("crash_bundle", payload)`, `output("export", payload)` | 환경·traceback·argv·헤더 필드 및 미확인 객체 차단 |
| 사용자에게 반환하는 오류 | `sanitized_error(code, request_id=..., connection_id=..., task_id=..., exception=...)` | **고정 오류 코드/문구만 노출**. 원시 예외는 버리고 안전한 ID 세 개만 유지 |

기본 규칙은 등록 비밀의 원문, URL 인코딩, base64, hex 및 높은 다양성의 연속 12글자 조각을 마스킹한다. `Bearer`, `Authorization`, `Cookie`, `X-Api-Key`, provider 토큰형 접두어, `apiKey=`·`clientSecret=` 등 일반 패턴도 정제한다. URL에는 사용자정보·path·query·fragment에 비밀이 포함될 수 있으므로 진단 데이터에서는 **전체 HTTP(S) URL을 생략**한다. camelCase 및 snake_case의 secret/token/password 필드를 처리한다.

지원하지 않는 사용자 객체는 `str`·`repr`를 부르지 않고 생략한다. 64KiB를 넘는 단일 텍스트/결과, 지나친 중첩·다량 필드도 제한한다. 출력에서 `request_id`, `connection_id`, `task_id`는 허용된 식별자 형식과 비밀 탐지 기준을 동시에 통과할 때만 유지한다. ID는 인가가 아니며 임의 외부 효과의 재실행을 허용하지 않는다.

## 검증

```bash
python -m unittest discover -s tests -p test_security_redaction.py -v
```

고정 synthetic canary 기반으로 프롬프트 미리보기, IPC, PEER, evidence, log, crash bundle, export, 중첩 오류, 토큰 부분 노출, 인코딩, 헤더, URL query, 추적 ID, 지원하지 않는 객체, 대용량 실패 차단을 확인한다. Windows Server 2022와 Ubuntu 24.04 전용 CI에 같은 17개 시험을 연결한다.

**미검증:** 실제 벤더 인증·vault 값 전달/폐기, 창/세션의 모든 출력 호출부 연결, OS 프로세스 격리, Windows 11 표준 사용자 설치본, 실제 AGS/VM PEER·evidence 연결. 별도 전 경로 DLP/누출 탐색과 독립 감사가 필요하다. 따라서 **AIK-017 전체 완료 또는 제품 비밀 유출 방지 보증**으로 해석하면 안 된다.
