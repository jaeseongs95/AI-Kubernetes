# AIK-083 — 독립 파일 oracle의 검증 바이트 결속

AIK-083의 기존 제품 인수 계약과 출시 승인 조건은 유지한다. 이번 변경은 파일 기반 oracle이 검증한 데이터와 영수증의 해시가 어긋나는 경로를 수정하고 입력 자원 제한을 보강하는 작업이다. 실제 모델 호출·완제품 사용자 흐름·Windows 설치본 검증이나 인수계약 승인을 대신하지 않는다.

## 파일 검증과 해시

기존 검사기는 입력 hash 확인 뒤 계산 과정에서 입력 파일을 다시 열었고, 산출물 JSON 검사 뒤 SHA-256을 만들기 위해 산출물을 다시 읽었다. 그 사이 파일이 바뀌면 검사한 내용과 영수증이 다른 바이트를 가리킬 수 있었다.

`tools/acceptance_io.py`의 `read_snapshot`은 일반 파일을 크기 제한 내에서 한 번만 읽고 불변 bytes를 반환한다. 입력 파일의 Git blob hash 확인, 독립 계산, 산출물 파싱·검증과 SHA-256이 해당 bytes 사본을 사용한다. 최종 영수증에는 검증한 `artifact_bytes`도 포함한다. descriptor 및 경로의 identity·size·mtime을 교차 비교하고, 각 조회 방식의 ctime을 포함한 전후 통계도 따로 비교한다. 검사 종료 직전에도 입력과 산출물의 경로 통계를 다시 확인한다.

Windows의 복사된 합성 파일에서 `lstat`와 `fstat`의 `st_ctime_ns`가 다르게 관측됐다. 따라서 ctime을 두 API 사이에서 직접 일치시키지 않는다. 경로 조회는 최초 경로 조회값과, 파일 핸들 조회는 최초 핸들 조회값과 비교한다. 파일 identity·크기·mtime·일반 파일/reparse 검사와 각 방식의 전후 ctime 검사는 유지한다. 관측 근거와 실패 후 보정은 아래 검증 기록에 보존한다.

이 방식은 파일 경합의 관찰 가능한 변경을 차단하고 영수증을 검사한 바이트에 결속한다. 동일 OS 사용자 공격자의 모든 교체·메타데이터 복원·다중 파일 동시 변경을 원자적으로 막는 OS 격리나 전체 디렉터리 transaction을 제공하지는 않는다. 검사 뒤 파일이 변경되는 상황까지 영구 불변이라고 보증하지 않는다.

## JSON 의미와 입력 제한

JSON object의 멤버 순서만 달라졌다는 이유로 동일한 정산 결과를 실패 처리하지 않는다. 그러나 rows 배열의 순서, 금액의 정확한 int 타입, 행/합계 일치, 필수 필드와 추가 필드 거절은 그대로 유지한다. bool·float 금액, 중복 JSON key와 변조된 입력은 통과하지 않는다.

파일 크기는 1MiB 한도이며, 무제한 `read_bytes()`로 전부 할당한 뒤 검사하지 않는다. 비정규 파일·symlink/reparse point를 거절하고, 읽는 중 파일 성장도 제한한다. JSON depth 64·노드 10,000 한도를 적용하며 `1e309` 같은 부동소수점 overflow와 lone surrogate를 포함한 잘못된 문자열을 고정 오류로 거절한다. 원시 파일 경로나 예외 내용을 정상 진단에 붙이지 않는다.

표준 API 참고: Python `os.open/fstat/lstat/read`와 `stat.S_ISREG` 문서(https://docs.python.org/3/library/os.html, https://docs.python.org/3/library/stat.html). 파일 통계와 descriptor 대조는 운영체제가 제공하는 관측값 범위에서만 유효하다.

## 검증·원장

```bash
python -m unittest discover -s tests -p "test_product_acceptance*.py" -v
python tools/product_acceptance.py validate
python tools/backlog.py validate
```

기존 12개 테스트는 변경하지 않는다. 신규 24개 중 선택 반례 8개를 정확한 이전 검사기에서 재현하고, 현재 수정본 36개와 정본 원장 회귀를 Windows/Linux에서 검증한다. 실행 결과·소스 바이트 hash와 원장 변화는 [근거 기록](../evidence/product-acceptance/2026-10-13-snapshots/README.ko.md)에 결속한다.

AIK-083에만 구현 evidence와 `todo → blocked` 상태 이력을 기록한다. AIK-003의 깨끗한 Windows 검증, 실제 VM/AGS 인수 흐름과 계약 승인 조건은 미완료로 유지하며 원래 완료 기준·의존성·지원 matrix·정답 fixture는 변경하지 않는다.
