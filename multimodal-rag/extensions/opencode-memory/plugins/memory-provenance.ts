/**
 * Memory Provenance Plugin
 *
 * Auto-attaches git and session provenance to every ``add_memory`` call so
 * memories are traceable to the exact repository state and opencode session
 * that produced them — without the LLM having to pass anything manually.
 *
 * What gets injected into ``metadata``:
 *   - ``git_before``  — HEAD commit at session start (sha / short / subject)
 *   - ``git_after``   — HEAD commit at memory-creation time
 *   - ``git_branch``  — current branch name
 *   - ``git_repo``    — remote URL (or repo root path as fallback)
 *   - ``git_dirty``   — whether the working tree has uncommitted changes
 *   - ``git_diff_stat``— one-line summary of uncommitted changes (when dirty)
 *   - ``session_title``— opencode session title
 *   - ``session_created_at``— ISO timestamp of session creation
 *   - ``project_dir`` — worktree / project root path
 *
 * The remote MCP server (``add_memory``) already stores arbitrary metadata
 * keys verbatim in the Qdrant payload, so no server change is needed for
 * storage.  ``_run_retrieval`` surfaces a compact provenance line in search
 * results.
 *
 * Design notes:
 *   - The ``session.created`` event snapshots HEAD ("before" commit) per
 *     session.  This is the commit that existed *before* the session's
 *     changes.
 *   - At ``add_memory`` time, the current HEAD ("after" commit) is captured.
 *     If the session committed during its run, ``git_before`` and
 *     ``git_after`` differ; if not, they match and ``git_dirty`` /
 *     ``git_diff_stat`` describe the uncommitted working-tree changes.
 *   - All git operations are best-effort: if the directory is not a git
 *     repo (or git is unavailable), provenance fields are simply omitted.
 */

import type { Plugin, PluginInput } from "@opencode-ai/plugin"

type Shell = PluginInput["$"]

// --- Types -----------------------------------------------------------------

interface GitCommit {
  sha: string
  short: string
  subject: string
}

interface SessionProvenance {
  directory: string
  title: string
  createdAt: number
  headBefore: GitCommit | null
}

// --- Git helpers (all best-effort, return null on failure) -----------------

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
  // %H = full SHA, %h = short SHA, %s = subject — tab-separated via %x09
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

const MAX_DIFF_STAT_LEN = 200

async function captureDiffStat($: Shell, cwd: string): Promise<string | null> {
  // Summary of all uncommitted changes (staged + unstaged) relative to HEAD.
  const stat = await git($, cwd, "diff", "HEAD", "--stat")
  if (!stat) return null
  // Keep only the trailing summary line, e.g.
  // "3 files changed, 12 insertions(+), 4 deletions(-)"
  const lines = stat.split("\n").filter((l) => l.trim())
  const summary = lines[lines.length - 1] || stat
  return summary.length > MAX_DIFF_STAT_LEN
    ? summary.slice(0, MAX_DIFF_STAT_LEN) + "\u2026"
    : summary
}

// --- Session tracking ------------------------------------------------------

/** sessionID → provenance snapshot (populated on ``session.created``). */
const sessions = new Map<string, SessionProvenance>()

// --- Plugin ----------------------------------------------------------------

const memoryProvenance: Plugin = async ({ client, $, directory, worktree }) => {
  return {
    // Snapshot HEAD when a session starts — this is the "before" commit.
    event: async ({ event }) => {
      try {
        if (event.type === "session.created") {
          const info = event.properties.info
          const sid = info.id
          const dir = info.directory || directory
          sessions.set(sid, {
            directory: dir,
            title: info.title || "",
            createdAt: info.time?.created ?? Date.now(),
            headBefore: await captureHead($, dir),
          })
        } else if (event.type === "session.deleted") {
          sessions.delete(event.properties.info.id)
        }
      } catch {
        // Event handling must never break the session.
      }
    },

    // Inject git + session provenance into add_memory calls before they
    // reach the remote MCP server.
    "tool.execute.before": async (input, output) => {
      if (input.tool !== "rag-memory_add_memory") return
      if (!output.args || typeof output.args !== "object") return

      try {
        const sid = input.sessionID
        const sess = sessions.get(sid)
        const dir = sess?.directory || directory

        // Capture current git state (all commands run in parallel).
        const [headAfter, branch, repoRoot, remoteUrl, dirty, diffStat] = await Promise.all([
          captureHead($, dir),
          captureBranch($, dir),
          captureRepoRoot($, dir),
          captureRemoteUrl($, dir),
          isDirty($, dir),
          captureDiffStat($, dir),
        ])

        // Fetch the current session title (may have been updated since
        // session.created, e.g. auto-titled from the first message).
        let title = sess?.title || ""
        try {
          const res = await client.session.get({ path: { id: sid } })
          if (res.data?.title) title = res.data.title
        } catch {
          // fall back to whatever we captured at session.created
        }

        const provenance: Record<string, unknown> = {
          session_title: title,
          session_created_at: sess
            ? new Date(sess.createdAt).toISOString()
            : undefined,
          project_dir: worktree || directory,
          git_branch: branch,
          git_repo: remoteUrl || repoRoot,
          git_dirty: dirty,
        }

        if (sess?.headBefore) provenance.git_before = sess.headBefore
        if (headAfter) provenance.git_after = headAfter
        if (dirty && diffStat) provenance.git_diff_stat = diffStat

        // Drop undefined values so the payload stays lean.
        for (const k of Object.keys(provenance)) {
          if (provenance[k] === undefined) delete provenance[k]
        }

        // Merge provenance into the caller's metadata without clobbering
        // fields the LLM set explicitly (explicit > auto-injected).
        const existing = output.args.metadata
        if (existing && typeof existing === "object" && !Array.isArray(existing)) {
          output.args.metadata = { ...provenance, ...existing }
        } else {
          output.args.metadata = provenance
        }
      } catch {
        // Provenance injection is best-effort; never block the memory write.
      }
    },
  }
}

export default memoryProvenance