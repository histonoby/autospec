export type JobStatus =
  | "queued" | "running" | "succeeded" | "failed" | "cancelled"
  | "waiting_approval" | "interrupted" | "waiting_device";

export interface Health {
  ollama: boolean;
  model?: string;
  model_available?: boolean;
  model_info?: {
    family?: string; parameter_size?: string; quantization?: string;
    modelfile_num_ctx?: number | null; request_num_ctx?: number;
  };
  error?: string;
  aider: string | null;
  running_job: string | null;
}

export interface Profile { name: string; display_name: string; extensions: string[] }

export interface Sample {
  name: string; title: string; source_path: string; profile: string; inputs: Record<string, string>;
}

export interface WorkspaceRow { name: string; path: string; created_at: string; last_status: JobStatus | null; last_job: string | null }

export interface JobRow {
  id: string; workspace: string; pipeline: string; profile: string | null; status: JobStatus;
  current_step: number; error: string | null; created_at: string; updated_at: string;
}

export interface WorkspaceDetail {
  name: string; path: string; branch: string | null; head: string; workspace_yaml: string | null; jobs: JobRow[];
}

export interface TreeEntry { name: string; path: string; type: "dir" | "file"; size: number | null }

export interface StepRow {
  idx: number; name: string; status: string; started_at: string | null; ended_at: string | null; message: string | null;
}

export interface Approval {
  job_id: string; target: string; decision: "approve" | "reject" | null; comment: string | null;
  requested_at: string; resolved_at: string | null;
}

export interface Round { round: number; passed: boolean; failures: number; failed_tests: string[]; digest: string }

export interface Audit {
  overall_status: "PASS" | "WARN" | "FAIL"; score: number; reasons: string[]; summary?: string;
}

export interface JobDetail extends JobRow {
  inputs: Record<string, unknown>;
  options: Record<string, unknown>;
  steps: StepRow[];
  approvals: Approval[];
  model: string;
  state: {
    gate_passed?: boolean; best_round?: number; rounds?: Round[]; audit?: Audit; aider_round?: number;
    _error?: string; _error_message?: string;
    approval_content?: Record<string, unknown>;
    [k: string]: unknown;
  };
}

export interface JobEvent {
  id: number; job_id: string; ts: string; type: string; step: string | null; level: string;
  message: string; data: Record<string, unknown>;
}

export interface Artifact { name: string; size: number }

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try { detail = JSON.parse(text).detail ?? text; } catch { /* plain text error */ }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  const type = res.headers.get("content-type") ?? "";
  return (type.includes("application/json") ? JSON.parse(text) : text) as T;
}

export const api = {
  health: () => call<Health>("GET", "/api/health"),
  profiles: () => call<Profile[]>("GET", "/api/profiles"),
  samples: () => call<Sample[]>("GET", "/api/samples"),
  workspaces: () => call<WorkspaceRow[]>("GET", "/api/workspaces"),
  createWorkspace: (b: { name: string; mode: "empty" | "import"; source_path?: string }) =>
    call<{ name: string }>("POST", "/api/workspaces", b),
  workspace: (name: string) => call<WorkspaceDetail>("GET", `/api/workspaces/${enc(name)}`),
  tree: (name: string, path: string) =>
    call<TreeEntry[]>("GET", `/api/workspaces/${enc(name)}/tree?path=${enc(path)}`),
  file: (name: string, path: string) =>
    call<string>("GET", `/api/workspaces/${enc(name)}/file?path=${enc(path)}`),
  createJob: (b: { workspace: string; pipeline: string; profile?: string; inputs: Record<string, unknown>; options: Record<string, unknown> }) =>
    call<{ id: string }>("POST", "/api/jobs", b),
  jobs: () => call<JobRow[]>("GET", "/api/jobs"),
  job: (id: string) => call<JobDetail>("GET", `/api/jobs/${enc(id)}`),
  cancel: (id: string) => call<{ status: string }>("POST", `/api/jobs/${enc(id)}/cancel`),
  resume: (id: string, from_step?: number) =>
    call<{ status: string }>("POST", `/api/jobs/${enc(id)}/resume`, from_step === undefined ? {} : { from_step }),
  approve: (id: string, b: { target: string; decision: "approve" | "reject"; comment: string }) =>
    call<{ status: string }>("POST", `/api/jobs/${enc(id)}/approve`, b),
  artifacts: (id: string) => call<Artifact[]>("GET", `/api/jobs/${enc(id)}/artifacts`),
  artifact: (id: string, name: string) =>
    call<string>("GET", `/api/jobs/${enc(id)}/artifacts/${name.split("/").map(enc).join("/")}`),
  eventsUrl: (id: string, after: number) => `/api/jobs/${enc(id)}/events?after=${after}`,
};

function enc(s: string): string {
  return encodeURIComponent(s);
}

export const STATUS_LABEL: Record<string, string> = {
  queued: "待機中", running: "実行中", succeeded: "成功", failed: "失敗", cancelled: "キャンセル",
  waiting_approval: "承認待ち", interrupted: "中断", waiting_device: "実機待ち",
  pending: "未実行", done: "完了", skipped: "スキップ",
};

export const TERMINAL: ReadonlySet<string> = new Set(["succeeded", "failed", "cancelled"]);
