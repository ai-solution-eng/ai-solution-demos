export interface User {
  user_id: string;
  name: string;
  role: string;
  employee_id: string;
  department: string;
}

export interface Step {
  seq: number;
  name: string;
  state: "pending" | "active" | "done" | "failed";
  ts: string | null;
}

export interface AuditEntry {
  ts: string;
  actor: string;
  action: string;
  detail: string;
  reason?: string;
  meta?: Record<string, unknown>;
}

export interface ApprovalItem {
  request_id: string;
  run_id: string;
  case_id: string;
  client: string;
  client_code: string;
  amount_usd: number;
  requested_by: string;
  requested_emp_id: string;
  created_at: string;
  expires_at: string;
  policy_reasons: string[];
  agent?: string;
  tool_host?: string;
  facility?: string;
  limit_usd?: number;
  utilization_usd?: number;
  utilization_pct?: number | string;
  risk_rating?: string;
  covenant_status?: string;
  kyc_status?: string;
  kyc_detail?: string;
  sanctions?: string;
}

export interface ApprovalRequest {
  request_id: string;
  run_id: string;
  status: string;
  decision: string | null;
  decided_by: string | null;
  decided_at: string | null;
}

export interface Run {
  run_id: string;
  case_id: string;
  status: "PENDING" | "RUNNING" | "AWAITING_APPROVAL" | "COMPLETED" | "REJECTED" | "FAILED";
  requested_by: string;
  requested_emp_id: string;
  amount_usd: number;
  started_at: string;
  completed_at: string | null;
  request_id: string | null;
  approved_by: string | null;
  approval_role: string | null;
  fail_reason: string | null;
  memo_draft_path?: string | null;
  memo_official_path?: string | null;
}

export interface RunSnapshot {
  run: Run | null;
  steps: Step[];
  audit: AuditEntry[];
  approval_request: ApprovalRequest | null;
}

const BASE = (import.meta as any).env?.VITE_API_BASE || "";

async function jfetch(path: string, opts: RequestInit = {}) {
  const r = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...opts,
  });
  if (r.status === 401 && !path.startsWith("/api/auth/login")) {
    logout();
    throw new Error("unauthorized");
  }
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const b = await r.json();
      detail = b.detail || detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  return r.json();
}

function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export function getToken(): string | null {
  return localStorage.getItem("slvd_token");
}

export function getUser(): User | null {
  const raw = localStorage.getItem("slvd_user");
  return raw ? JSON.parse(raw) : null;
}

export function logout() {
  localStorage.removeItem("slvd_token");
  localStorage.removeItem("slvd_user");
  window.location.reload();
}

export async function login(username: string, password: string): Promise<User> {
  const r = await fetch(`${BASE}/api/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!r.ok) throw new Error("Invalid credentials");
  const b = await r.json();
  localStorage.setItem("slvd_token", b.token);
  localStorage.setItem("slvd_user", JSON.stringify(b.user));
  return b.user;
}

export async function startRun(caseId: string, amountUsd: number,
                               onSnap?: (s: RunSnapshot) => void): Promise<RunSnapshot> {
  const b = await jfetch("/api/runs", {
    method: "POST",
    body: JSON.stringify({ case_id: caseId, amount_usd: amountUsd }),
  });
  return pollRun(b.run_id, onSnap);
}

export async function getRun(runId: string): Promise<RunSnapshot> {
  return jfetch(`/api/runs/${runId}`);
}

export async function getMemo(runId: string): Promise<{ memo_md: string; path: string; status: string }> {
  return jfetch(`/api/runs/${runId}/memo`);
}

export async function resubmit(requestId: string): Promise<{ request_id: string; run_id: string }> {
  return jfetch(`/api/approvals/${requestId}/resubmit`, { method: "POST" });
}

export async function getApproval(requestId: string): Promise<ApprovalRequest> {
  return jfetch(`/api/approvals/${requestId}`);
}

export async function getPendingApprovals(): Promise<{ items: ApprovalItem[] }> {
  return jfetch("/api/approvals/pending");
}

export async function decideApproval(requestId: string, decision: "approve" | "reject",
                                     reason: string): Promise<{ request_id: string; status: string; run_id?: string }> {
  return jfetch(`/api/approvals/${requestId}/decide`, {
    method: "POST",
    body: JSON.stringify({ decision, reason }),
  });
}

export async function chat(message: string): Promise<{ reply: string }> {
  return jfetch(`/api/chat?message=${encodeURIComponent(message)}`, { method: "POST" });
}

/** Poll a run until terminal OR awaiting approval (a stable state the UI can
 *  act on). Resolving on AWAITING_APPROVAL is essential: in production mode the
 *  run parks there until the approver clicks the signed e-mail link, so
 *  polling to a terminal status would make the UI hang for the whole timeout. */
export async function pollRun(runId: string, onSnap?: (s: RunSnapshot) => void,
                              timeoutMs = 180000): Promise<RunSnapshot> {
  const settle = ["COMPLETED", "REJECTED", "FAILED", "AWAITING_APPROVAL"];
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const snap = await getRun(runId);
    onSnap?.(snap);
    if (snap.run && settle.includes(snap.run.status)) {
      return snap;
    }
    if (Date.now() > deadline) throw new Error("run timed out");
    await new Promise((r) => setTimeout(r, 1500));
  }
}
