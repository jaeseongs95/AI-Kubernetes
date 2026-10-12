# 설치형 연동 계약의 실행·검토 근거

이 디렉터리는 AIK-001의 공개 인터페이스 기준선 검사에 사용한 입력과 독립 검증을 보존한다. 실제 실행 보고서는 [installed-inspection.json](../../../contracts/upstream/installed-inspection.json)에 있다. AGS·VM 실제 모델 실행이나 제품 qualification의 성공 근거로 사용하지 않는다.

## 정확한 설치 입력

`artifacts/flowmarshal_engine-0.2.0a1-py3-none-any.whl`은 VM 커밋 `32bb0f9dd9f024045d24487312b50f5b703573a3`에서 만든 non-editable 설치 입력이다.

- 크기: 807,964 bytes
- SHA-256: `fe7cc253f2a401fd9dc618ee2b3d1af0f791129246ec47d82c88a7ccfd60389e`
- 검증한 Python 소스: 86개. 정확한 bytes와 Git blob은 `contracts/upstream/baseline.json`에 있다.
- 라이선스: Apache-2.0. wheel에 포함된 `LICENSE`, `NOTICE`를 `artifacts/`에도 원문 그대로 보존했다. AI-Kubernetes 자체의 라이선스 결정과는 별개다.

이 wheel의 build generator는 setuptools 84.0.0이다. 같은 Git 커밋을 재빌드해도 ZIP timestamp나 build backend가 달라지면 archive digest가 바뀔 수 있다. 검사기는 다른 wheel identity를 거부하며, 새 wheel에는 새 기준선과 실행 검증이 필요하다.

## 계약 schema 검증

`schema-validation.py`는 Draft 2020-12 정식 validator를 사용하며, `schema-validation.json`에는 완전한 합성 fixture와 기대/실제 결과가 있다. `task-execution-binding.before-validation.schema.json`은 최초 결함을 재현하기 위한 수정 전 입력이다.

격리된 개발 검증 환경에 `jsonschema==4.26.0`을 설치한 후, 저장소 루트에서 실행한다.

```bash
python evidence/upstream/2026-10-12/schema-validation.py \
  --schema contracts/upstream/task-execution-binding.schema.json \
  --before evidence/upstream/2026-10-12/task-execution-binding.before-validation.schema.json \
  --output /absolute/results/schema-validation.json
```

수정 전에는 세 digest 문자열의 마지막 개행을 허용했다. 각 digest의 길이를 71자로 제한한 뒤 같은 59개 fixture가 모두 통과했다. 이 검사는 schema 형식과 입력 경계를 확인한다. 실제 계정 소유권·Task/Attempt 존재·인가·lease 유효성은 후행 runtime 검증의 책임이다.
