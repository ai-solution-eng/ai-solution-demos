/**
 * Session Memory Logger Plugin
 *
 * Automatically writes a detailed, structured history of every opencode
 * session to the rag-memory dataset shortly after the conversation goes
 * quiet.
 *
 * Why this exists: the agent-facing ``add_memory`` tool is deliberately
 * "concise, not a raw transcript", so LLM-curated memories end up slim.
 * This plugin instead reconstructs the *actual* session — every user
 * prompt, assistant response, tool call and file change — straight from
 * the session's message parts, and persists it as a
 * ``kind: "session_history"`` memory. Future sessions can recall it via
 * ``search_memory`` to see what happened and what changed in a codebase.
 *
 * Design notes:
 *   - Trigger: ``session.status`` events. A ``busy`` status cancels any
 *     pending write (a new turn started); an ``idle`` status schedules a
 *     debounced write. One history is written ~45s after the conversation
 *     stops, not after every turn.
 *   - Exit flush: the plugin's ``dispose`` hook (and the
 *     ``server.instance.disposed`` event) flush any still-pending sessions,
 *     so quitting opencode right after a turn doesn't lose that history.
 *   - Liveness guard: the plugin only considers sessions for which it saw a
 *     ``message.updated`` event in this process, so reopening opencode over
 *     old sessions does not re-write their histories.
 *   - De-duplication: the plugin remembers the last persisted message ID per
 *     session and skips if nothing new arrived. The server additionally
 *     drops near-duplicate text (cosine >= RAG_DEDUP_THRESHOLD).
 *   - Transport: the memory MCP server is called directly over its
 *     streamable-HTTP JSON-RPC endpoint (``tools/call add_memory``) using
 *     the same ``X-Memory-Dataset`` / ``X-Dataset-Password`` headers the MCP
 *     config already sends. TLS trusts the private CA via the
 *     ``NODE_EXTRA_CA_CERTS`` env var that opencode already requires to
 *     reach these servers. Everything is best-effort; diagnostics go to a
 *     log file (never the terminal, which would overlap the TUI).
 *
 * Shutdown safety: the whole shutdown flush is bounded by FLUSH_TIMEOUT_MS
 * and each POST is aborted after POST_TIMEOUT_MS, so a hung or unreachable
 * memory server can never block opencode from exiting.
 */

import type { Plugin, PluginInput } from "@opencode-ai/plugin"

type Shell = PluginInput["$"]

// --- Logger ------------------------------------------------------------------
// Plugins must never write to the terminal — opencode's TUI renders it and any
// stray output overlaps/corrupts the display. Diagnostics go to a file instead:
// ~/.local/share/opencode/log/session-memory.log (override the directory with
// SESSION_MEMORY_LOG_DIR). Lines are buffered and appended in ONE shell call at
// the end of each write (no per-line subprocesses).

function logFile(): string {
  const dir = process.env.SESSION_MEMORY_LOG_DIR
    || [process.env.HOME || "/tmp", ".local", "share", "opencode", "log"].join("/")
  return dir + "/session-memory.log"
}

const logBuffer: string[] = []

function logMsg(level: string, message: string): void {
  logBuffer.push(`[${new Date().toISOString()}] ${level} ${message}`)
}

async function flushLogs($: Shell): Promise<void> {
  if (logBuffer.length === 0) return
  const lines = logBuffer.splice(0)
  try {
    const file = logFile()
    await $`mkdir -p ${file.slice(0, file.lastIndexOf("/"))}`.nothrow().quiet()
    await $`echo ${lines.join("\n")} >> ${file}`.nothrow().quiet()
  } catch {
    // Logging must never break the session.
  }
}

// --- Tunables ---------------------------------------------------------------

const DEBOUNCE_MS = 45_000 // how long a session must be quiet before we write
const MAX_TOOL_OUTPUT = 400 // per-tool output/title excerpt
const MAX_COMMAND = 240 // bash command excerpt
const MAX_TEXT = 8_000 // per assistant/user message body cap
const MAX_HISTORY = 150_000 // total history cap (oldest messages trimmed first)
const POST_TIMEOUT_MS = 20_000 // abort a single memory-server POST after this
// The shutdown flush must be LONGER than POST_TIMEOUT_MS or it gives up
// before a slow replace+embed POST can finish (the flush aborts the write).
const FLUSH_TIMEOUT_MS = 45_000 // abort the shutdown flush after this total
// The MCP server splits long histories into ≤MEMORY_MAX_TOKENS (default 8192)
// token chunks, each prefixed with the header — so a big payload here just
// means more retained chunks, not a longer single document.

/** Read-only / administrative tools that add noise to a history. */
const NOISE_TOOLS = new Set([
  "read",
  "glob",
  "grep",
  "list",
  "todowrite",
  "question",
  "session-id",
  "describe_media",
  "list_datasets",
  "get_dataset_files",
  "get_dataset_info",
  "get_dataset",
  "unlock_dataset",
  "list_mcp_resources",
  "list_mcp_resource_templates",
  "read_mcp_resource",
  "run_kubectl",
  "get_resource",
  "list_pods",
])

// --- Types -------------------------------------------------------------------

interface GitCommit {
  sha: string
  short: string
  subject: string
}

type Part = {
  id: string
  type: string
  text?: string
  synthetic?: boolean
  ignored?: boolean
  tool?: string
  state?: {
    status?: string
    title?: string
    error?: string
    input?: Record<string, unknown>
  }
  files?: Array<string>
}

type MessageRow = {
  info: {
    id: string
    role: "user" | "assistant"
    time?: { created?: number }
    agent?: string
    providerID?: string
    modelID?: string
    model?: { providerID?: string; modelID?: string }
  }
  parts?: Array<Part>
}

// --- Git helpers (all best-effort, return null on failure) ------------------

async function git($: Shell, cwd: string, ...args: string[]): Promise<string | null> {
  try {
    const out = await $`git ${args}`.cwd(cwd).quiet().nothrow().text()
    const text = out.trim()
    return text || null
  } catch {
    return null
  }
}

async function captureHead($: Shell, cwd: string): Promise<GitCommit | null> {
  const line = await git($, cwd, "log", "-1", "--format=%H%x09%h%x09%s")
  if (!line) return null
  const [sha, short, ...rest] = line.split("\t")
  if (!sha) return null
  return { sha, short: short || sha.slice(0, 8), subject: rest.join("\t") || "" }
}

async function captureBranch($: Shell, cwd: string): Promise<string | null> {
  return git($, cwd, "rev-parse", "--abbrev-ref", "HEAD")
}

async function captureRepoRoot($: Shell, cwd: string): Promise<string | null> {
  return git($, cwd, "rev-parse", "--show-toplevel")
}

async function captureRemoteUrl($: Shell, cwd: string): Promise<string | null> {
  return git($, cwd, "remote", "get-url", "origin")
}

async function isDirty($: Shell, cwd: string): Promise<boolean> {
  const status = await git($, cwd, "status", "--porcelain")
  return status !== null && status.length > 0
}

async function captureDiffStat($: Shell, cwd: string): Promise<string | null> {
  const stat = await git($, cwd, "diff", "HEAD", "--stat")
  if (!stat) return null
  const lines = stat.split("\n").filter((l) => l.trim())
  const summary = lines[lines.length - 1] || stat
  return summary.length > 200 ? summary.slice(0, 200) + "\u2026" : summary
}

// --- Small helpers -----------------------------------------------------------

function truncate(s: string, n: number): string {
  const t = s.trim()
  return t.length > n ? t.slice(0, n) + "\u2026" : t
}

function formatTime(ts: number | undefined): string {
  if (!ts) return ""
  return new Date(ts).toISOString()
}

function collectFiles(rows: Array<MessageRow>): Array<string> {
  const files = new Set<string>()
  for (const row of rows) {
    for (const part of row.parts ?? []) {
      if (part.type === "patch" && Array.isArray(part.files)) {
        for (const f of part.files) files.add(f)
      } else if (part.type === "tool" && part.state) {
        const st = part.state
        if (st.status !== "completed" && st.status !== "error") continue
        if (part.tool !== "edit" && part.tool !== "write") continue
        const input = st.input ?? {}
        const fp = input.filePath ?? input.path
        if (typeof fp === "string") files.add(fp)
      }
    }
  }
  return [...files].sort()
}

function summarizeTools(parts: Array<Part>): Array<string> {
  const out: string[] = []
  for (const part of parts) {
    if (part.type !== "tool" || !part.state) continue
    const st = part.state
    if (st.status !== "completed" && st.status !== "error") continue
    const name = part.tool ?? ""
    if (NOISE_TOOLS.has(name)) continue
    const input = st.input ?? {}
    let line = ""
    if (name === "edit" || name === "write") {
      const fp = input.filePath ?? input.path
      line = `\`${name}\` ${truncate(String(fp ?? ""), 160)}\``
    } else if (name === "bash") {
      line = `\`bash\` \`${truncate(String(input.command ?? ""), MAX_COMMAND)}\``
    } else if (name === "task") {
      line = `\`task\` ${truncate(String(input.description ?? st.title ?? ""), MAX_TOOL_OUTPUT)}`
    } else {
      line = `\`${name}\` ${truncate(String(st.title ?? ""), MAX_TOOL_OUTPUT)}`
    }
    if (st.status === "error") {
      line += ` (FAILED: ${truncate(String(st.error ?? ""), 200)})`
    }
    out.push(line)
  }
  return out
}

function renderMessage(row: MessageRow, lines: string[]): void {
  const info = row.info
  const when = formatTime(info.time?.created)
  const parts = row.parts ?? []

  if (info.role === "user") {
    const body = parts
      .filter((p) => p.type === "text" && !p.synthetic)
      .map((p) => p.text ?? "")
      .join("\n")
      .trim()
    if (!body) return
    lines.push(`### User${when ? ` \u2014 ${when}` : ""}`)
    lines.push(truncate(body, MAX_TEXT))
    lines.push("")
    return
  }

  const model = info.modelID
    ? `${info.providerID ?? ""}/${info.modelID}`
    : info.agent
      ? `agent:${info.agent}`
      : ""
  lines.push(`### Assistant${when ? ` \u2014 ${when}` : ""}${model ? ` (${model})` : ""}`)

  const actions = summarizeTools(parts)
  if (actions.length) {
    lines.push("**Actions:**")
    lines.push(...actions)
    lines.push("")
  }

  const body = parts
    .filter((p) => p.type === "text" && !p.synthetic && !p.ignored)
    .map((p) => p.text ?? "")
    .join("\n")
    .trim()
  if (body) {
    lines.push(truncate(body, MAX_TEXT))
    lines.push("")
  }
}

function buildHistory(
  sid: string,
  title: string,
  rows: Array<MessageRow>,
  provenance: Record<string, unknown>,
): string | null {
  const userMessages = rows.filter((r) => r.info.role === "user")
  const assistantMessages = rows.filter((r) => r.info.role === "assistant")
  if (userMessages.length === 0 || assistantMessages.length === 0) return null

  const files = collectFiles(rows)
  const started = rows[0]?.info?.time?.created

  const header: string[] = []
  header.push(`# Session History \u2014 ${title || sid}`)
  header.push("")
  header.push(`- **session:** ${sid}`)
  if (started) header.push(`- **started:** ${formatTime(started)}`)
  for (const [k, v] of Object.entries(provenance)) {
    if (v !== undefined && v !== null && v !== "") {
      header.push(`- **${k}:** ${typeof v === "object" ? JSON.stringify(v) : String(v)}`)
    }
  }
  header.push("")

  const body: string[] = []
  if (files.length) {
    body.push("## Files changed")
    body.push(...files.map((f) => `- \`${f}\``))
    body.push("")
  }

  body.push("## Transcript")
  body.push("")
  for (const row of rows) renderMessage(row, body)

  // Cap total size, dropping the oldest rows first.
  let full = header.join("\n") + "\n" + body.join("\n")
  while (full.length > MAX_HISTORY && rows.length > 2) {
    rows = rows.slice(1)
    body.length = 0
    body.push("## Transcript")
    body.push("")
    for (const row of rows) renderMessage(row, body)
    full = header.join("\n") + "\n" + body.join("\n")
  }
  return full
}

// --- Session state ----------------------------------------------------------

const sessions = new Map<string, { directory: string; title: string }>()
const seen = new Set<string>() // sessions with at least one message this run
const written = new Map<string, { messageID: string }>()
const latestMsg = new Map<string, string>() // newest message id observed per session
const timers = new Map<string, ReturnType<typeof setTimeout>>()
// De-dupe concurrent writes for the same session (e.g. a pending debounce
// firing at the same time as the dispose/exit flush) into a single POST.
const inFlight = new Map<string, Promise<void>>()

// --- Plugin ------------------------------------------------------------------

const sessionMemoryLogger: Plugin = async ({ client, $, directory, worktree }) => {
  let memoryUrl =
    process.env.RAG_MEMORY_URL ||
    "https://rag-mcp-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp"
  const dataset = process.env.RAG_MEMORY_DATASET
  const password = process.env.RAG_MEMORY_PASSWORD || ""

  return {
    // Grab the configured rag-memory MCP URL from the merged config so we
    // don't hard-code an endpoint that may change.
    config: async (cfg) => {
      const ragMemory = (cfg.mcp as Record<string, { url?: string }> | undefined)?.["rag-memory"]
      if (ragMemory?.url) memoryUrl = ragMemory.url
    },

    event: async ({ event }) => {
      try {
        switch (event.type) {
          case "session.created": {
            const info = event.properties.info
            if (!info.parentID) {
              sessions.set(info.id, {
                directory: info.directory || directory,
                title: info.title || "",
              })
            }
            break
          }
          case "session.deleted": {
            const sid = event.properties.info.id
            cancelTimer(sid)
            sessions.delete(sid)
            seen.delete(sid)
            written.delete(sid)
            latestMsg.delete(sid)
            break
          }
          case "message.updated": {
            const sid = event.properties.info.sessionID
            if (sid) {
              seen.add(sid)
              latestMsg.set(sid, event.properties.info.id)
            }
            break
          }
          case "server.instance.disposed": {
            await flushPending()
            break
          }
          case "session.status": {
            const { sessionID, status } = event.properties
            if (status.type === "busy") {
              cancelTimer(sessionID)
            } else if (status.type === "idle") {
              schedule(sessionID)
            }
            break
          }
        }
      } catch {
        // Event handling must never break the session.
      }
    },

    // Flush any sessions that still have pending/unwritten histories when
    // opencode shuts down, so quitting right after a turn doesn't lose it.
    dispose: async () => {
      await flushPending()
    },
  }

  function cancelTimer(sid: string): void {
    const t = timers.get(sid)
    if (t) {
      clearTimeout(t)
      timers.delete(sid)
    }
  }

  function schedule(sid: string): void {
    if (!dataset) return
    if (!seen.has(sid)) return
    cancelTimer(sid)
    const timer = setTimeout(() => {
      timers.delete(sid)
      void writeHistory(sid)
    }, DEBOUNCE_MS)
    timers.set(sid, timer)
  }

  /** Flush histories for every session with pending or unwritten work.
   *  Bounded: total runs in parallel and gives up after FLUSH_TIMEOUT_MS,
   *  so a hung/unreachable memory server can never block opencode exit. */
  async function flushPending(): Promise<void> {
    const pending = new Set<string>(timers.keys())
    for (const sid of seen) pending.add(sid)
    const tasks: Array<Promise<void>> = []
    for (const sid of pending) {
      cancelTimer(sid)
      tasks.push(writeHistory(sid))
    }
    await Promise.race([
      Promise.allSettled(tasks),
      new Promise((resolve) => setTimeout(resolve, FLUSH_TIMEOUT_MS)),
    ])
  }

  async function writeHistory(sid: string): Promise<void> {
    const existing = inFlight.get(sid)
    if (existing) return existing
    const run = doWrite(sid)
    inFlight.set(sid, run)
    try {
      await run
    } finally {
      inFlight.delete(sid)
    }
  }

  async function doWrite(sid: string): Promise<void> {
    try {
      // Nothing new since the last write? Skip entirely — avoids re-fetching
      // the whole session (the expensive exit-time cost) when idle re-fires.
      const writtenID = written.get(sid)?.messageID
      if (writtenID && writtenID === latestMsg.get(sid)) return

      const sess = sessions.get(sid)
      const dir = sess?.directory || directory
      const res = await client.session.messages({ path: { id: sid } })
      const rows = (res.data ?? []) as Array<MessageRow>
      if (!Array.isArray(rows) || rows.length === 0) return

      const lastRow = rows[rows.length - 1]
      const lastID = lastRow?.info?.id
      if (written.get(sid)?.messageID === lastID) return

      let title = sess?.title || ""
      try {
        const sessionRes = await client.session.get({ path: { id: sid } })
        if (sessionRes.data?.title) title = sessionRes.data.title
      } catch {
        // fall back to whatever we captured at session.created
      }

      // Capture current git state (all commands run in parallel).
      const [gitAfter, branch, repoRoot, remoteUrl, dirty, diffStat] = await Promise.all([
        captureHead($, dir),
        captureBranch($, dir),
        captureRepoRoot($, dir),
        captureRemoteUrl($, dir),
        isDirty($, dir),
        captureDiffStat($, dir),
      ])
      const provenance: Record<string, unknown> = {
        git_branch: branch,
        git_repo: remoteUrl || repoRoot,
        git_dirty: dirty,
      }
      if (gitAfter) provenance.git_after = gitAfter
      if (dirty && diffStat) provenance.git_diff_stat = diffStat
      for (const k of Object.keys(provenance)) {
        if (provenance[k] === undefined || provenance[k] === null) delete provenance[k]
      }

      const text = buildHistory(sid, title, rows, provenance)
      if (!text) return

      const metadata: Record<string, unknown> = {
        kind: "session_history",
        session_id: sid,
        session_title: title,
        message_count: rows.length,
        tool_count: rows.reduce(
          (n, r) => n + (r.parts ?? []).filter((p) => p.type === "tool").length,
          0,
        ),
        ...provenance,
      }

      const result = await postMemory(text, metadata)
      if (result === "stored" || result === "skipped") {
        written.set(sid, { messageID: lastID })
      }
      if (result === "stored") {
        logMsg("INFO", `stored history for ${sid} (${rows.length} messages, ${title || "untitled"})`)
      } else if (result === "skipped") {
        logMsg("INFO", `duplicate history skipped for ${sid}`)
      }
    } catch (err) {
      logMsg("ERROR", `write failed: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      await flushLogs($)
    }
  }

  /** Returns "stored", "skipped", or "failed" — dedups are not failures.
   *  Bounded by POST_TIMEOUT_MS via AbortController. */
  async function postMemory(
    text: string,
    metadata: Record<string, unknown>,
  ): Promise<"stored" | "skipped" | "failed"> {
    const payload = {
      jsonrpc: "2.0",
      id: 1,
      method: "tools/call",
      params: {
        name: "add_memory",
        arguments: { text, metadata },
      },
    }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), POST_TIMEOUT_MS)
    try {
      const response = await fetch(memoryUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          ...(dataset ? { "X-Memory-Dataset": dataset } : {}),
          ...(password ? { "X-Dataset-Password": password } : {}),
        },
        body: JSON.stringify(payload),
        signal: controller.signal,
      })
      if (!response.ok) {
        logMsg("ERROR", `memory server responded ${response.status}`)
        return "failed"
      }
      const data = (await response.json().catch(() => null)) as {
        result?: { content?: Array<{ text?: string }> }
        error?: { message?: string }
      } | null
      if (data?.error) {
        logMsg("ERROR", `memory server error: ${data.error.message}`)
        return "failed"
      }
      // The server returns {"status":"stored", "stored_ids": [...]}; when a
      // near-duplicate is skipped, stored_ids is empty.
      try {
        const text = data?.result?.content?.[0]?.text ?? ""
        const parsed = JSON.parse(text) as { stored_ids?: Array<string> }
        if (Array.isArray(parsed.stored_ids) && parsed.stored_ids.length === 0) {
          return "skipped"
        }
      } catch {
        // Not JSON — treat as stored.
      }
      return "stored"
    } catch (err) {
      logMsg("ERROR", `failed to reach memory server: ${err instanceof Error ? err.message : String(err)}`)
      return "failed"
    } finally {
      clearTimeout(timer)
    }
  }
}

export default sessionMemoryLogger