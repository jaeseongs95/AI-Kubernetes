# AIK-093 벤더 중립 prompt cache·불투명 응답 블록·usage 구성요소

상태: **구현(부분)**. 대응 작업은 AIK-093이고 설계 원칙은 PRN-VENDOR·PRN-SOLID, 근거 결정은 DEC14다. 선행 작업 AIK-013·035가 끝나지 않았으므로 done이 아니다. 이 문서는 원장 상태를 바꾸지 않는다.

## 왜 필요한가

- 같은 작업 안에서 모델을 여러 번 부를 때, 앞부분(지시문·도구 정의·지난 대화)은 매번 같다. 이 부분을 provider 캐시에 맞히면 입력 비용과 지연이 크게 준다.
- thinking을 쓰는 모델은 응답에 `thinking` 블록을 넣는다.
  - 도구 호출 다음 턴에는 이 블록을 그대로 다시 보내야 대화가 이어진다.
  - 이전 adapter는 이 블록을 "알 수 없는 블록"으로 보고 200 응답을 실패로 처리했다.
- 비용 정산이 캐시 쓰기·읽기를 구분하지 않으면 토큰 사용량을 실제보다 적게 센다.

## 경계 (공통 port와 adapter)

| 위치 | 담는 것 | 담지 않는 것 |
|---|---|---|
| `aik_provider/turns.py` (공통) | `CachePolicy`(NONE·SHORT·LONG·PROVIDER_DEFAULT), `PromptCacheCapability`, `OpaqueBlock`, `Measured`·`Provenance`, `UsageBreakdown` | TTL 문자열, `cache_control`, 블록 형식, 벤더 usage 필드 이름 |
| `AdapterCapabilities.prompt_cache` | adapter가 공개하는 캐시 방식(`explicit`·`automatic_prefix`·`none`)과 지원 정책 | 실측 적중률 |
| Anthropic adapter | `cache_control` 위치, `thinking`·`output_config` 요청 문법, thinking 블록 보존, `cache_creation` 보존 기간별 내역 | — |
| OpenAI adapter | `cached_tokens`·`reasoning_tokens` 해석. 요청 본문은 바꾸지 않는다(자동 prefix 캐시) | reasoning 항목 보존(남은 범위) |
| `aik_dispatch.ApiToolLoop` | `ModelTurn.opaque`·`ModelTurn.usage`, `LoopResult.usage`(턴별, 합산하지 않음) | 벤더 문법 |
| `aik_budget`·`aik_observer` | `cache_read_tokens`·`cache_write_tokens`·`reasoning_tokens`, `usage_from_breakdown` | — |

공통 패키지는 벤더 adapter를 import하지 않는다(`tests/test_platform.py` `VendorBoundaryTests`). `turns.py` 본문에 벤더 용어가 없는 것도 시험으로 고정했다(`tests/test_provider_turns.py`).

## 캐시 정책

- 공통 코드는 "안정된 prefix를 얼마나 오래 둘 가치가 있는가"만 고른다.
  - SHORT: 한 작업 안에서 연달아 부르는 턴
  - LONG: 턴 사이가 긴 작업, 약 1시간
- Anthropic adapter의 처리
  - 마지막 system 블록, 마지막 tool 정의, 마지막 message의 마지막 블록에 `cache_control`을 붙인다. 표식은 3개이고, 상한 4개 안이다.
  - thinking 블록에는 표식을 붙이지 않는다.
  - SHORT는 `{"type":"ephemeral"}`, LONG은 `{"type":"ephemeral","ttl":"1h"}`로 보낸다.
- 정책이 None이면 요청 본문은 이전과 바이트까지 같다.
- `AnthropicTurnPort`는 지시문·도구·옵션을 생성자에서 고정한다. 그래서 같은 작업의 모든 턴이 같은 prefix를 보내고 대화 뒤에 덧붙이기만 한다.

## 불투명 블록

- `OpaqueBlock(provider, kind, position, raw_json)`
  - `raw_json`은 provider 응답 블록을 파싱한 뒤 다시 직렬화한 JSON이다. key 순서·값·signature는 그대로다. 원본 바이트의 공백·escape 표기까지 같다고 보장하지는 않는다.
  - `position`은 원래 응답 블록 순서다. 다시 보낼 때 이 순서로 되돌린다.
- 같은 provider에만 다시 보낸다. 다른 provider로 보내면 `OPAQUE_BLOCK_PROVIDER_MISMATCH`로 거절한다. Anthropic adapter에서는 `prepare`가 거절한다.
- ApiToolLoop는 블록이 있을 때만 assistant 메시지에 `"opaque"` key를 넣는다. 블록이 없는 기존 메시지의 형태와 digest는 바뀌지 않는다. 형식이 깨진 블록이 든 대화는 모델 호출 전에 `REPLAY_MISMATCH`로 멈춘다.
- tool_use 인자는 모델이 쓴 key 순서로 다시 보낸다. digest는 기존 canonical 값을 쓴다.

## usage 구성요소

`UsageBreakdown`은 한 턴의 토큰을 아래 구성요소로 나눈다.

| 구성요소 | 뜻 |
|---|---|
| `input_uncached` | 캐시를 쓰지 않은 입력 |
| `cache_write_short`·`cache_write_long`·`cache_write_unspecified` | 캐시에 쓴 입력. 보존 기간을 알면 short·long으로, 모르면 unspecified로 나눈다 |
| `cache_read` | 캐시에서 읽은 입력 |
| `output` | 모든 출력 |
| `reasoning` | 출력 중 reasoning 몫. `output`에 이미 포함돼 있으므로 둘을 더하지 않는다 |

- 각 값은 `Measured(value, provenance)`다.
  - provider가 보고하지 않은 값은 0이 아니라 unknown(`None`)이다.
  - provenance는 `provider_observed`(보고값) 또는 `local_derived`(보고값에서 계산)다.
- 예산의 `tokens_observed`는 `input + output + cache_read + cache_write`로 바뀌었다. 이전에는 input과 output만 셌다. 이 중 하나라도 unknown인 기록은 `token_unknown_records`로 센다. 예를 들어 캐시 쓰기를 보고하지 않는 OpenAI 기록이 여기에 들어간다.
  - unknown은 정지 판정에 쓰지 않는다. 자세한 내용은 [budget](budget.ko.md)에 있다.
- 예전 형식의 usage 문서(새 key 없음)도 그대로 읽는다.

## 실제 관측 (2026-10-14)

`tools/probe_prompt_cache.py`를 egress gateway(자격 주입, `/v1/messages`만 허용)를 거쳐 `claude-opus-5-5`로 실행했다. 보고서는 `evidence/implementation/2026-10-14-prompt-cache/probe-report.json`이다.

| 관측 | 결과 |
|---|---|
| 같은 system prefix로 2회 호출(SHORT) | 1회차 캐시 쓰기 15,614(5분 보존), 2회차 캐시 읽기 15,614 |
| `thinking: adaptive`·`effort: high` + 도구 1회 | 1턴: `thinking` 블록 1개(위치 0)와 tool_use. 블록을 다시 보낸 2턴: `end_turn`, 캐시 읽기 16,023 |

- 이 관측으로 다음 두 가지를 확인했다.
  - thinking을 켠 실제 응답에 `thinking` 블록이 들어 있다. 이전 adapter라면 이 응답을 실패로 처리했을 것이다.
  - 블록을 원래 위치로 다시 보내면 대화가 이어진다.
- thinking을 요청하지 않았을 때 블록이 오는지는 관측하지 않았다.

## NOT_RUN과 남은 범위

- LONG(1시간) 보존의 실제 적중
- 다른 provider의 실제 캐시 관측
- Windows 호스트 실행
- route qualification(AIK-086)
- OpenAI reasoning 항목을 stateless로 보존하는 일(`encrypted_content`). 지금은 `previous_response_id`가 서버 쪽에서 대화를 이어 준다.
- OpenAI용 ApiToolLoop model port
- `server_tool_use` 등 다른 블록 형식. 지금처럼 unknown으로 처리한다.
- 캐시 적중률에 따른 배치 최적화는 AIK-065(M5)다.
