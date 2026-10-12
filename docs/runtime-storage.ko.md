# AIK runtime 저장소 — AIK-005

AIK-005의 산출물인 runtime DB schema·migration·참조 repository를 `src/aik_runtime/`에 구현한다. Python 표준 라이브러리만 사용한다. 개발 태스크 정본 `planning/backlog.sqlite3`와 별도 파일·스키마·application ID를 사용한다.

## 저장하는 데이터와 소유권

| 모델 / 테이블 | 저장하는 내용 |
| --- | --- |
| `CredentialRef` / `credential_refs` | 비밀 저장소 또는 공식 호스트의 식별 참조 |
| `Pool` / `pools` | provider·connection ID·credential 참조·설정된 동시성·사용 설정 |
| `ExecutionProfile` / `execution_profiles` | pool·역할·model·effort 식별값 |
| `Placement` / `placements` | AIK 배정 상태와 VM instance/project/task/attempt·AGS session 외부 ID |
| `Lease` / `leases` | AIK 예약 상태·placement/pool/worker·fencing 값·만료 시각·provider job 참조 |

각 모델은 필드가 고정된 immutable dataclass다. 임의 JSON payload, API key, 로그인 token, VM Task 본문·상태·verdict를 저장하는 필드나 API는 제공하지 않는다. credential locator는 `secret://local/example-ref` 또는 `host-session://provider/session-ref` 같은 참조만 받는다. 식별 필드에 전달하는 값도 실제 비밀이 아닌 외부 ID여야 한다.

이 저장소는 VM/AGS의 ID가 실제 존재하거나 실행이 허가됐다는 판정을 만들지 않는다. `enabled`, `max_parallel`, lease state와 fencing 값은 AIK가 소유하는 저장값이다. 실제 배치·용량 예약·fencing 전이·실행 인가는 AIK-033/034 및 관련 VM/AGS 연결 태스크가 수행한다. 저장 성공을 그 실행의 성공으로 해석하지 않는다.

## 사용법

현재 독립 모듈의 개발 import 경로는 저장소의 `src`다. Windows PowerShell에서 저장소 루트 기준으로 설정한다.

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
```

아래 예시는 합성 ID와 참조로 다섯 객체를 한 트랜잭션에 저장한다. 실제 credential을 읽거나 provider를 호출하지 않는다.

```python
from dataclasses import replace
from aik_runtime import (
    RuntimeStore, CredentialRef, Pool, ExecutionProfile, Placement, Lease,
    RevisionConflict,
)

store = RuntimeStore()
with store.transaction() as tx:
    tx.save(CredentialRef('credential-example', 'secret://local/example-ref'), expected_revision=0)
    tx.save(Pool('pool-example', 'synthetic', 'connection-example',
                 credential_ref_id='credential-example', max_parallel=2), expected_revision=0)
    tx.save(ExecutionProfile('profile-example', 'pool-example', 'worker',
                             'synthetic-model', effort='high'), expected_revision=0)
    tx.save(Placement('placement-example', 'pool-example', 'profile-example',
                      'vm-instance-example', 'vm-project-example', 'vm-task-example',
                      'vm-attempt-example', ags_session_id='ags-session-example'), expected_revision=0)
    tx.save(Lease('lease-example', 'placement-example', 'pool-example',
                  'worker-example', 1, '2030-01-01T00:00:00Z'), expected_revision=0)

current = store.get(Pool, 'pool-example')
assert current is not None
updated = store.save(replace(current.value, max_parallel=4),
                     expected_revision=current.revision)
assert updated.revision == current.revision + 1

# An older revision cannot overwrite the update above.
try:
    store.save(current.value, expected_revision=current.revision)
except RevisionConflict:
    latest = store.get(Pool, 'pool-example')
```

`expected_revision=0`은 새 ID에만 사용할 수 있다. 같은 예시를 다시 실행하면 중복 생성 충돌이 발생하고 트랜잭션 전체가 원복된다. 갱신·제거에는 마지막으로 읽은 양의 revision을 사용한다. `get`은 `Versioned(value, revision)` 또는 `None`, `list`는 같은 객체의 tuple을 반환한다. `list(Model, limit=100, after_id=...)`는 ID 순서의 조회다. 동시에 데이터가 변경되는 여러 번의 조회를 하나의 snapshot으로 사용할 때는 같은 `transaction()`에서 받은 `tx`로 호출한다.

`delete(Model, id, expected_revision=...)`도 revision을 확인한다. 다른 객체가 참조하는 credential/pool/profile/placement는 외래 키가 제거를 거절한다. transaction 안에서는 반환된 `tx`를 사용하고, 별도의 `store` 호출로 중첩 트랜잭션을 만들지 않는다. 각 일반 호출은 연결을 열고 작업을 마친 뒤 닫으므로 영구 연결에 대한 별도 `close()`는 필요하지 않다.

## 파일 위치와 동시성

Windows 기본 위치는 `%LOCALAPPDATA%\AIK\runtime\runtime.sqlite3`다. `RuntimeStore(absolute_path)`로 다른 로컬 경로를 지정할 수 있다. 저장소 내부, Git의 ignored 경로, `.git` 메타데이터, 개발 원장과 그 SQLite sidecar는 지정할 수 없다. symlink·reparse point·hardlink 및 Windows 장치 이름도 거절한다. Windows에서 드라이브 문자를 가진 원격 드라이브도 `GetDriveTypeW`로 확인해 차단한다. [Windows 파일 이름 규칙](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file)과 [드라이브 형식 조회](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getdrivetypew)를 따른다.

Linux의 `$XDG_STATE_HOME/aik/runtime.sqlite3` 또는 `~/.local/state/aik/runtime.sqlite3`는 개발·회귀 시험용 기본값이다. 이 fallback과 Linux 테스트 통과가 제품의 Linux 지원을 추가하지 않는다.

협력하는 저장소 프로세스는 DB 옆의 `.storage.lock` 파일에 OS 잠금을 잡는다. 잠금 파일은 삭제하지 않으며, 프로세스가 죽으면 OS가 잠금을 해제한다. 기본 대기 시간은 5초이고 `timeout`으로 변경할 수 있다. 기한을 넘기면 `StorageBusy`로 끝나며 해당 요청의 쓰기를 수행하지 않는다. 직접 SQLite로 쓰는 다른 프로그램은 이 API의 잠금·revision 계약을 따르는 저장소 클라이언트가 아니다.

SQLite의 외래 키·`DELETE` journal·`FULL` 동기화와 명시적 트랜잭션을 사용한다. 여러 객체를 쓰는 도중 예외가 전파되면 그 트랜잭션의 변경을 모두 rollback한다. 각 `save`/`delete`도 원자적이므로 트랜잭션 내부에서 잡아 처리한 개별 실패가 앞선 성공 쓰기를 손상시키지 않는다.

현재 버전은 트랜잭션을 열 때 DB 전체 사본과 무결성·레코드 검사를 수행하므로 검사 비용과 임시 디스크 사용량이 DB 크기에 비례한다. 연관된 읽기·쓰기는 하나의 `transaction()`으로 묶는다. 이번 검증은 대규모 운영 DB의 처리량이나 지연 시간을 보장하지 않는다.

## 스키마와 복구

runtime application ID는 `0x41494B52`(`AIKR`), 현재 schema version은 **2**다. version 1과 2 모두 위 다섯 종류를 저장하며, version 2는 profile→placement→lease가 서로 다른 pool을 가리키는 조합을 composite foreign key로 거절한다.

기존 파일은 원본과 journal의 private 사본에서 실제 SQLite 복구·스키마·무결성·참조 검사를 수행한 뒤 사용한다. 마이그레이션도 검증 사본에서 실행·commit·재검증하고, 성공한 결과만 원본 경로에 반영한다. 잘못된 schema, 알 수 없는 버전, 손상 파일 또는 migration 실패를 빈 DB 초기화로 덮지 않는다. 원본의 hot journal은 먼저 삭제하지 않고 SQLite의 복구 절차를 따른다.

마이그레이션 게시와 원본 복원이 연속으로 실패하거나, 게시 후 디렉터리 동기화가 실패하면 `MigrationError.recovery_directory`에 복구용 private 디렉터리를 남긴다. 그 안의 `original.sqlite3`와 기존에 존재했던 `original.sqlite3-journal`은 실패 전 원본 bytes다. 두 파일을 함께 보존하고 복구를 판단해야 한다. 이미 게시된 새 schema 파일에 옛 journal을 붙이지 않는다. 일반적인 migration 실패에서 원본을 보전한 경우에는 이 속성이 `None`이다.

프로세스 강제 종료 뒤의 transaction 복구를 실제 실행으로 검증한다. 파일은 동기화하고 지원하는 POSIX 파일시스템에서는 디렉터리도 동기화한다. Windows의 rename에 대한 전원 장애 내구성은 Python의 portable directory fsync 범위 밖이며, 이번 검증에서 전원 차단이나 저장장치 고장은 재현하지 않았다.

지원하지 않는 새 버전에서 이전 버전으로 자동 downgrade하지 않는다. 이 저장소의 schema migration과 DB transaction 복구가 VM/AGS 원장·사용자 artifact를 함께 되돌리는 제품 전체 복구의 완료를 뜻하지는 않는다. 그 통합 복구는 AIK-058의 별도 범위다.

## 검증과 원장 판정

```powershell
python -m unittest discover -s tests -p 'test_runtime_storage.py' -v
python tools/verify_runtime_storage.py --output "$env:TEMP\aik-runtime-storage-validation.json"
```

결과 파일은 새 경로에만 작성하며 기존 증거를 덮어쓰지 않는다. 모든 DB fixture는 저장소 밖의 OS 임시 폴더에 만들고, 저장·재열기, 두 저장소의 revision 충돌, 실제 writer 강제 종료와 재열기, migration 성공/실패 및 경로 거절을 검사한다. `Runtime storage` workflow는 같은 소스에서 Windows와 Linux 결과를 각각 보존한다.

이번 사용자 지시의 개발 대상은 AIK-005 한 태스크다. 원래 세 완료 기준과 AIK-001·AIK-083 의존성을 유지한다. 독립 저장 부품의 구현과 검증 근거는 기록하되, AIK-083이 미완료인 동안 AIK-005를 `in_progress` 또는 `done`으로 승격하지 않는다. 전체 제품 GUI·VM/AGS 연결·클린 Windows 11 판정도 이 부품의 테스트 결과로 대신하지 않는다.
