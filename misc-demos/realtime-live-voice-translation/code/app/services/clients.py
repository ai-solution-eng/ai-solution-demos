import httpx
from openai import AsyncOpenAI

# Bounds every ASR/LLM call: without these the SDK defaults apply — a 600s read
# timeout and max_retries=2 (a silent full-WAV re-upload on 429/5xx can triple
# latency). The live pipeline is single-flight per presenter, so one wedged
# call freezes that room's translation for the whole timeout; 20s bounds the
# blast radius while staying far above realistic Whisper/vLLM latencies.
_HTTP_TIMEOUT_SECONDS = 20.0
_HTTP_CONNECT_TIMEOUT_SECONDS = 2.0
_MAX_RETRIES = 1

# Connection pooling: reuse one AsyncOpenAI client per (base_url, api_key)
# instead of one per WebSocket connection. Keeps TCP+TLS pools warm across
# connections and config changes (~50-300ms saved on the first utterance of
# every connection), and avoids discarding pools on config messages that
# don't actually change the endpoint.
_CLIENT_CACHE: dict[tuple[str, str], AsyncOpenAI] = {}


def make_client(base_url: str, api_key: str) -> AsyncOpenAI:
    key = (base_url or "", api_key or "")
    client = _CLIENT_CACHE.get(key)
    if client is None:
        client = AsyncOpenAI(
            base_url=key[0],
            api_key=key[1],
            timeout=httpx.Timeout(_HTTP_TIMEOUT_SECONDS, connect=_HTTP_CONNECT_TIMEOUT_SECONDS),
            max_retries=_MAX_RETRIES,
        )
        _CLIENT_CACHE[key] = client
    return client
