# AIK-004 — Desktop IPC boundary: local implementation evidence

- Task: AIK-004 only (partially implemented; prerequisites unchanged)
- Base main before this patch: f6effcfe90dce8b57c651d21b95b1e866fa8c98f
- Source: src/aik_ipc/desktop.cjs, src/aik_ipc/preload.cjs
- Test: tests/test_desktop_ipc.cjs
- Contract and trust-profile analysis: docs/desktop-ipc.ko.md
- Local test command: node --test tests/test_desktop_ipc.cjs
- Local environment: Node v22.16.0 (Linux)
- Results: 10 PASS, 0 FAIL, 0 SKIP
- Verified source Git blob: desktop.cjs 9be3d3fad14c70babbafa0bbe1f6ec1d92250c10; preload.cjs 6d69228102157881e47acf09c620e02f1474e1f9; tests/test_desktop_ipc.cjs 32c6076df898e8a57aff25bb975651d02694f7b1
- Out of scope: installed Windows 11 application, actual OS principal isolation, VM/AGS runtime, credential broker, production qualification
- Original ledger record not edited: AIK-004 remains todo because AIK-083 is incomplete. No change to acceptance criteria/dependencies.
- Independent GitHub Actions Windows/Linux results should be judged separately after push.

This proves the bounded renderer IPC library and adversarial unit/integration simulations. It does not prove an installed product IPC boundary or task completion.
