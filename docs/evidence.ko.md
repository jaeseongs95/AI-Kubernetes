# 결과·artifact·evidence 회수 — AIK-037

`src/aik_evidence/`는 worker 산출물을 회수한다. 산출물의 identity와 SHA-256을 append-only로 기록하고, bytes는 관리 보관소에 인계한다. VM evidence 권위나 Task 완료 판정은 만들지 않는다. 이 기록은 VM·AGS 판정에 넘길 근거 후보다.

## 구성

| 요소 | 역할 |
| --- | --- |
| `EvidenceRelay(path, *, managed_root, clock=None)` | 별도 SQLite 원장(append-only `artifacts`·`conflicts`·`workspace_roots`)과 `managed_root/sha256/` 아래의 content-addressed 보관소 |
| `ArtifactRecord` | `artifact_id`, `kind`(`authoritative`·`derivative`), `sha256`, `size`, `vm_attempt_id`, `lease_id`, `callback_id`, `workspace_id`, `relative_path`, `access_scope`, `derived_from`, `transform`, `recorded_at` |
| `VerifyResult` | `intact`·`missing`·`tampered`와 관측 digest |
| `redact_text_artifact(relay, id, redactor)` | `aik_security.OutputRedactor`로 비밀을 제거한 표시용 파생본을 만든다 |

원장과 보관소 경로는 `aik_runtime.paths` 규칙을 따른다. 저장소·개발 원장·`.git` 내부와 링크는 거절한다. application ID나 스키마가 다른 파일은 열지 않는다(`InvalidEvidenceDatabase`).

## 수집 (`collect`)

- workspace root는 `aik_workspace.canonical_root`로 확인한다. 파일은 `aik_workspace.inputs.read_verified`로 읽는다. 그래서 링크·별칭·`..`·절대 경로·`.git`은 거절된다.
- 한 `workspace_id`는 처음 본 root에 고정된다. 다른 workspace의 root와 겹치는 경로, 관리 보관소와 겹치는 workspace도 거절한다.
- 읽은 bytes의 SHA-256으로 `authoritative` 기록을 남기고, bytes를 보관소에 한 번만 저장한다. 저장은 임시 파일, fsync, rename 순서이며 기존 객체를 덮어쓰지 않는다.
- 같은 `callback_id`가 다시 오면 다음과 같이 처리한다.
  - digest와 메타데이터가 같으면 기존 기록을 돌려준다(dedup).
  - 다르면 `CALLBACK_CONFLICT`를 append하고 `EvidenceConflict`를 던진다. 기존 기록과 bytes는 바꾸지 않는다.

## 확인·파생·내보내기

- `verify(id)`는 보관소 bytes를 다시 읽어 digest와 크기를 대조한다. 새 인스턴스(재시작)에서도 같다. worker 작업 파일이 지워져도 보관소 bytes로 확인한다.
- `derive(...)`는 표시용 파생본을 별도 `artifact_id`·digest의 `derivative`로 기록한다. 변환 이름·버전·설정 digest를 provenance로 남긴다. 원본 기록과 bytes는 바뀌지 않는다.
- `export(id, destination_dir)`는 확인된 bytes를 사용자가 고른 폴더에 **새 파일**로 저장한다. 같은 이름이 있으면 거절한다(`EvidenceExportExists`). 저장 뒤 bytes와 digest를 다시 확인한다.

## 범위와 한계

- 후속 Task 입력 binding은 AIK-084와 VM 공개 참조 계약의 범위다. 파일명만으로 최신성을 추정하지 않는다.
- 원본 보관이 정책상 불가한 경우 처음부터 비밀 없는 근거를 생산하는 경로는 이 패키지에 없다. 이 패키지는 원본을 보관하고 파생본을 따로 기록한다.
- GUI 열기·저장 연결, 보관 기간 정책, 실제 worker 반환 흐름은 NOT_RUN이다.
- 시험은 합성 파일만 쓴다. Windows 링크·권한 동작은 NOT_RUN이다.
