# AIK-083 — 제품 인수 계약 v1 / 독립 파일 oracle

## 상태와 책임

이 문서와 `contracts/product-acceptance/v1.json`은 **AIK-083의 실제 초기 구현 산출물**이다. 구현 전 계약을 고정하기 위한 **제안 판본**이며 현재 상태 `proposed / candidate_not_qualified`다. AIK-003의 실제 클린 Windows 11 일반 사용자 검증이 완료되지 않았으므로 **태스크 상태를 완료로 변경하지 않으며** 이 도구 결과를 출시 판정으로 쓰지 않는다. Windows Server CI는 Windows 11 설치본 인수 근거가 아니다.

입력 기준은 `planning/backlog.sqlite3`의 기존 P01~P12, 생성 보고서 `docs/development-backlog.ko.md`, `contracts/upstream/support-matrix.json` revision 1이다. 위 정본과 충돌하면 본 후보 계약이 아니라 원장의 기준과 소유자 결정을 따르며, 갱신 시 결정·범위·재검증 근거를 기록해야 한다.

## 제품 흐름 매핑

계약 JSON은 P01~P10을 M4 `AIK-064`, P11을 M5 `AIK-089`, P12를 M6 `AIK-090`으로 매핑하고, M5는 P01~P11, M6는 P01~P12의 **동일 새 설치본 회귀**를 요구한다. 각 항목에는 최소 사용자 입력, 관측 가능한 성공, 필수 차단 사유, 구현 담당 태스크, GUI 담당 태스크, 검증 태스크를 기록했다. 단순 체크리스트만으로 판정을 내리지 않는다. 첫 출시 지원 조합은 matrix git blob `b563f9f63b88f2a6e52a77f3e67acc871a98b88e`, revision 1에 고정된다. DEC12 지원 범위는 여전히 `proposed`, 각 필수 경로의 운영 적합성은 `NOT_RUN`이며 이를 건너뛰지 않는다.

개발 흐름은 001의 설치형 계약 확인→031 VM 공개 seam 확보/085 도구 효과 계약/082 실제 사전 probe→035 실행 결속→086 후보 governed qualification→044 독립 합성 E2E→059 설치형 GUI 검증→064 Windows 릴리스 게이트 순서다. 082의 사전 probe를 완료된 086 운영 적합성으로 간주하지 않는다. M5/M6 추가 자원은 선택했을 때 해당 실제 gate 의존성에 넣으며, 미선택 태스크를 done 처리하지 않는다.

## 고정 제안 부하·성능 한계

AIK-003이 제시한 최소 2 logical CPU/8 GiB RAM/디스크 여유 2 GiB, 기준 4 CPU/16 GiB RAM/4 GiB, 1280×800/1920×1080, Windows 11 25H2/26H2 x64 후보를 유지한다. AIK-083의 **측정 전 승인 대상 제안 임계값**은 아래와 같다. 이는 이전에 측정하여 확인된 값도, 사용자가 이미 승인한 결정도 아니다.

| 측정 | 제안 임계값 |
|---|---|
| cold 시작 20회 | p95 ≤ 15,000ms |
| warm 시작 20회 | p95 ≤ 8,000ms |
| 20 세션·4 worker·GUI 조작 100회 | 입력 응답 p95 ≤ 300ms, p99 ≤ 750ms |
| 0/3 connection metadata 상태에서 각 10분 유휴 | 의도하지 않은 모델 호출 0회 |
| 기준 사양 8시간 연속 실행 | 메모리 증가 ≤ 256MiB, orphan owned worker 0 |
| unknown/stale effect | 자동 재실행 0회 |

프로바이더의 외부 지연과 GUI/로컬 제어부 시간을 분리해 측정한다. 분포별 관측값, 스레드/사용량, 계정·빌드·하드웨어 고정 상태는 AIK-062가 남겨야 한다. 임계값을 **실제 첫 제품 측정 전에** 승인/동결하고 사후 변경하면 결정·영향·재검증을 다시 요구한다. 현재 계약의 `proposed`를 `confirmed`로 바꾼다고 실제 실기 검증이 자동 생성되지는 않는다.

## 고정 RAW Goal과 독립 oracle

합성 참조 프로젝트 `contracts/product-acceptance/fixture/project`는 **정답을 포함하지 않은 오류 있는 초기 소스**, `orders.csv`, `policy.json`만 가진다. `settlement.json` 완성 출력·Plan·Task·GoalVerdict는 포함하지 않는다. 고정 사용자 RAW 요청은 `v1.json.oracle.raw_goal`이며, VM/총괄의 실제 계획 준비가 A/B 병렬→C 의존 실행을 생산해야 한다. 평가 전에 workspace 입력 원본 두 파일을 Git blob hash로 고정한다. 평가는 모델의 성공 선언이나 provider job 상태가 아닌 실제 정산 파일의 bytes를 직접 읽고 독립 계산값과 대조한다.

명령:
```bash
python tools/product_acceptance.py validate
python tools/product_acceptance.py oracle --workspace "/path/to/한글 공백 프로젝트"
python tools/product_acceptance.py release-preflight --report "/path/to/release-evidence.json"
python -m unittest discover -s tests -p test_product_acceptance.py -v
```

oracle의 기본 합성 정산에서 **net 41,510원 / line-by-line VAT 4,152원 / gross 45,662원**이 요구된다. 입력/정책·행의 금액형·합계·행 순서와 결과의 SHA-256을 검증하며 원시 비밀이나 VM 원장에는 접근하지 않는다. 원본 `settlement.py`는 전체 net에 VAT를 한 번 적용하고 행을 생략하므로 그대로 실행한 첫 시험은 **FAIL**이어야 한다. 그러나 예제용 정답 JSON만 직접 써서 독립 oracle에 통과하는 것으로 **AGS/VM의 실제 goal 수행, 첫 실패→재작업, 모델 A/B/C와 tool effect를 증명할 수는 없다.** 그 확인에는 서로 다른 벤더·모델 요청과 VM Task/Attempt, lease, effect intent/receipt, 실제 검증·독립 review 기록이 추가로 필요하다.

## 출시 사전검사 한계

`release-preflight`는 후보 계약 승인 여부, 같은 installer SHA/commit/matrix, 필수 P 흐름 PASS, 독립 심사 근거의 존재, Windows 11 일반 사용자·도구 없음 상태, 동일 빌드 회귀, 고정된 성능 임계값을 보수적으로 점검한다. 필수 `FAIL/NOT_RUN/BLOCKED/SKIP/UNKNOWN`, 미승인 scope/threshold/route, 중대 결함은 **BLOCKED**다. 결과가 `READY_FOR_INDEPENDENT_RELEASE_REVIEW`라도 **출시 승인과 같지 않다**. 이 검사기는 evidence JSON의 독립 서명을 검증하지 않으므로 실제 CI artifact/원본 해시·권위 receipt를 별도의 감사관이 대조해야 한다.

현 계약이 `proposed`이므로 임의의 녹색 JSON을 넣어도 사전검사가 출시를 통과시킬 수 없다. 정본 원장과 상충하는 상태 변경·선행 관계 변경도 하지 않는다.
