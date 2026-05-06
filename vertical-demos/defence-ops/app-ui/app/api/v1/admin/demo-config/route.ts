import { NextRequest, NextResponse } from "next/server";
import fs from "fs";
import path from "path";

const CONFIG_PATH = process.env.CONFIG_PATH || "/app/data/config.json";

function loadConfig() {
  if (fs.existsSync(CONFIG_PATH)) {
    try {
      const data = fs.readFileSync(CONFIG_PATH, "utf-8");
      return JSON.parse(data);
    } catch (e) {
      console.error("Failed to load config", e);
    }
  }
  return {};
}

function saveConfig(config: Record<string, unknown>) {
  try {
    const dir = path.dirname(CONFIG_PATH);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
  } catch (e) {
    console.error("Failed to save config", e);
  }
}

export async function GET() {
  const config = loadConfig();
  return NextResponse.json({
    status: "success",
    data: config.demo_services || {}
  });
}

export async function POST(request: NextRequest) {
  try {
    const payload = await request.json();
    const current = loadConfig();
    current.demo_services = payload;
    saveConfig(current);
    return NextResponse.json({
      status: "success",
      message: "Configuration saved successfully"
    });
  } catch (err) {
    const errorMessage = err instanceof Error ? err.message : String(err);
    return NextResponse.json({
      status: "error",
      message: `Failed to save configuration: ${errorMessage}`
    }, { status: 500 });
  }
}
