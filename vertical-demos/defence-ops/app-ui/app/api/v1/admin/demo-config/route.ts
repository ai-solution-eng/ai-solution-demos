import { NextRequest, NextResponse } from "next/server";
import { promises as fs } from "fs";
import path from "path";
import { z } from "zod";
import { settingsAccess } from "@/lib/auth";

// Runtime data on the app-data volume, not a build asset: the turbopackIgnore comments below keep
// the build from tracing the whole project because of these dynamic paths.
const CONFIG_PATH = process.env.CONFIG_PATH || "/app/data/config.json";

// System Config holds no secrets: the model endpoint key and the Kafka credentials are Kubernetes
// Secrets that only llm-service and kafka-service read. Saving rewrites demo_services with
// these fields alone, which also drops the keys older versions stored here.
const demoServicesSchema = z.object({
  llm_model: z.string().trim().max(200).default(""),
  kafka_broker: z
    .string()
    .trim()
    .max(500)
    .regex(/^$|^[\w.-]+:\d{1,5}(,[\w.-]+:\d{1,5})*$/, "use host:port, comma-separated")
    .default(""),
  kafka_topic: z
    .string()
    .trim()
    .max(249)
    .regex(/^$|^[\w./:-]+$/, "use letters, digits and . _ - / : only")
    .default(""),
});

type DemoServices = z.infer<typeof demoServicesSchema>;

const storedConfigSchema = z.looseObject({
  demo_services: z.record(z.string(), z.unknown()).optional(),
});

type StoredConfig = z.infer<typeof storedConfigSchema>;

function describe(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

function errorResponse(message: string, status: number) {
  return NextResponse.json({ status: "error", message }, { status });
}

async function loadConfig(): Promise<StoredConfig> {
  let text: string;
  try {
    text = await fs.readFile(/*turbopackIgnore: true*/ CONFIG_PATH, "utf-8");
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code === "ENOENT") return {};
    throw err;
  }
  return storedConfigSchema.parse(JSON.parse(text));
}

async function saveConfig(config: StoredConfig): Promise<void> {
  await fs.mkdir(/*turbopackIgnore: true*/ path.dirname(CONFIG_PATH), { recursive: true });
  // Write a temporary file and rename it, so a failed write never leaves half a file behind.
  const tmpPath = `${CONFIG_PATH}.tmp`;
  await fs.writeFile(/*turbopackIgnore: true*/ tmpPath, JSON.stringify(config, null, 2));
  await fs.rename(/*turbopackIgnore: true*/ tmpPath, /*turbopackIgnore: true*/ CONFIG_PATH);
}

// The chart's defaults (llm.model, kafka.broker, kafka.topic), shown and used until an administrator
// saves a value of their own; common/demo_config.py applies the same rule for the backends.
const DEFAULTS: DemoServices = {
  llm_model: process.env.LLM_MODEL ?? "",
  kafka_broker: process.env.KAFKA_BROKER ?? "",
  kafka_topic: process.env.KAFKA_TOPIC ?? "",
};

function publicSettings(stored: Record<string, unknown>): DemoServices {
  const text = (key: keyof DemoServices) =>
    typeof stored[key] === "string" && stored[key] !== "" ? (stored[key] as string) : DEFAULTS[key];
  return { llm_model: text("llm_model"), kafka_broker: text("kafka_broker"), kafka_topic: text("kafka_topic") };
}

export async function GET(request: NextRequest) {
  try {
    const [config, access] = await Promise.all([loadConfig(), settingsAccess(request.headers)]);
    return NextResponse.json({
      status: "success",
      data: publicSettings(config.demo_services ?? {}),
      meta: { can_edit: access.canEdit, reason: access.reason },
    });
  } catch (err) {
    return errorResponse(
      `Could not read the saved settings from ${CONFIG_PATH}: ${describe(err)}. ` +
        "Check that the app-data volume is mounted and the file holds valid JSON.",
      500,
    );
  }
}

export async function POST(request: NextRequest) {
  const access = await settingsAccess(request.headers);
  if (!access.canEdit) {
    return errorResponse(`Settings not saved: ${access.reason}`, 403);
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return errorResponse("Settings not saved: the request body is not valid JSON.", 400);
  }

  const parsed = demoServicesSchema.safeParse(body);
  if (!parsed.success) {
    const detail = parsed.error.issues
      .map((issue) => `${issue.path.join(".") || "body"}: ${issue.message}`)
      .join("; ");
    return errorResponse(`Settings not saved: ${detail}`, 400);
  }

  try {
    const current = await loadConfig();
    await saveConfig({ ...current, demo_services: parsed.data });
  } catch (err) {
    return errorResponse(
      `Settings not saved: could not write ${CONFIG_PATH} (${describe(err)}). ` +
        "Check that the app-data volume is mounted and writable.",
      500,
    );
  }

  console.info(JSON.stringify({ event: "settings_saved", user: access.user, ...parsed.data }));
  return NextResponse.json({ status: "success", message: "Configuration saved successfully" });
}
