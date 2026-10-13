# worker 실행 관측·스트림·usage 회수 — AIK-036

`src/aik_observer/`는 lease 하나와 그 provider job을 관측하는 observer다. 이 패키지는 event cursor와 usage normalizer를 포함한다. lease ID는 AIK-034 lease ID이며 aik_budget reservation ID와 같다. provider 호출은 주입형 port로만 수행한다. 실제 provider·네트워크·credential은 사용하지 않는다. 표준 라이브러리와 기존 `aik_runtime`·`aik_provider`·`aik_budget` 모듈만 사용한다.

## 구성

| 요소 | 역할 |
| --- | --- |
| `ProviderEvent` | `sequence`(≥1), `event_id`, `kind`(`output_delta`·`model_reported`·`usage`·`state`·`tool_call`), plain JSON `payload`(≤64KiB), `provenance`(`provider_observed`·`model_reported`)를 담는다. digest는 sequence를 제외한 내용의 canonical JSON sha256이다. |
| `EventSource.read(after_sequence, limit)` | 주입형 이벤트 스트림이다. 첫 조회의 `after_sequence`는 `None`이다. |
| `JobPort.observe(request, receipt)` | 주입형 job 상태 조회이며 `aik_provider.JobReceipt`를 반환한다. |
| `WorkerObserver(path, *, budget, clock=None)` | 별도 SQLite 파일(application_id `0x41494B4F`)을 사용한다. 저장 내용은 현재 상태 행과 append-only `events`·`conflicts`·`settlements`, 그리고 `gaps`다. |

DB 경로는 `aik_runtime.paths` 규칙을 그대로 따른다. 저장소·개발 원장·`.git` 내부, symlink·hardlink는 거절하며 `.storage.lock` 프로세스 잠금을 공유한다. application ID·버전·테이블이 다른 기존 파일은 덮어쓰지 않고 `ObserverError("INVALID_DATABASE")`로 거절한다.


## 요청값과 관측값의 분리 (완료 기준 1)

- `start()`는 요청 model/effort를 `client_requested`로 저장한다. `requested_model`은 `JobRequest.model`과 같아야 한다.
- `observed` model/effort는 `provenance="provider_observed"`인 `state` 이벤트가 payload에 명시한 값만 기록한다. 출처 이벤트의 sequence도 함께 남긴다.
- `model_reported` 이벤트나 `model_reported` provenance의 값은 진단용 `model_reported` 칸에만 둔다. 관측값으로 승격하지 않는다.
- provider가 값을 주지 않으면 observed는 `null`이고 이유는 `NOT_PROVIDED`다. 요청값으로 채우지 않는다.

## event cursor·중복·충돌·gap (완료 기준 2)

- `ingest(lease_id, source)`는 저장된 cursor(마지막 연속 sequence) 뒤부터 읽는다. 연결이 끊기면 `disconnected=True`로 돌아오고, 다음 호출은 저장된 cursor부터 다시 읽는다.
- 이미 적용한 sequence가 같은 digest로 다시 오면 중복으로 무시한다.
- 같은 sequence에 다른 digest가 오면 `conflicts`에 append하고 stream을 `conflict`로 바꾼 뒤 `EventConflict`로 멈춘다(fail-closed). 이후 `ingest`는 `STREAM_IN_CONFLICT`로 거절한다.
- sequence에 빈칸이 있으면 빈칸 뒤 이벤트는 적용하지 않고 gap을 기록한다. 그다음 cursor부터 한 번 다시 조회해 메운다.
  - 메워지면 gap을 해소로 표시한다.
  - 다시 조회해도 빈칸이면 stream을 `gap_unresolved`로 둔다. 빈칸 뒤 이벤트는 적용하지 않으므로 완료·usage 근거가 되지 않는다.

## usage와 완료 판정 (완료 기준 3)

- `usage` 이벤트(`provider_observed`)의 필드만 `aik_budget.Usage`로 바꾼다. 키가 없으면 `UNKNOWN`, 명시적 0이면 `confirmed(0)`이다. 0이나 추정으로 채우지 않는다. 같은 호출의 뒤 보고는 총량 재보고로 다룬다.
- `model_reported` provenance의 usage는 회계 근거로 쓰지 않는다.
- 완료(terminal)는 `poll()`이 `JobPort.observe`로 받은 provider `JobState`가 `aik_provider.TERMINAL`에 속할 때만이다. `output_delta`나 HTTP 성공만으로는 완료가 아니다.
- terminal이 관측되면 현재 usage로 `BudgetStore.settle(lease_id, usage)`를 한 번 기록한다. usage가 모두 `UNKNOWN`이어도 0으로 바꾸지 않고 그대로 정산한다. 뒤에 usage가 바뀌면 다시 제출하며, budget이 `late`로 기록한다.

## 범위와 한계

- provider job의 실제 stream·receipt 연결(OpenAI·Anthropic adapter, native host)은 이 패키지가 하지 않는다. 시험은 합성 이벤트와 fake port만 쓴다.
- 이 observer는 Task 완료나 외부 효과 완료를 판정하지 않는다. provider terminal은 VM Core 판정의 입력일 뿐이다.
- 실제 provider·Windows·다중 프로세스 실행은 NOT_RUN이다.
