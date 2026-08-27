import { tool } from "@opencode-ai/plugin"

/**
 * Returns the current opencode session ID.
 *
 * The session ID is available in the tool execution context and is
 * useful for tagging memories or logging.  The LLM can call this tool
 * to retrieve the ID, then pass it as metadata to rag-memory_add_memory.
 */
export default tool({
  description: "Get the current opencode session ID for tracking/memory purposes.",
  args: {},
  async execute(_args, context) {
    return context.sessionID || "unknown"
  },
})