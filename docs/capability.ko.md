# AIK-020 — 모델·추론 수준·도구 capability 조회와 검증

`src/aik_capability/`는 연결(resource)별 모델 목록을 받아 inventory를 만들고, 총괄·worker 설정의 `(model, effort, tools)` 조합이 그 resource에서 지원되는지 판정한다. 이 패키지는 표준 라이브러리만 사용한다. 다음 기존 패키지를 import해 재사용하며, 수정하지 않는다.

- `aik_provider`: `ConnectionBinding`을 resource 식별자로 사용한다.
- `aik_pool`: `Freshness`(fresh/stale/unknown) 규칙을 사용한다.
- `aik_runtime`: 식별자 검증(`identifier`)을 사용한다.

실제 provider 호출, 모델 목록 API 조회, 로그인은 하지 않는다. 목록 JSON은 호출자가 주입한다.

## 모델 이름을 고정하지 않음

제품 코드에는 모델 이름이 없다. 모델은 resource마다 다음 두 방식으로 등록한다.

- 목록 로드: `load_list(connection_id, provenance, raw_json)`
- 검증된 수동 등록: `register_manual(connection_id, raw_json, catalog_ref=...)`

수동 등록은 `configured_catalog` provenance로 저장되며, 검토·승인 기록을 가리키는 `catalog_ref` 식별자가 필수다. 참조 없이 `configured_catalog`를 로드하면 `CATALOG_REFERENCE_REQUIRED`로 거절한다.

시험은 합성 이름(`synthetic-model-*`)만 사용한다. 패키지 소스에 대표적인 vendor 모델 이름 패턴이 없는지도 검사한다.

## provenance

모든 snapshot과 항목(`ModelCapability`)은 provenance를 필수 필드로 가지며, 기본값은 없다.

| provenance | 의미 | 지원 근거로 사용 |
| --- | --- | --- |
| `requested` | 사용자·설정이 요청한 값 | 아니오 |
| `provider_observed` | provider에서 관측한 목록 | 예(우선) |
| `configured_catalog` | 검증 참조가 있는 수동 등록 | 관측 목록이 없을 때만 |
| `local_derived` | 로컬에서 추론·계산한 값 | 아니오 |

`requested`와 `local_derived` 목록에 모델이 있어도 지원으로 판정하지 않는다. 거절 이유에 `requested:NOT_SUPPORT_EVIDENCE` 같은 코드로 표시만 한다. 검증 결과의 요청값은 `request_provenance=requested`로 남으며, inventory의 관측 목록으로 복사·승격되지 않는다.

## resource별 별도 capability

inventory 키는 `ConnectionBinding.connection_id`다. 이름이 같은 모델이라도 resource가 다르면 서로 무관한 capability로 판정한다. 예를 들어 `conn-a`에서 effort `high`가 지원되어도, `conn-b`의 같은 이름 모델에는 적용하지 않는다. 모델 이름 조회도 정확히 일치하는 이름만 찾는다.

## 원본 목록 JSON 형식과 거절 규칙

입력 형식은 다음과 같다.

```json
{"models": [{"model": "synthetic-model-alpha", "efforts": ["low", "high"], "tools": ["shell"]}]}
```

typed 변환 전에 아래 경우를 고정 코드의 `CapabilityError`로 거절한다. 거절된 목록은 inventory와 revision을 바꾸지 않는다.

- `DUPLICATE_KEY`: JSON 객체 키가 중복된다.
- `NULL_VALUE`: 위치와 무관하게 null이 있다.
- `EMPTY_VALUE`: 빈 문자열 또는 공백뿐인 문자열이 있다.
- `INVALID_JSON_CONSTANT`: `NaN`·`Infinity`가 있다.
- `DUPLICATE_MODEL`, `DUPLICATE_EFFORT`, `DUPLICATE_TOOL`: 같은 값이 중복된다. 대소문자만 다른 값도 중복으로 본다.
- `INVALID_DOCUMENT`, `INVALID_ENTRY`: 키 집합이 정확히 `models` 또는 `model`·`efforts`·`tools`가 아니다.
- `INVALID_MODEL`: 형식이 맞지 않는 모델 값이다.
- `INVALID_JSON`, `INVALID_ENCODING`, `RAW_TOO_LARGE`(1 MiB 초과)

빈 배열은 허용한다. 빈 `models`는 "모델 없음"이라는 관측이므로, 모든 조합을 `MODEL_NOT_LISTED`로 거절한다. 빈 `efforts`/`tools`는 지원 항목이 없다는 뜻이다.

snapshot은 원문 bytes의 SHA-256 `digest`와 원문의 모델 순서(`models`)를 보존한다. effort·tool 순서도 원문 그대로 유지한다. 오류 메시지는 입력 문자열을 포함하지 않는다.

## validator 판정

`validate(connection_id, model, effort=None, tools=())`는 `ValidationResult`를 반환한다. 판정 규칙은 다음과 같다.

1. `provider_observed` snapshot이 있으면 그것만으로 판정한다. 그 snapshot이 TTL을 넘었거나 시계가 역행하면 `INVENTORY_STALE`로 거절한다. 이 경우 catalog로 대신하지 않는다.
2. 관측 snapshot이 없으면 `configured_catalog`로 판정한다.
3. 둘 다 없으면 `NO_SUPPORT_EVIDENCE`로 거절한다.
4. 모델이 없으면 `MODEL_NOT_LISTED`, effort가 목록에 없으면 `EFFORT_NOT_SUPPORTED`, 도구가 없으면 도구마다 `TOOL_NOT_SUPPORTED:<tool>`을 낸다. 이유는 `provider_observed:` 같은 provenance 접두어를 붙인다.

`effort=None`은 effort를 지정하지 않은 요청이며, effort 검사를 생략한다. 수락된 결과는 `supported_by`(근거 provenance)와 `evidence_digest`(근거 목록의 digest)를 가진다.

## 목록 변경과 stale 전이

resource의 목록이 바뀌면 resource revision이 증가한다. 모델 추가·제거, effort·tool 변경, catalog 참조 변경, 목록 삭제, binding 갱신이 모두 해당한다. 같은 digest의 재로드는 revision을 바꾸지 않고 관측 시각만 갱신한다.

`freshness(result)`는 다음 경우에 `stale`을 반환한다.

- 결과를 만든 뒤 resource revision이나 binding revision이 바뀌었다.
- 관측 목록에 근거한 결과인데, 그 목록이 TTL을 넘었다.

resource가 등록되어 있지 않으면 `unknown`이다. `revalidate(result)`로 같은 요청을 다시 판정한다. 다른 resource의 변경은 결과에 영향을 주지 않는다.

`update_binding()`으로 새 connection revision(재로그인·계정 변경 등)을 반영하면 다음과 같이 처리한다.

- 계정에 종속된 `provider_observed`·`local_derived` 목록을 버린다.
- `configured_catalog`와 `requested`는 유지한다.

provider·runtime·auth_kind 변경이나 revision 역행은 거절한다.

## 검증

```powershell
python -m unittest discover -s tests -p 'test_capability.py' -v
```

검사 항목은 다음과 같다.

- 원문 순서·digest 보존과 거절 규칙
- 같은 이름의 resource별 분리
- requested·local_derived 비승격
- 지원되지 않는 effort/tool 조합
- 모델 추가·제거에 따른 stale 전이
- TTL·시계 역행, 수동 등록과 관측 우선순위, binding 갱신, 입력 검증

## 한계·미검증(NOT_RUN)

- 실제 provider의 모델 목록 조회 adapter는 없다. 공통 adapter 계약에 목록·도구 probe가 없으므로, provider 응답을 이 JSON 형식으로 바꾸는 변환은 후행 연결 태스크 범위다.
- `configured_catalog`의 `catalog_ref`는 형식만 검사하며, 참조된 검토 기록의 실재·승인 여부는 검증하지 않는다.
- inventory는 프로세스 메모리에만 있다. 재시작 후에는 목록을 다시 로드해야 한다.
- 실제 provider, 실계정, Windows 11 동작은 미검증이다. Linux 단위시험 통과는 Windows 지원 증거가 아니다.
- 장부 상태와 완료 판정은 변경하지 않는다.
