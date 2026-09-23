import React, { useCallback, useEffect, useRef, useState } from "react";
import MemoBody from "./MemoBody";
import {
  chat, decideApproval, getApproval, getMemo, getPendingApprovals, getRun, getUser, login,
  logout, pollRun, resubmit, startRun,
  type ApprovalItem, type RunSnapshot, type User,
} from "./api";

const CASES = [
  { id: "CR-2026-00451", client: "Acme Industrial Holdings", type: "Renewal Decision Memo", amount: 5000000 },
  { id: "CR-2026-00452", client: "Blue Harbor Logistics", type: "Renewal Decision Memo", amount: 2000000 },
  { id: "CR-2026-00453", client: "Cedar Peak Materials", type: "Renewal Decision Memo", amount: 2000000 },
];

// Mailpit web UI (approval + client emails). The demo deploys a dedicated
// ingress host (slvd-mail.<platform domain>) that proxies to mailpit:8025.
// Resolve it at RUNTIME — adaptive to whatever platform domain the app is
// deployed on (no baked-in cluster hostname):
//   1. window.__SLVD_MAIL_BASE__   explicit override (e.g. injected config.js)
//   2. localhost / 127.0.0.1 dev   -> port-forwarded mailpit on :8025
//   3. deployed host "slvd.<base>" -> "https://slvd-mail.<base>"
//   4. unknown host                -> same-origin :8025 fallback
function mailUrl(): string {
  const override = (window as any).__SLVD_MAIL_BASE__;
  if (typeof override === "string" && override.trim()) return override.trim().replace(/\/+$/, "") + "/";
  const host = window.location.hostname || "";
  if (host === "localhost" || host.startsWith("127.")) return "http://127.0.0.1:8025/";
  if (host.startsWith("slvd.")) return "https://slvd-mail." + host.slice("slvd.".length) + "/";
  return window.location.protocol + "//" + host + ":8025/";
}

// ---------- architecture (who does what) ----------

const ACTORS = [
  { icon: "👤", name: "Analyst", sub: "you — requests the memo", color: "#1e40af" },
  { icon: "🖥", name: "Portal", sub: "authenticates + tracks the run", color: "#0f766e" },
  { icon: "⚙", name: "Workflow Engine", sub: "orchestrates + policy gate", color: "#7c3aed" },
  { icon: "🤖", name: "credit-memo-agent", sub: "LLM drafts the memo", color: "#db2777" },
  { icon: "🗄", name: "credit-memo-mcp", sub: "governed tools → bank systems", color: "#b45309" },
];

const STEP_META: Record<number, { actor: string; why: string }> = {
  1: { actor: "🔐 Identity", why: "proves who is requesting (JWT) before any governed data is accessed" },
  2: { actor: "🗂 Case", why: "resolves the case ID to the canonical case record the agent works on" },
  3: { actor: "🗄 MCP · CRM", why: "governed tool get_crm_profile — relationship context (industry, tenure, ownership)" },
  4: { actor: "🗄 MCP · Credit", why: "governed tool get_credit_exposure — limits, utilization, risk rating" },
  5: { actor: "🗄 MCP · Transactions", why: "governed tool get_transactions — repayment-behavior evidence" },
  6: { actor: "🗄 MCP · Compliance", why: "governed tool get_compliance_status — KYC / sanctions the policy needs" },
  7: { actor: "🗄 MCP · Prior Memos", why: "governed tool get_prior_memo — prior conditions + open follow-ups" },
  8: { actor: "🤖 LLM Agent", why: "credit-memo-agent (LLM) drafts the memo from the five data payloads" },
  9: { actor: "🛡 Policy", why: "policy engine checks amount threshold + KYC — governs publication" },
  10: { actor: "✉ Human", why: "a Senior Credit Officer signs off (signed email link or this portal)" },
};

function ArchitectureStrip() {
  return (
    <div className="card arch">
      <h2>How this run is governed</h2>
      <div className="arch-flow">
        {ACTORS.map((a, i) => (
          <React.Fragment key={a.name}>
            <div className="arch-node" style={{ borderColor: a.color }}>
              <div className="arch-icon">{a.icon}</div>
              <div className="arch-name" style={{ color: a.color }}>{a.name}</div>
              <div className="arch-sub">{a.sub}</div>
            </div>
            {i < ACTORS.length - 1 && <div className="arch-arrow">→</div>}
          </React.Fragment>
        ))}
      </div>
      <div className="arch-note">
        The analyst's request flows through the <b>workflow engine</b>, which drives the
        <b> LLM agent</b> (the memo writer) and the <b>governed MCP tools</b> (every bank-system
        read is identity-checked and audit-logged). The <b>policy gate</b> can pause the run until a
        <b> senior credit officer</b> approves — the memo is only published after sign-off.
      </div>
    </div>
  );
}

// ---------- decision summary (bottom line up front) ----------

function recommendedDecision(memo: string | null): string | null {
  if (!memo) return null;
  const m = memo.match(/##\s*Recommended Decision\s*\n+([\s\S]*?)(?:\n#|\n##|$)/i);
  if (!m) return null;
  const first = m[1].trim().split("\n").find((l) => l.trim()) || null;
  return first;
}

function DecisionSummary({ snap, memo }: { snap: RunSnapshot; memo: string | null }) {
  const run = snap.run!;
  const audit = snap.audit ?? [];
  const policy = audit.find((a) => a.action === "Policy evaluation");
  const reasons = (policy?.meta?.reasons as string[] | undefined) ?? [];
  const decision = audit.find((a) => a.action.startsWith("Decision recorded"));
  const published = audit.find((a) => a.action === "Memo published");
  const rec = recommendedDecision(memo);
  return (
    <div className="card decision">
      <h2>Decision Summary</h2>
      <div className="dverdict">
        <span className={`status-pill ${run.status === "COMPLETED" ? "ok" : "err"}`}>{run.status}</span>
        {run.status === "COMPLETED" && <span className="dtext">Renewal approved — memo published to the official record.</span>}
        {run.status === "REJECTED" && <span className="dtext">Renewal rejected by the senior credit officer.</span>}
        {run.status === "FAILED" && <span className="dtext">Run failed: {run.fail_reason}</span>}
      </div>
      <div className="kv"><span className="k">Recommended</span>
        <span className="dv">{rec ?? "—"}</span></div>
      <div className="kv"><span className="k">Policy trigger</span>
        <span className="dv">{reasons.length ? reasons.join(" + ") : "no approval required (auto-completed)"}</span></div>
      <div className="kv"><span className="k">Decided by</span>
        <span className="dv">{run.approved_by ? `${run.approved_by} (${run.approval_role}) — ${decision?.detail ?? ""}` : "—"}</span></div>
      <div className="kv"><span className="k">Memo</span>
        <span className="dv mono">{String(published?.meta?.path ?? run.memo_official_path ?? "draft only")}</span></div>
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<User | null>(getUser());
  const [snap, setSnap] = useState<RunSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [chatOpen, setChatOpen] = useState(false);
  const [memo, setMemo] = useState<string | null>(null);
  const [expandedAudit, setExpandedAudit] = useState<Set<number>>(new Set());
  const [fromConsole, setFromConsole] = useState(false);
  const runIdRef = useRef<string | null>(null);
  const terminalRef = useRef(false);

  // Read ?run= (dashboard deep link from approval email)
  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    const run = p.get("run");
    if (run && run.startsWith("approval:")) {
      const rid = run.slice(9);
      // open the governance view by requesting the run behind this approval
      getApproval(rid).then((a) => loadRun(a.run_id)).catch(() => { /* noop */ });
    } else if (run) {
      loadRun(run);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep a parked (AWAITING_APPROVAL) run in view polling until it reaches a terminal
  // state. Without this, opening a run "View run" from the console (or a deep link)
  // settles on AWAITING_APPROVAL and never shows the officer's approve/reject outcome.
  useEffect(() => {
    if (!snap?.run || snap.run.status !== "AWAITING_APPROVAL") return;
    const rid = snap.run.run_id;
    const t = setInterval(async () => {
      try {
        const s = await getRun(rid);
        if (s.run) setSnap(s);
        if (s.run && ["COMPLETED", "REJECTED", "FAILED"].includes(s.run.status)) {
          clearInterval(t);
          if (s.run.status === "COMPLETED") {
            try { setMemo((await getMemo(rid)).memo_md || null); }
            catch { /* memo not ready yet */ }
          }
        }
      } catch { /* transient — keep polling */ }
    }, 2500);
    return () => clearInterval(t);
  }, [snap?.run?.status, snap?.run?.run_id]);

  async function loadRun(runId: string) {
    setExpandedAudit(new Set());
    try {
      const final = await pollRun(runId, setSnap);
      runIdRef.current = runId;
      // a completed run has a published memo — show it (deep links, approval
      // email, and the resubmit path all land here)
      if (final.run?.status === "COMPLETED") {
        try { setMemo((await getMemo(runId)).memo_md || null); }
        catch { /* memo not ready yet */ }
      }
    } catch (e) { setError((e as Error).message); }
  }

  const onLogin = async (u: string, p: string) => {
    setError(null);
    try {
      setUser(await login(u, p));
    } catch (e) { setError((e as Error).message); }
  };

  const onGenerate = useCallback(async () => {
    if (!user) return;
    const caseId = document.getElementById("caseId") as HTMLSelectElement;
    const amountEl = document.getElementById("amount") as HTMLInputElement;
    const amount = parseInt(amountEl.value || "0", 10);
    if (!amount || amount <= 0) { setError("Enter a valid loan amount"); return; }
    setError(null);
    setBusy(true);
    setMemo(null);
    try {
      // stream live progress into the workflow panel while the governed run
      // executes; resolves as soon as the run reaches a stable state
      // (terminal OR AWAITING_APPROVAL).
      const snap = await startRun(caseId.value, amount, setSnap);
      runIdRef.current = snap.run!.run_id;
      // show the memo once the run has produced it (after approval / completion)
      const refreshMemo = async () => {
        try {
          const m = await getMemo(snap.run!.run_id);
          setMemo(m.memo_md || null);
        } catch { /* memo not ready yet */ }
      };
      if (snap.run?.status === "COMPLETED") await refreshMemo();
    } catch (e) { setError((e as Error).message); }
    setBusy(false);
  }, [user]);

  const onResubmit = async () => {
    if (!snap?.approval_request) return;
    setError(null);
    try {
      const r = await resubmit(snap.approval_request.request_id);
      await loadRun(r.run_id);
    } catch (e) { setError((e as Error).message); }
  };

  const [chatMsgs, setChatMsgs] = useState<{ who: "user" | "agent"; text: string }[]>([]);
  const [chatText, setChatText] = useState("");
  const [chatBusy, setChatBusy] = useState(false);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  // keep the newest message / thinking indicator in view
  useEffect(() => {
    const el = chatBodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chatMsgs.length, chatBusy]);
  const onChat = async (text: string) => {
    const q = text.trim();
    if (!q || chatBusy) return;
    setChatMsgs((m) => [...m, { who: "user", text: q }]);
    setChatBusy(true);
    try {
      const r = await chat(q);
      setChatMsgs((m) => [...m, { who: "agent", text: r.reply }]);
    } catch (e) {
      setChatMsgs((m) => [...m, { who: "agent", text: `error: ${(e as Error).message}` }]);
    } finally {
      setChatBusy(false);
    }
  };

  if (!user) {
    return <Login onLogin={onLogin} error={error} />;
  }

  const isOfficer = user.role === "Senior Credit Officer";
  const run = snap?.run ?? null;
  const isTerminal = run ? ["COMPLETED", "REJECTED", "FAILED"].includes(run.status) : false;
  const isAwaiting = run?.status === "AWAITING_APPROVAL";

  return (
    <div className="app">
      <Header user={user} onLogout={logout} />
      <div className="layout">
        <div className="main">
          {error && <div className="banner err">⚠ {error}</div>}

          {isAwaiting && snap?.approval_request && (
            <div className="gov">
              <h2>Governance Review In Progress</h2>
              <div>Request ID: <span className="rid">{snap.approval_request.request_id}</span></div>
              <p>The policy gate paused this run: a Senior Credit Officer must approve before the
                memo is published. A signed approval email has been sent to the officer — the
                decision can be recorded from the email or from the officer's console in this portal.
                This page will automatically continue once approval is granted, or you can re-submit
                manually after approval.</p>
              <div className="actions">
                {isOfficer && (
                  <a className="btn-primary btn-blue" href={mailUrl()} target="_blank" rel="noreferrer">
                    📧 View approval email (inbox)
                  </a>
                )}
                <button className="btn-primary btn-green" onClick={onResubmit} disabled={busy}>
                  ↻ Re-submit (Approved)
                </button>
              </div>
            </div>
          )}

          {run ? (
            <>
              {fromConsole && (
                <button className="back-btn" onClick={() => {
                  setSnap(null);
                  setMemo(null);
                  setExpandedAudit(new Set());
                  setFromConsole(false);
                  if (window.location.search) {
                    history.replaceState({}, "", window.location.pathname);
                  }
                }}>
                  ← Back to Approval Queue
                </button>
              )}
              {!isTerminal && (
                <div className="banner"><span className="spin">◌</span> Governed run in progress…</div>
              )}
              {run.status === "COMPLETED" && (
                <div className="banner ok">✔ Loan verification completed</div>
              )}
              {run.status === "REJECTED" && (
                <div className="banner err">✖ Loan verification rejected</div>
              )}
              {run.status === "FAILED" && (
                <div className="banner err">✖ Run failed: {run.fail_reason}</div>
              )}

              <ArchitectureStrip />

              {isTerminal && <DecisionSummary snap={snap!} memo={memo} />}

              <div className="card">
                <h2>Workflow Progress</h2>
                <ul className="steps">
                  {(snap?.steps ?? []).map((s) => {
                    const meta = STEP_META[s.seq];
                    return (
                      <li key={s.seq} className={s.state}>
                        <span className={`dot ${s.state}`}>
                          {s.state === "done" ? "✓" : s.state === "active" ? <span className="pulse" /> : s.state === "failed" ? "✖" : ""}
                        </span>
                        <div className="step-txt">
                          <span className="lbl">{s.name}
                            {meta && <span className="step-actor">{meta.actor}</span>}
                          </span>
                          {meta && <span className="step-why">{meta.why}</span>}
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </div>

              {memo && (
                <div className="card memo">
                  <h2>Generated Memo</h2>
                  <MemoBody md={memo} />
                  <MemoProvenance snap={snap!} />
                </div>
              )}

              {isTerminal && (
                <div className="card">
                  <h2>Run Summary</h2>
                  <div className="kv"><span className="k">Status</span>
                    <span className={`status-pill ${run.status === "COMPLETED" ? "ok" : "err"}`}>{run.status}</span></div>
                  <div className="kv"><span className="k">Case ID</span><span>{run.case_id}</span></div>
                  <div className="kv"><span className="k">Run ID</span><span>{run.run_id}</span></div>
                  <div className="kv"><span className="k">Requested by</span><span>{run.requested_by} ({run.requested_emp_id})</span></div>
                  {run.approved_by && <div className="kv"><span className="k">Approved by</span><span>{run.approved_by} ({run.approval_role ?? "Senior Credit Officer"})</span></div>}
                </div>
              )}

              {isTerminal && (
                <div className="card">
                  <h2>Audit Trail</h2>
                  <div className="audit-note">
                    Every entry explains <b>what</b> happened, <b>why</b> it was needed, and
                    <b> what resulted</b> — click a row for the full evidence (actor, tool, source,
                    latency, model, decision path).
                  </div>
                  <ul className="audit-list">
                    {(snap?.audit ?? []).map((a, i) => {
                      const open = expandedAudit.has(i);
                      const hasInfo = Boolean((a.detail && a.detail.length > 0) || (a.reason && a.reason.length > 0));
                      const metaEntries = Object.entries(a.meta ?? {}).filter(([, v]) => v !== null && v !== "" && v !== undefined);
                      return (
                        <li key={i} className={open ? "open" : ""}>
                          <span className="n">{i + 1}</span>
                          <div className="audit-main"
                            onClick={() => hasInfo && setExpandedAudit((prev) => {
                              const next = new Set(prev);
                              if (next.has(i)) next.delete(i); else next.add(i);
                              return next;
                            })}>
                            <span className="audit-action">{a.action}</span>
                            {a.detail && <span className="audit-detail-inline">{a.detail}</span>}
                            {hasInfo && (
                              <span className="audit-toggle" aria-expanded={open}>{open ? "−" : "+"}</span>
                            )}
                          </div>
                          <span className="ts">{fmtTs(a.ts)}</span>
                          {open && (
                            <div className="audit-detail">
                              <div className="d-row"><span className="d-k">Actor</span><span>{a.actor}</span></div>
                              <div className="d-row"><span className="d-k">Why</span><span>{a.reason || "—"}</span></div>
                              <div className="d-row"><span className="d-k">Result</span><span>{a.detail || "—"}</span></div>
                              {metaEntries.length > 0 && (
                                <div className="d-row"><span className="d-k">Evidence</span>
                                  <span className="evid">
                                    {metaEntries.map(([k, v]) => (
                                      <span className="ev" key={k}><b>{k}</b>: {String(v ?? "—")}</span>
                                    ))}
                                  </span>
                                </div>
                              )}
                              <div className="d-row"><span className="d-k">Timestamp</span><span className="mono">{a.ts}</span></div>
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </>
          ) : isOfficer ? (
            <ApproverConsole onOpenRun={(rid) => { setFromConsole(true); loadRun(rid); }} />
          ) : (
            <CaseForm cases={CASES} user={user} onGenerate={onGenerate} busy={busy} />
          )}
        </div>
        {chatOpen && (
          <div className="chat">
            <div className="chat-head">
              <span>SLVD Assistant</span>
              <span style={{ cursor: "pointer" }} onClick={() => setChatOpen(false)}>✕</span>
            </div>
            <div className="chat-body" ref={chatBodyRef}>
              {chatMsgs.length === 0 && <div className="placeholder">I'm the SLVD demo assistant. Ask me how this demo works, about its components, the approval policy, the roles — or about a case like CR-2026-00451. You can also ask me to generate a memo.</div>}
              {chatMsgs.map((m, i) => (
                <div key={i} className={`msg ${m.who}`}>
                  <div className="who">{m.who === "user" ? user.name : "SLVD Assistant"}</div>
                  <div className="bubble">{m.text}</div>
                </div>
              ))}
              {chatBusy && (
                <div className="msg agent">
                  <div className="who">SLVD Assistant</div>
                  <div className="bubble thinking">thinking…</div>
                </div>
              )}
            </div>
            <div className="chat-input">
              <input value={chatText} placeholder={chatBusy ? "Assistant is thinking…" : "Ask about a case, client, or request a memo…"}
                disabled={chatBusy}
                onChange={(e) => setChatText(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && chatText.trim()) { onChat(chatText.trim()); setChatText(""); } }} />
              <button className="send" disabled={chatBusy || !chatText.trim()}
                onClick={() => { if (chatText.trim()) { onChat(chatText.trim()); setChatText(""); } }}>➤</button>
            </div>
          </div>
        )}
      </div>
      {!chatOpen && <button onClick={() => setChatOpen(true)}
        style={{ position: "fixed", right: 16, bottom: 16, background: "#16294f", color: "#fff", border: 0, borderRadius: 8, padding: "8px 14px", cursor: "pointer" }}>💬 SLVD Assistant</button>}
    </div>
  );
}

function MemoProvenance({ snap }: { snap: RunSnapshot }) {
  const audit = snap.audit ?? [];
  const draft = audit.find((a) => a.action === "Draft memo saved");
  const systems = (audit.find((a) => a.action === "Systems accessed")?.meta?.systems as string[] | undefined) ?? [];
  const decision = audit.find((a) => a.action.startsWith("Decision recorded"));
  const published = audit.find((a) => a.action === "Memo published");
  const backend = (draft?.meta?.backend as string) ?? "";
  const model = draft?.meta?.model as string | undefined;
  return (
    <div className="provenance">
      <div className="section-label">PROVENANCE — HOW THIS MEMO WAS PRODUCED</div>
      <div className="prov-flow">
        <span className="prov">🗄 {systems.join(" · ")} <i>via governed MCP</i></span>
        <span className="prov-arrow">→</span>
        <span className="prov">🤖 credit-memo-agent <i>{backend}{model ? ` (${model})` : ""}</i></span>
        <span className="prov-arrow">→</span>
        <span className="prov">🛡 policy + approval <i>{decision ? decision.detail : "auto"}</i></span>
        <span className="prov-arrow">→</span>
        <span className="prov">📄 published <i>{String(published?.meta?.path ?? "draft")}</i></span>
      </div>
      {Boolean(draft?.meta?.reprompted) && (
        <div className="prov-note">⚠ The LLM's first draft failed validation and was re-prompted once before it passed.</div>
      )}
    </div>
  );
}

// ---------- approver console (sarah) ----------

function ApproverConsole({ onOpenRun }: { onOpenRun: (runId: string) => void }) {
  const [items, setItems] = useState<ApprovalItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejecting, setRejecting] = useState<string | null>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [note, setNote] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  async function refresh() {
    try {
      const r = await getPendingApprovals();
      setItems(r.items);
      setErr(null);
    } catch (e) { setErr((e as Error).message); }
    finally { setLoading(false); }
  }
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function toggleDetail(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  async function decide(item: ApprovalItem, decision: "approve" | "reject") {
    const rsn = (reasons[item.request_id] ?? "").trim();
    if (decision === "reject" && !rsn) {
      setNote({ kind: "err", text: "Enter a reason before rejecting." });
      return;
    }
    setBusyId(item.request_id);
    setNote(null);
    try {
      const r = await decideApproval(item.request_id, decision, rsn);
      if (r.status === decision) {
        setNote({ kind: "ok", text: decision === "approve"
          ? `Approved ${item.case_id} — the run is completing and the memo is being published.`
          : `Rejected ${item.case_id}. The run is now closed (REJECTED); the analyst can re-submit. Open "View run" to confirm.` });
      } else {
        setNote({ kind: "ok", text: `${item.case_id} was already decided — no change made.` });
      }
      setRejecting(null);
      setReasons((prev) => { const n = { ...prev }; delete n[item.request_id]; return n; });
      await refresh();
    } catch (e) { setNote({ kind: "err", text: (e as Error).message }); }
    finally { setBusyId(null); }
  }

  return (
    <>
      <div className="card">
        <h2>Governance Console</h2>
        <div className="section-label">SENIOR CREDIT OFFICER — {`Sarah Chen (E200145)`}</div>
        <p className="console-lead">
          Runs that cross the approval policy (amount threshold and/or KYC pending) pause here.
          Review the case and policy trigger, then record your decision — it is timestamped in the
          audit trail under your identity.
        </p>
      </div>
      <div className="card">
        <h2>Approval Queue <span className={`qcount ${items.length ? "on" : ""}`}>{items.length}</span></h2>
        {err && <div className="banner err">⚠ {err}</div>}
        {loading && <div className="banner">Loading queue…</div>}
        {!loading && !err && items.length === 0 && (
          <div className="queue-empty">✔ No pending approvals — everything that needs sign-off has been decided.</div>
        )}
        {items.map((it) => {
          const open = expanded.has(it.request_id);
          return (
            <div className="queue-item" key={it.request_id}>
              <div className="qi-head">
                <span className="qi-case">{it.case_id}</span>
                <span className="qi-client">{it.client} ({it.client_code})</span>
                <span className="qi-amount">${it.amount_usd.toLocaleString()}</span>
              </div>
              <div className="qi-meta">
                <span>Requested by {it.requested_by} ({it.requested_emp_id})</span>
                <span>· {fmtTs(it.created_at)}</span>
              </div>
              {it.policy_reasons.length > 0 && (
                <div className="qi-policy">
                  <b>Policy:</b> {it.policy_reasons.join(" + ")}
                </div>
              )}
              {/* Collapsible approval details — same content as the approval email */}
              <div className="qi-toggle" onClick={() => toggleDetail(it.request_id)}>
                <span>{open ? "▾" : "▸"}</span> Approval details
                <span className="qi-toggle-hint">{open ? "hide" : "show case & governance context"}</span>
              </div>
              {open && (
                <div className="qi-details">
                  <div className="qd-section">
                    <div className="qd-label">CASE SUMMARY</div>
                    <div className="qd-kv"><span>Case ID</span><span>{it.case_id}</span></div>
                    <div className="qd-kv"><span>Client</span><span>{it.client} ({it.client_code})</span></div>
                    <div className="qd-kv"><span>Facility</span><span>{it.facility ?? "—"}</span></div>
                    <div className="qd-kv"><span>Requested renewal</span><span>${it.amount_usd.toLocaleString()}</span></div>
                    <div className="qd-kv"><span>Current utilization</span>
                      <span>${(it.utilization_usd ?? 0).toLocaleString()} ({it.utilization_pct ?? "—"}% of ${(it.limit_usd ?? 0).toLocaleString()} limit)</span></div>
                    <div className="qd-kv"><span>Risk rating</span><span>{it.risk_rating ?? "—"}</span></div>
                    <div className="qd-kv"><span>Covenants</span><span>{it.covenant_status ?? "—"}</span></div>
                    <div className="qd-kv"><span>KYC / sanctions</span>
                      <span>KYC {it.kyc_status ?? "—"} ({it.kyc_detail ?? "—"}); sanctions {it.sanctions ?? "—"}</span></div>
                  </div>
                  <div className="qd-section">
                    <div className="qd-label">WHY APPROVAL IS REQUIRED (POLICY)</div>
                    <ul className="qd-policy-list">
                      {(it.policy_reasons.length ? it.policy_reasons : ["No rule triggered — auto-approved by policy."])
                        .map((r, i) => <li key={i}>{r}</li>)}
                    </ul>
                  </div>
                  <div className="qd-section">
                    <div className="qd-label">GOVERNANCE CONTEXT</div>
                    <div className="qd-kv"><span>Requested by</span><span>Analyst (employee ID {it.requested_emp_id})</span></div>
                    <div className="qd-kv"><span>Agent</span><span>{it.agent ?? "credit-memo-agent"}</span></div>
                    <div className="qd-kv"><span>Governed MCP method</span>
                      <span className="mono">{it.tool_host ?? "credit-memo-mcp/workflow__submit_credit_memo"}</span></div>
                    <div className="qd-kv"><span>Reason</span>
                      <span>Agent requires approval to invoke governed MCP method: {it.tool_host ?? "workflow__submit_credit_memo"}</span></div>
                    <div className="qd-kv"><span>Approval request ID</span>
                      <span className="mono">{it.request_id}</span></div>
                    <div className="qd-kv"><span>Valid for</span><span>24 hours from issue</span></div>
                  </div>
                  <div className="qd-note">
                    Approving publishes the memo to the official credit record and notifies the
                    client. Rejecting closes the run with your reason logged in the audit trail.
                  </div>
                </div>
              )}
              {rejecting === it.request_id && (
                <div className="qi-reason">
                  <input value={reasons[it.request_id] ?? ""}
                    onChange={(e) => setReasons((prev) => ({ ...prev, [it.request_id]: e.target.value }))}
                    placeholder="Rejection reason (recorded in the audit trail)…" />
                </div>
              )}
              <div className="qi-actions">
                <button className="btn-primary btn-green" disabled={busyId === it.request_id}
                  onClick={() => decide(it, "approve")}>
                  {busyId === it.request_id ? "Recording…" : "✔ Approve"}
                </button>
                <button className="btn-primary btn-red" disabled={busyId === it.request_id}
                  onClick={() => { setRejecting(rejecting === it.request_id ? null : it.request_id); setNote(null); }}>
                  ✖ Reject
                </button>
                <button className="btn-ghost" onClick={() => onOpenRun(it.run_id)}>
                  👁 View run
                </button>
              </div>
            </div>
          );
        })}
        {note && <div className={`banner ${note.kind === "ok" ? "ok" : "err"}`}>{note.text}</div>}
      </div>
    </>
  );
}

function Header({ user, onLogout }: { user: User; onLogout: () => void }) {
  const initials = user.name.split(" ").map((s) => s[0]).join("").slice(0, 2).toUpperCase();
  const isOfficer = user.role === "Senior Credit Officer";
  return (
    <div className="header">
      <div className="brand"><span className="gear">⚙</span> Credit Risk Portal</div>
      <div className="userbox">
        {isOfficer && (
          <a className="mail-btn" href={mailUrl()} target="_blank" rel="noreferrer"
            title="Open the approval inbox (Mailpit) to view governance emails">
            📧 Approval Inbox
          </a>
        )}
        <div className="avatar">{initials}</div>
        <div style={{ textAlign: "right" }}>
          <div>{user.name}</div>
          <div style={{ fontSize: 11, opacity: .7 }}>{user.employee_id} · {user.role}</div>
        </div>
        <button className="logout" onClick={onLogout}>Logout</button>
      </div>
    </div>
  );
}

function Login({ onLogin, error }: { onLogin: (u: string, p: string) => void; error: string | null }) {
  const [u, setU] = useState("nick");
  const [p, setP] = useState("analyst123");
  return (
    <div className="app" style={{ alignItems: "center", justifyContent: "center", background: "#0f1e3a" }}>
      <div className="card" style={{ width: 400, boxShadow: "0 20px 50px rgba(0,0,0,.4)" }}>
        <h2 style={{ textAlign: "center" }}>Credit Risk Portal</h2>
        <div className="section-label" style={{ textAlign: "center" }}>SIGN IN</div>
        {error && <div className="banner err" style={{ marginBottom: 12 }}>{error}</div>}
        <div className="field"><label>Username</label>
          <input value={u} onChange={(e) => setU(e.target.value)} /></div>
        <div className="field"><label>Password</label>
          <input type="password" value={p} onChange={(e) => setP(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && onLogin(u, p)} /></div>
        <button className="btn-primary" style={{ width: "100%", justifyContent: "center" }} onClick={() => onLogin(u, p)}>
          Sign In
        </button>
        <div className="how" style={{ marginTop: 14 }}>
          Demo credentials — Analyst: <b>nick / analyst123</b> · Senior Credit Officer: <b>sarah / officer123</b>
        </div>
      </div>
    </div>
  );
}

function CaseForm({ cases, user, onGenerate, busy }: { cases: typeof CASES; user: User; onGenerate: () => void; busy: boolean }) {
  const [sel, setSel] = useState(cases[0].id);
  const c = cases.find((x) => x.id === sel)!;
  return (
    <>
      <div className="card">
        <h2>Analyst Portal</h2>
        <div className="section-label">SESSION INFORMATION</div>
        <div className="row"><span className="k">Logged in as</span><span className="v">{user.name}</span></div>
        <div className="row"><span className="k">Role</span><span className="v">{user.role}</span></div>
        <div className="row"><span className="k">Department</span><span className="v">{user.department}</span></div>
        <div className="row"><span className="k">Employee ID</span><span className="v">{user.employee_id}</span></div>
      </div>
      <div className="card">
        <div className="card-head">
          <h2>Case Selection</h2>
          <span className="info-tip">
            <span className="info-icon" aria-label="How this works" role="img" title="How this works">
              <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true" fill="currentColor">
                <circle cx="12" cy="5.2" r="1.9" />
                <rect x="10.1" y="9.2" width="3.8" height="9.4" rx="1.9" />
              </svg>
            </span>
            <span className="info-pop">
              <span className="info-title">HOW THIS WORKS</span>
              <p>When you click <b>Generate</b>, the portal passes your authenticated identity and the selected case ID to the platform, then:</p>
              <ol>
                <li>The <b>workflow engine</b> drives the <b>credit-memo-agent (LLM)</b>, which pulls data through <b>governed MCP tools</b> — every bank-system read (CRM, credit exposure, transactions, compliance, prior memos) is identity-checked and audit-logged.</li>
                <li>The LLM <b>drafts the memo</b> using the verified data.</li>
                <li>The <b>policy engine</b> evaluates it (amount threshold + KYC).</li>
                <li>If approval is required, the workflow <b>pauses</b> until a senior officer approves.</li>
              </ol>
            </span>
          </span>
        </div>
        <div className="field"><label>Case ID</label>
          <select id="caseId" value={sel} onChange={(e) => setSel(e.target.value)}>
            {cases.map((x) => <option key={x.id} value={x.id}>{x.id}</option>)}
          </select>
        </div>
        <div className="row"><span className="k">Client</span><span className="v">{c.client}</span></div>
        <div className="row"><span className="k">Type</span><span className="v">{c.type}</span></div>
        <div className="field"><label>Amount (USD)</label>
          <input id="amount" type="number" defaultValue={c.amount} min={1000} step={1000} /></div>
        <button className="btn-primary" onClick={onGenerate} disabled={busy}>
          {busy ? "Generating…" : "▶ Generate renewal decision memo"}
        </button>
      </div>
    </>
  );
}

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "";
  try {
    return new Date(ts).toISOString().replace("T", " ").slice(0, 19) + "Z";
  } catch { return ts; }
}
