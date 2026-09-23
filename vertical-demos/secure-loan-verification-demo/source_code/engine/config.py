"""Engine configuration via environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # Core
    base_url: str = os.environ.get("SLVD_BASE_URL", "http://localhost:8080")
    data_dir: str = os.environ.get("SLVD_DATA_DIR", "/data")
    jwt_secret: str = os.environ.get("SLVD_JWT_SECRET") or "dev-only-jwt-secret"
    hmac_secret: str = os.environ.get("SLVD_HMAC_SECRET") or "dev-only-hmac-secret"
    jwt_ttl_hours: int = int(os.environ.get("SLVD_JWT_TTL_HOURS", "8"))
    link_ttl_hours: int = int(os.environ.get("SLVD_LINK_TTL_HOURS", "24"))

    # Backends
    agent_backend: str = os.environ.get("SLVD_AGENT_BACKEND", "stub")  # stub|direct_llm|openclaw
    mcp_base_url: str = os.environ.get("SLVD_MCP_BASE_URL", "http://mcp:8000")
    llm_base_url: str = os.environ.get("SLVD_LLM_BASE_URL", "http://litellm:4000/v1")
    llm_api_key: str = os.environ.get("SLVD_LLM_API_KEY", "")
    llm_model: str = os.environ.get("SLVD_LLM_MODEL", "qwen3-8-27b-int4-dflash2")
    llm_timeout_s: float = float(os.environ.get("SLVD_LLM_TIMEOUT_S", "180"))
    # Reasoning effort for reasoning-model backends: "low" (default) or "none".
    # "none" fully disables chain-of-thought (fastest; content-only). On qwen3 this
    # maps to enable_thinking=false. "low" caps the thinking budget. Set to "" to
    # send no reasoning parameter at all (let the model decide).
    llm_reasoning_effort: str = os.environ.get("SLVD_LLM_REASONING_EFFORT", "low")
    max_llm_rounds: int = int(os.environ.get("SLVD_MAX_LLM_ROUNDS", "12"))

    # Simulation
    simulation: bool = _env_bool("SLVD_SIMULATION", False)
    sim_approve_delay_s: float = float(os.environ.get("SLVD_SIM_APPROVE_DELAY", "8"))

    # Mail
    mail_host: str = os.environ.get("SLVD_MAIL_HOST", "mailpit")
    mail_port: int = int(os.environ.get("SLVD_MAIL_PORT", "1025"))
    mail_from: str = os.environ.get("SLVD_MAIL_FROM", "noreply@agentplatform.local")
    approver_email: str = os.environ.get("SLVD_APPROVER_EMAIL", "Sarah.Chen@mybank.com")
    client_email: str = os.environ.get("SLVD_CLIENT_EMAIL", "client@acme-industrial.example")

    # OpenClaw
    # No baked default host: an unset/empty URL is a CONFIG ERROR surfaced at
    # first use (agent.py) — the chart always sets the detected in-cluster URL.
    openclaw_url: str = os.environ.get("SLVD_OPENCLAW_URL", "")
    openclaw_token: str = os.environ.get("SLVD_OPENCLAW_TOKEN", "")

    # Policy
    approval_threshold_usd: int = int(os.environ.get("SLVD_APPROVAL_THRESHOLD_USD", "5000000"))

    # Internal webhook token (MCP -> engine)
    mcp_internal_token: str = os.environ.get("SLVD_MCP_INTERNAL_TOKEN", "")

    @property
    def sqlite_path(self) -> str:
        return os.path.join(self.data_dir, "slvd.db")

    @property
    def drafts_dir(self) -> str:
        d = os.path.join(self.data_dir, "docs", "drafts", "credit")
        os.makedirs(d, exist_ok=True)
        return d

    @property
    def official_dir(self) -> str:
        d = os.path.join(self.data_dir, "docs", "official", "credit", str(_year()))
        os.makedirs(d, exist_ok=True)
        return d


def _year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


settings = Settings()
