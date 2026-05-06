
import httpx


def get_http_client(ignore_tls: bool = False) -> httpx.AsyncClient:
    """Returns an async HTTP client with optional TLS verification bypass."""
    return httpx.AsyncClient(verify=not ignore_tls)

def get_sync_http_client(ignore_tls: bool = False) -> httpx.Client:
    """Returns a sync HTTP client with optional TLS verification bypass."""
    return httpx.Client(verify=not ignore_tls)

def normalize_v1_endpoint(endpoint: str) -> str:
    """Consistently normalizes LLM/embedding endpoints to ensure /v1 suffix
    where appropriate for model discovery and verification.
    """
    endpoint = endpoint.strip().rstrip('/')
    
    # If the endpoint doesn't look like it hits /v1 or /models already, try to make it so
    # but we should be careful not to break user-provided full URLs.
    if not (endpoint.lower().endswith("/v1") or "/v1/" in endpoint.lower()):
        # If it's just a host/port, append /v1
        if endpoint.count("/") < 3: # e.g. http://localhost:11434 (protocol + host)
            endpoint += "/v1"
            
    return endpoint
