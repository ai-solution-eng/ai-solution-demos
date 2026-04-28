const getBaseUrl = (defaultLocal: string) => {
  if (typeof window !== 'undefined') {
    // In browser: if not on localhost, use relative paths to support custom domains via Istio/VirtualService
    if (window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1') {
      return '';
    }
  }
  // Fallback to local development URL
  return defaultLocal;
};

const ADMIN_URL = getBaseUrl('http://localhost:8002');
const LLM_URL = getBaseUrl('http://localhost:8003');

async function safeJson(response: Response) {
  const text = await response.text();
  
  if (!response.ok) {
    let errorMessage = `Request failed (${response.status})`;
    try {
      const errorJson = JSON.parse(text);
      errorMessage = errorJson.message || errorJson.detail || errorMessage;
    } catch {
      // Not JSON, use the raw text if short or generic message
      if (text.length > 0 && text.length < 500) {
        errorMessage = text;
      }
    }
    throw new Error(errorMessage);
  }

  try {
    return JSON.parse(text);
  } catch (_err) {
    console.error('Failed to parse response as JSON:', text);
    throw new Error(`Invalid response from server: ${text.substring(0, 50)}...`);
  }
}

export const api = {
  getDemoConfig: () => fetch(`${ADMIN_URL}/api/v1/admin/demo-config`).then(safeJson),
  saveDemoConfig: (config: Record<string, unknown>) => 
    fetch(`${ADMIN_URL}/api/v1/admin/demo-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(config)
    }).then(safeJson),
  getModels: () => fetch(`${LLM_URL}/api/v1/models`).then(safeJson),
  getDiscoveredEndpoints: () => fetch(`${LLM_URL}/api/v1/llm/discovered-endpoints`).then(safeJson),
  discoverModels: (endpoint: string, api_key?: string) => 
    fetch(`${LLM_URL}/api/v1/llm/discover-models`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ endpoint, api_key })
    }).then(safeJson),
};

