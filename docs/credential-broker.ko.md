# AIK-018 — 작업 범위 credential broker와 실행 시점 주입

상태: **구현(부분) — 실제 Windows 동작 NOT_RUN**. 이 문서는 `src/aik_broker/`의 동작과 한계를 설명한다. 장부(`planning/`)의 AIK-018 상태와 완료 판정은 이 문서로 바꾸지 않는다.

## 1. 역할

- 총괄(director)·worker에게는 `connection_id`(및 broker가 발급한 `grant_id`)만 전달한다. `Grant` 레코드에는 `credential_ref`도 비밀도 없다.
- 신뢰된 backend(오케스트레이터)가 `issue_grant`로 adapter·workspace·task·lease(fencing token)·connection 집합을 하나의 grant에 결속한다. TTL은 1~3600초, 사용 횟수는 기본 1회(최대 16회)다.
- 등록된 adapter만 `inject`/`run_with_credential`로 실행 직전에 비밀을 요청한다. broker는 `aik_vault`에서 평문을 해석해 **adapter callback 한 번에만** 넘기고 바로 참조를 버린다.
- 평문을 호출자에게 그대로 반환하는 API는 없다. renderer용 raw secret API·IPC 명령도 만들지 않았다(`aik_ipc/desktop.cjs`는 여전히 handshake/status만 허용).

## 2. 인가 순서 (fail-closed)

1. adapter handle: `register_adapter`가 돌려준 객체 **그 자체**(identity 비교)여야 한다. 같은 이름으로 새로 만든 `AdapterHandle`, dict, 문자열(직렬화된 renderer 요청 형태)은 `ADAPTER_NOT_ALLOWED`.
2. grant 존재·미철회·미만료(`GRANT_UNKNOWN`/`GRANT_REVOKED`/`GRANT_EXPIRED`). 시계는 주입되며, 역행해도 관측한 최대값을 쓰므로 grant 수명이 늘지 않는다.
3. adapter·workspace·task 일치, connection이 grant 범위 안(`WORKSPACE_MISMATCH`/`TASK_MISMATCH`/`CONNECTION_OUT_OF_SCOPE`). 여기까지의 거절은 사용 횟수를 소모하지 않는다.
4. 남은 사용 횟수 확인 후 **즉시 소모**(`GRANT_EXHAUSTED`). 동시에 여러 요청이 와도 lock 안에서 한 번만 통과한다.
5. lease 활성(`LEASE_INACTIVE`): `lease_checker(lease_id, fencing_token)`이 정확히 `True`여야 한다. `RuntimeLeaseChecker`는 `aik_runtime` Lease가 `state == "active"`, 같은 fencing token, 주입된 현재 시각 < `expires_at`일 때만 허용한다.
6. connection 재확인: 같은 workspace·adapter provider, `api`/`api_key`, `aik_vault` 참조 형식, `verified_at` 존재, 발급 시점과 같은 `credential_ref`·`binding_revision`·`provenance_id`(`PROVIDER_MISMATCH`/`NO_BROKER_SECRET`/`AUTH_REQUIRED`/`BINDING_CHANGED`/`CONNECTION_UNAVAILABLE`). 연결 원천은 `get(connection_id)`가 `.value`로 `aik_connection.Connection`을 돌려주는 객체이며 `ConnectionStore.get`과 같은 형태다.
7. 감사 기록(`resolve_allowed`). sink가 실패하면 `AUDIT_UNAVAILABLE`로 vault를 읽지 않는다.
8. vault 해석 실패는 `CREDENTIAL_UNAVAILABLE`.

4단계 이후(lease·connection·감사·vault) 거절도 이미 소모된 사용은 되돌리지 않는다. 재시도에는 새 grant가 필요하다.

## 3. 사용법

```python
from aik_broker import CredentialBroker, RuntimeLeaseChecker

broker = CredentialBroker(vault, connection_store, RuntimeLeaseChecker(runtime_store))
adapter = broker.register_adapter('adapter-vendor', 'vendor')        # 신뢰된 backend만
grant = broker.issue_grant(workspace_id='ws-1', task_id='task-1', adapter_id='adapter-vendor',
                           connection_ids=('conn-api-a',), lease_id='lease-1', fencing_token=1,
                           ttl_seconds=60, max_uses=1)
# worker에는 grant.grant_id와 connection_id만 전달한다.
result = broker.run_with_credential(adapter, grant.grant_id, workspace_id='ws-1', task_id='task-1',
                                    connection_id='conn-api-a',
                                    argv=['vendor-cli', 'run'], delivery='stdin', timeout=30)
```

- `inject(handle, grant_id, *, workspace_id, task_id, connection_id, consumer)`: `consumer(plaintext)`를 한 번 호출하고 결과를 반환한다. 결과(문자열·bytes·`ChildResult`·list/tuple/dict 4단계까지)에 평문 또는 `aik_security.redaction`이 아는 표현(base64·hex·URL 인코딩·12자 조각)이 있으면 `SECRET_IN_OUTPUT`으로 결과를 버린다. consumer 예외는 `AdapterFailed`로 바꾸며 원래 예외를 `__cause__`/`__context__`로도 연결하지 않는다.
- `revoke(grant_id)`, `revoke_task(task_id)`, `revoke_lease(lease_id)`: lease·작업 종료 시 오케스트레이터가 호출한다. lease checker도 매 요청마다 확인하므로 철회 누락 시에도 종료된 lease는 거절된다.
- `audit_records()`: 최근 4096건. 필드는 순번·broker 시계 값·event·outcome·고정 code·ID뿐이며, ID는 `OutputRedactor.safe_id`를 통과한 것만 남는다. 각 기록은 `aik_broker` logger와 `audit_sink`(`OutputRedactor.output("log", ...)` 형식)로도 내보낸다. 4096건을 넘는 메모리 기록은 버려지므로 영속 감사는 sink 쪽 책임이다.

## 4. 자식 프로세스 주입 (완료 기준 3)

`run_injected(argv, secret, delivery='stdin'|'env', env_name=None, base_env=None, cwd=None, timeout=30)`

- `shell=False`, `close_fds=True`. 비밀은 **stdin**(UTF-8 bytes 후 EOF, 기본) 또는 **부모의 사적 env 사본의 변수 하나**로만 전달한다. `os.environ`은 바꾸지 않고, 사본은 실행 후 `clear()`한다.
- 명령행 인수와 base 환경 변수 값에서 평문·지원 인코딩을 찾으면 프로세스를 시작하지 않는다(`ARGV_CONTAINS_SECRET`/`ENV_CONTAINS_SECRET`).
- 파일을 쓰지 않는다. 시험은 작업 디렉터리 전체에 평문 bytes가 없음을 확인한다. 자식이 스스로 파일에 쓰는 것은 막을 수 없다.
- stdout/stderr는 `aik_security.redaction`으로 정제해 반환한다(64KiB 초과는 `[OMITTED]`). 12~256자가 아닌 비밀은 redactor에 등록할 수 없어 정확한 부분 문자열 치환만 한다.
- 정리 범위: POSIX에서는 자식을 새 session으로 시작하고, 기한 초과 시 아직 회수하지 않은 자식의 process group 전체에 SIGKILL을 보낸 뒤 회수한다(손자 프로세스 종료를 시험으로 확인). 손자가 스스로 새 session을 만들면 대상에서 빠진다. 기한 내 정상 종료 후 남은 손자가 stdout/stderr를 잡고 있지 않으면 정리하지 않는다.
- Windows: `CREATE_NEW_PROCESS_GROUP` 후 `Popen.kill()`(직계 자식만). Job Object 기반 손자 정리는 구현하지 않았다. **Windows 동작 전체 NOT_RUN.**
- env 전달은 같은 OS 사용자의 다른 프로세스가 자식의 환경을 읽을 수 있는 경로가 있으므로(OS별 차이, 미검증) 가능하면 stdin을 쓴다.

## 5. 보호 범위와 trust profile (완료 기준 2 단서)

이 검사는 **broker 경로**를 통한 해석만 통제한다. 같은 OS 사용자로 실행되는 native 프로세스는 vault 암호문 파일을 읽고 DPAPI를 직접 호출할 수 있으므로 이 검사로 격리되지 않는다. 해당 경로의 trust profile은 [vault ADR §3 보호 범위](vault.ko.md#3-보호-범위경계)를 따른다(동일 사용자 native 프로세스·관리자·SYSTEM·메모리 덤프는 경계 밖). 같은 프로세스 안의 코드도 신뢰 경계 안이다(예: `AdapterHandle`을 넘겨받은 코드는 adapter로 행동할 수 있다). broker 프로세스 분리와 OS principal 격리는 범위 밖이다.

평문은 `str`로 adapter callback에 전달되며 Python 불변 문자열은 지울 수 없다. 메모리 잔존 최소화는 best effort다.

## 6. 검증

```bash
python -m unittest discover -s tests -p 'test_broker.py' -v
```

| 항목 | 상태 |
|---|---|
| 다른 workspace·task·범위 밖 connection·임의 renderer/위조 handle·종료/만료 lease·만료/재사용/철회 grant·미확인/재결속 connection 거절, 동시 재사용 1회 통과 | 합성 데이터 단위 시험(fake vault, fake 연결 원천, fake/실제 `RuntimeStore` lease). 이 문서 작성 시점에 실행 결과 기록 없음 |
| 로그·감사·sink·예외(str/repr/traceback)·반환값의 평문 비검출(`OutputRedactor` 비교) | 위와 같음 |
| 자식 stdin/env 전달, argv·env 차단, 출력 정제, 파일 비잔존, POSIX timeout 시 process group 종료 | Linux 개발 검증 대상. 실행 결과 기록 없음 |
| 실제 `ConnectionStore`·DPAPI vault와의 연동 | **NOT_RUN** (시험은 같은 형태의 fake 사용) |
| Windows 자식 프로세스 정리·환경 블록 노출·경로 | **NOT_RUN** |
| 실제 provider adapter·Electron main 연결 | 범위 밖 |

Linux 시험 통과는 개발 검증이며 Windows 지원 증거가 아니다. 선행 태스크(AIK-004·009·010·017)의 미완료 부분과 Windows 실측이 남아 있으므로 이 구현만으로 AIK-018 완료를 주장하지 않는다.
