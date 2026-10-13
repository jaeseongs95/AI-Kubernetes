# VM governance gate 입력 준비 — AIK-086 일부

`src/aik_governance_env/`는 AIK 설치·구성 단계가 VM(vibemarshal) 프로세스를 시작할 때 넘길 governance 입력 세 가지를 VM 계약 형식대로 만들고 검사한다. 표준 라이브러리만 사용한다. VM·AGS 소스는 수정하지 않는다. 관측 모델 이름, class 선택, key/pin/state 위치는 호출자(사용자 선택·capability inventory)가 제공한다. AIK는 이 값을 지어내지 않는다.

이 문서는 구현 범위만 설명한다. 장부 상태나 완료 판정을 바꾸지 않는다. 이 모듈을 근거로 qualified 또는 PASS를 주장하지 않는다.

## 계약 출처

계약 출처는 vibemarshal commit `32bb0f9`이며, 상수는 `contract.py`에 출처 주석과 함께 고정했다.

| VM 위치 | AIK 대응 |
| --- | --- |
| `flowmarshal/engine/governance_gate.py`: 환경변수 3종, `MINIMUM_NODE=(24,0)`, `CONSUMED_SURFACE["entry_points"]` | `contract.py` |
| `governance_gate.load_model_classes` | `model_classes.check_model_class_table` |
| `GovernanceGate.preflight`의 manifest 구조 검사 | `plugin_root.inspect_plugin_root` |
| `ags_observation_producer._canonical`, `_a2_digest` | `canonical.canonical_json`, `a2_digest` (바이트 동일) |
| `ags_observation_producer._load_a2_producer` | `a2_profile.check_a2_config` |

## 세 입력과 상태 판정

| 환경변수 | 입력 | 생성 | 검사 |
| --- | --- | --- | --- |
| `FLOWMARSHAL_GOVERNANCE_PLUGIN_ROOT` | AGS 플러그인 루트 | AGS 제공(AIK 생성 안 함) | `inspect_plugin_root` |
| `FLOWMARSHAL_GOVERNANCE_MODEL_CLASSES` | model class 표 JSON | `build_model_class_table` | `check_model_class_table` |
| `FLOWMARSHAL_GOVERNANCE_A2_PROFILE` | A2 profile 설정 JSON | `compose_a2_config` | `check_a2_config` |

`prepare_governance_environment(plugin_root=..., model_class_table_path=..., a2_config_path=...)`는 입력마다 `READY` 또는 `BLOCKED`와 고정 code/check를 모은다. 경로는 절대 경로이고 정규화된 경로여야 하며, 그렇지 않으면 `PATH_NOT_ABSOLUTE`로 처리한다. 세 입력이 모두 `READY`일 때만 `env()`가 세 변수를 반환한다. 하나라도 막히면 `GovernanceEnvError("GOVERNANCE_ENVIRONMENT_BLOCKED")`를 발생시키며, 일부 변수만 담은 환경은 반환하지 않는다. `write_atomic(path, data)`는 임시 파일 작성, fsync, 게시 순서로 동작한다. 기본값 `exclusive=True`에서는 기존 파일을 덮어쓰지 않으며, 이 경우 `OUTPUT_EXISTS`가 발생한다.

### model class 표

- 이름은 provider가 관측한 model 이름(`observed_models`)이어야 하며, 아니면 `MODEL_NOT_OBSERVED`로 거절한다.
- class는 `lightweight/general/deep/frontier` 중 하나여야 하며, 아니면 `INVALID_MODEL_CLASS`로 거절한다.
- 빈 이름은 `INVALID_MODEL_NAME`, 빈 표는 `EMPTY_MODEL_CLASSES`로 거절한다.
- 출력은 정확히 `{"format","classes"}` 키를 가진 UTF-8 JSON이다(정렬 키, 끝 개행 하나).
- 검사는 duplicate key·형식 키 초과·빈 classes·enum 밖 class를 `GOVERNANCE_CONTRACT_MISMATCH`(check=`model_class_table`)로 거절한다. 반환 digest는 `sha256:` + 원본 바이트의 hex다.
- class는 호출자 설정의 주장이며 관측 결과가 아니다.

### A2 profile 설정

- `compose_a2_config`는 pin 파일만 읽는다. pin 파일은 1 MiB 이하여야 하며, 링크와 duplicate key는 거절한다. 이 파일로 `pinSetDigest`, `resourceBindingDigest`, `freezeIdentity`를 계산한다.
- **key 파일은 열지 않는다.** AIK는 개인키를 만들거나 읽지 않는다.
- `check_a2_config`의 검사 순서:
  1. JSON 구조
  2. selection·profile
  3. resources namespace
  4. location
  5. resourceBindingDigest
  6. freezeIdentity
  7. key 파일 형태
  8. pin 파일 형태
  9. pinSetDigest
- key 파일 검사는 키 집합, `version==1`, namespace, `hostId`, `keyId`, `privateKeyPkcs8`의 base64 형식만 확인한다. 이 값은 읽은 즉시 버리며 보고서·예외에 넣지 않는다.
- 보고서에는 `profile_id`, `freeze_identity`, `key_id`, `pin_set_digest`, `assurance="same-user"`, 한계 문구, `key_pin_match="NOT_CHECKED"`만 담는다.
- 보호 producer B 전용 경로는 `PROTECTED_PRODUCER_PATH`로 거절한다: `/etc/flowmarshal/ags-producer-key.json`, `/etc/agent-governance-suite/vm-operator-policy.json`, `C:\ProgramData\...`의 두 경로. 비교 시 대소문자는 구분하지 않는다.

| 상황 | code |
| --- | --- |
| JSON 파싱 실패, duplicate key, 객체 아님 | `A2_PRODUCER_CONFIG_JSON_INVALID` |
| 최상위 키·format 불일치 | `A2_PRODUCER_PROFILE_MISMATCH` (VM과 같은 코드) |
| selection 키·source·profile 불일치 | `A2_PRODUCER_PROFILE_MISMATCH` |
| resources 키·namespace 불일치 | `A2_PRODUCER_NAMESPACE_MISMATCH` |
| location이 절대·정규화 경로가 아님 | `A2_PRODUCER_{KEY,PIN,STATE}_PATH_INVALID` (VM `_read_a2_json`과 같은 계열) |
| resourceBindingDigest 불일치 | `A2_PRODUCER_RESOURCE_BINDING_MISMATCH` |
| freezeIdentity 불일치 | `A2_PRODUCER_FREEZE_IDENTITY_MISMATCH` |
| key 파일 없음/링크/크기 초과 | `A2_PRODUCER_KEY_UNAVAILABLE` (VM과 같은 코드. 링크·크기는 AIK가 더 엄격) |
| key 파일 형태 불일치 | `A2_PRODUCER_KEY_MISMATCH` |
| pin 파일 없음/링크/크기 초과 | `A2_PRODUCER_PIN_UNAVAILABLE` |
| pin 형태 불일치 | `A2_PRODUCER_PIN_MISMATCH` |
| pinSetDigest 불일치 | `A2_PRODUCER_PIN_SET_DIGEST_MISMATCH` |

### A2 same-user 한계

A2는 같은 OS 사용자 경계다. 서명이 보여 주는 것은 선택한 producer가 발행했다는 사실과 호출 결속뿐이다. 관측의 진실성이나 OS 격리의 근거가 아니다.

key와 pin의 키쌍 일치(공개키 대조)는 Ed25519 연산이 필요하다. 표준 라이브러리로는 할 수 없으므로 AIK 검사 결과는 항상 `key_pin_match="NOT_CHECKED"`이며, 일치한다고 가정하지 않는다. 실제 대조는 VM `_load_a2_producer`가 수행한다. state 자원은 AGS 서버 자원이므로 digest 결속만 확인한다.

### 플러그인 루트

`inspect_plugin_root`는 다음을 재현한다.

- `host-integration.json`의 format 검사: `agent-governance-suite.host-integration.v1`
- 네 entry point(`mcp-server`, `scope-baseline`, `scope-compare`, `acceptance-cli`)의 `path`와 `executionClosure`가 resolve 후 루트 안의 실제 파일인지 검사

검사 결과로 `closure_tree_digest`, `manifest_sha256`, `file_count`를 계산하며 provenance는 `local_derived`다. MCP 서버를 실행하거나 node를 확인하지 않는다. 선택 함수 `check_node(node_path)`는 `node --version`이 24.0 이상인지 확인한다(timeout 10초).

실패 시 check 값은 다음 중 하나다: `manifest`, `manifest_format`, `entry_point:<id>`, `closure_file`.

**AGS 3.x의 host-integration.json이 아직 병합되지 않았다.** 따라서 실제 AGS 플러그인 루트에서 `check="manifest"` 차단이 나오는 것은 현재 예상되는 결과다. 결함이나 우회 대상으로 보지 않는다.

## 실제 VM 수용 시험의 범위

`tests/test_governance_env.py`의 `RealVmContractTests`는 다음 두 조건이 모두 충족될 때만 실행된다.

- VM 소스를 import할 수 있다(`AIK_VM_SRC`, 기본값 `../vibemarshal/src`).
- `cryptography`를 import할 수 있다.

조건이 충족되지 않으면 `NOT_RUN`으로 skip한다. 실행되면 다음을 확인한다.

- 시험 안에서만 임시 Ed25519 키쌍을 만들어, VM `_load_a2_producer`가 AIK `compose_a2_config` 출력을 수용하는지 확인한다.
- VM `load_model_classes`가 AIK `build_model_class_table` 출력을 수용하고 digest가 같은지 확인한다.
- freezeIdentity를 한 글자 변조한 설정을 VM과 AIK가 같은 코드로 거절하는지 대조한다.

- closure_tree_digest가 VM preflight 공식(`sha256_bytes`, `_sha256_file`, manifest 경로 문자열 키)과 같은지 확인한다. `./` 접두 경로도 포함한다.

2026-10-13 세션 Linux에서 실제 VM 소스(vibemarshal `32bb0f9`)와 `cryptography` 50.0.1로 이 시험 3개가 통과했다. CI modules job에는 VM이 없어 skip(NOT_RUN)된다.

이 시험은 입력 형식의 수용성만 확인한다. 실제 governance 실행이나 dispatch를 검증하지 않는다.

## 검증 범위와 NOT_RUN

```bash
python -m unittest tests/test_governance_env.py -v
```

단위 시험은 합성 데이터만 사용한다.

다음 항목은 **NOT_RUN**이다.

- 실제 AGS A2 서버와의 연동
- AGS 적합성 16검사
- steward 절차
- 실제 AGS 3.x 플러그인 루트
- MCP 서버 기동
- Windows 실행 전반: 경로 정규화, `C:\ProgramData` 보호 경로, 디렉터리 fsync 부재, `os.link` 동작

Linux 시험 통과는 개발 검증일 뿐 Windows 지원의 증거가 아니다.
