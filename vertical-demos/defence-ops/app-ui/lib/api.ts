import { z } from "zod";

// All services sit behind the same hostname (Istio VirtualService), so paths are relative.

export const demoConfigSchema = z.object({
  llm_model: z.string(),
  kafka_broker: z.string(),
  kafka_topic: z.string(),
});
export type DemoConfig = z.infer<typeof demoConfigSchema>;

const demoConfigResponse = z.object({
  data: demoConfigSchema,
  meta: z.object({ can_edit: z.boolean(), reason: z.string().nullable() }),
});
export type DemoConfigResponse = z.infer<typeof demoConfigResponse>;

export const llmModelSchema = z.object({
  id: z.string(),
  vision: z.boolean().nullable(),
  video: z.boolean().nullable(),
  reasoning: z.boolean().nullable(),
});
export type LlmModel = z.infer<typeof llmModelSchema>;

const modelsResponse = z.object({
  data: z.object({ models: z.array(llmModelSchema), selected: z.string().nullable() }),
});
export type ModelsResponse = z.infer<typeof modelsResponse>["data"];

export interface Alert {
  timestamp?: number;
  severity: string;
  location?: string;
  message: string;
}

export interface ChatRequest {
  message: string;
  system_prompt: string;
  videos: string[];
  kafka_context: Alert[];
  enable_thinking: boolean;
}

const chatResponse = z.object({
  data: z.object({
    reply: z.string(),
    reasoning: z.string().nullable(),
    model: z.string(),
    context: z.enum(["video", "frames", "none"]),
    thinking: z.boolean(),
    notices: z.array(z.string()),
  }),
});
export type ChatReply = z.infer<typeof chatResponse>["data"];

const messageResponse = z.object({ message: z.string() });

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// Next.js routes answer {message}; FastAPI answers {detail}, a string or a validation list.
function errorMessage(body: unknown): string | undefined {
  if (typeof body !== "object" || body === null) return undefined;
  const record = body as Record<string, unknown>;
  if (typeof record.message === "string") return record.message;
  if (typeof record.detail === "string") return record.detail;
  if (Array.isArray(record.detail)) {
    return record.detail
      .map((d) => (typeof d === "object" && d !== null && "msg" in d ? String((d as { msg: unknown }).msg) : ""))
      .filter(Boolean)
      .join("; ");
  }
  return undefined;
}

async function request<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(`Could not reach ${path}; check the connection and reload.`, 0);
  }
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    throw new ApiError(errorMessage(body) ?? `Request to ${path} failed (HTTP ${res.status})`, res.status);
  }
  const parsed = schema.safeParse(body);
  if (!parsed.success) {
    throw new ApiError(`Unexpected response from ${path}`, res.status);
  }
  return parsed.data;
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  getDemoConfig: () => request("/api/v1/admin/demo-config", demoConfigResponse),
  saveDemoConfig: (config: DemoConfig) =>
    request("/api/v1/admin/demo-config", messageResponse, json(config)).then((r) => r.message),
  getModels: (refresh = false) =>
    request(`/api/v1/llm/models${refresh ? "?refresh=true" : ""}`, modelsResponse).then((r) => r.data),
  chat: (payload: ChatRequest) => request("/api/v1/llm/chat", chatResponse, json(payload)).then((r) => r.data),
};
