export type ProbeStatus = "PASS" | "FAIL" | "NOT_RUN";
export type Command = "health" | "worker_probe" | "vault_probe" | "auth_vault_probe";
export type ProbeResult = {
  status: ProbeStatus;
  code: string;
  pid?: number;
  parent_pid?: number;
  python_version?: string;
  frozen?: boolean;
  platform?: string;
  backend?: string;
  artifact_bytes?: number;
  artifact_sha256?: string;
  temporary_artifact_removed?: boolean;
  product_integration?: "NOT_RUN";
  cases?: Record<string, string>;
};
export type Response = {
  protocol_version: 1;
  request_id: string;
  workspace_id: "foundation-spike";
  command: Command;
} & ({ok: true; result: ProbeResult} | {ok: false; error: {code: string; message: string}});
export interface FoundationClient {
  health(): Promise<Response>;
  runWorkerProbe(): Promise<Response>;
  runVaultProbe(): Promise<Response>;
  runAuthVaultProbe(): Promise<Response>;
}
declare global {
  interface Window { aik: FoundationClient; }
}
