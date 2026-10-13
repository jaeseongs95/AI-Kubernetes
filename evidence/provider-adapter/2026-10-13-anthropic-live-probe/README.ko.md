# AIK-013 어댑터 실제 Messages API 최소 진단 — 2026-10-13

**결과: `partial_probe_verified_wire_only`.** AIK-082의 Anthropic API 경로 하나에 대한 부분 근거다. 운영 qualification(AIK-086)이 아니며, AIK-013·082를 완료로 처리하지 않는다.

## 실행

- 도구: [`tools/probe_anthropic_messages.py`](../../../tools/probe_anthropic_messages.py)
- 원본 보고서: [`probe-report.json`](probe-report.json)
- 환경: Linux 원격 세션, Python 3.13. egress gateway(`claude-auth.sjs9505.workers.dev`)가 인증을 주입하며 `/v1/messages`만 허용한다.
- 요청 모델: `claude-haiku-5-5`, `max_tokens` 16, 고정 프롬프트 한 문장.
- 비밀: 도구는 실제 API key를 받지 않는다. 어댑터에 placeholder를 등록하고, 전송 직전에 placeholder `x-api-key` 헤더를 제거한다. 그 외 값의 credential 헤더는 전송을 거부한다.

## 관측

| 경로 | 결과 | 의미 |
|---|---|---|
| `GuardedAdapter.start_job`(제품 경로) | `NOT_RUN_GATEWAY_REFUSED_AUTH_PREFLIGHT` | AIK-011 guard는 dispatch 전에 `auth_status`(`GET /v1/models`)를 요구한다. gateway가 이 경로를 403으로 거절해 fail-closed로 멈췄다. 우회하지 않았다. |
| `AnthropicAdapter.start_job`(wire 진단) | `probe_verified` | 어댑터가 만든 `POST /v1/messages` 요청이 실제 API에서 200을 받았다. 어댑터 decoder가 응답을 해석했다: provider model `claude-haiku-5-5`(`provider_raw_response`), `stop_reason=end_turn`, usage input 18·output 4·cache 0/0, text part 1, receipt `succeeded`, provider job ID 있음. |

## 발견한 결함과 수정

첫 실행에서 gateway 앞단의 Cloudflare가 Python 기본 User-Agent(`Python-urllib/3.x`)를 `error 1010 browser_signature_banned`로 거절했다. 어댑터는 이제 제품 이름을 밝히는 `user-agent: aik-adapter-anthropic/0.1 (AI-Kubernetes)`를 보낸다. 다른 클라이언트를 사칭하지 않는다. 같은 문제는 다른 vendor 어댑터(AIK-012 OpenAI)에도 생길 수 있으나 이번에는 바꾸지 않았다.

## 검증하지 않은 것 (NOT_RUN)

- vendor에 대한 직접 `x-api-key` 인증, 실제 계정 binding·fingerprint
- `/v1/models` 조회와 `check_model`, 그리고 guard 경로 전체(gateway 제약)
- Windows, AGS gate, VM 실행 binding, route qualification(AIK-086)
- OpenAI API·Codex·Claude Code 구독 경로(AIK-082의 나머지 필수 matrix)
