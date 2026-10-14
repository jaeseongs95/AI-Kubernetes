"use strict";
// Test-only stand-in engine: behaves according to argv[2]. Synthetic values only.
const readline = require("node:readline");
const mode = process.argv[2];
const lines = readline.createInterface({input: process.stdin});
let nonce = null;
const write = (value) => process.stdout.write(JSON.stringify(value) + "\n");
lines.on("line", (line) => {
  const message = JSON.parse(line);
  if (nonce === null) {
    nonce = message.nonce;
    if (mode === "incompatible") { write({kind: "incompatible", supported_versions: [2]}); process.exit(3); }
    if (mode === "silent") return;
    if (mode === "exit") process.exit(1);
    write({kind: "ready", protocol_version: mode === "newer" ? 2 : 1, pid: process.pid, engine_version: "9.9.9"});
    return;
  }
  if (mode === "garbage") { process.stdout.write("not json\n"); return; }
  if (mode === "hang") return;
  if (mode === "wrong-id") { write({protocol_version: 1, request_id: "00000000-0000-4000-8000-000000000000", command: message.command, ok: true, result: {}}); return; }
  if (mode === "crash-after-ready") process.exit(1);
  const authorized = message.capability === nonce;
  write(authorized
    ? {protocol_version: 1, request_id: message.request_id, command: message.command, ok: true, result: {echo: message.command}}
    : {protocol_version: 1, request_id: message.request_id, command: message.command, ok: false, error: {code: "UNAUTHORIZED"}});
});
