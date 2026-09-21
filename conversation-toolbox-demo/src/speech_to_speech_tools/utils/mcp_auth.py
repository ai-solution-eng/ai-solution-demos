"""Shared API-key middleware for the MCP fleet (the K8S-MCP pattern, extracted).

NOT an auth server — a pure-ASGI middleware class each server wraps its own
app with. No extra deployment, no network hop, no central point of failure;
keys stay in each server's own Kubernetes Secret and are re-read PER REQUEST,
so a Secret rotation reaches a running pod without a restart.

One-address wiring (fleet decision, 2026-09):

* ``MCP_API_KEYS`` is the UNIVERSAL env var — every fleet server accepts it.
* Per-server names (``K8S_MCP_API_KEY``, ``APPLYGATE_API_KEYS``,
  ``WORKBENCH_API_KEYS``, ...) keep working: the key sets are UNIONED, all
  comparisons constant-time (``hmac.compare_digest``). Comma-separated keys
  within one var are the rotation mechanism — append the new key, move
  clients over, drop the old one, zero downtime.
* No keys configured → auth is OPEN (local development mode); call
  ``warn_if_open`` in ``main()`` to scream about it once at startup. Whether
  that is acceptable per server is a CHART decision: mandatory servers wire
  the env from an operator-created Secret unconditionally (the pod fails
  loud with CreateContainerConfigError until the Secret exists); optional
  servers wire it only when the chart values point at one.

Scope per server (fleet decision, 2026-09):

* MANDATORY auth: k8s-mcp, applygate, logsearch, workbench (execution or
  cluster-mutation surfaces).
* OPTIONAL auth: RAG-MCP, SQLhandler, searxng (read/search surfaces fronted
  by the gateway).

This module is hardlinked into the fleet by pcai_utils machinery; keep it
dependency-free (stdlib only) and import-agnostic (no relative imports, no
sibling-module imports) so every consumer can import it from wherever its
tree places it.
"""

import hmac
import os

UNIVERSAL_API_KEYS_ENV = "MCP_API_KEYS"

UNAUTHORIZED_BODY = b'{"error": "unauthorized: missing or invalid API key"}'

DEFAULT_PUBLIC_PATHS = ("/health", "/healthz")


def configured_keys(env_names=("MCP_API_KEYS",)):
    """Union of comma-separated keys from the given env vars (order kept,
    duplicates dropped). Env is read on every call — rotation without restart."""
    keys: list = []
    for name in env_names:
        raw = os.environ.get(name, "")
        for k in raw.split(","):
            k = k.strip()
            if k and k not in keys:
                keys.append(k)
    return keys


def presented_keys(scope):
    """Candidate keys from raw ASGI headers (names must already be lowercase).

    Accepts ``Authorization: Bearer <key>`` and ``X-API-Key: <key>``; both are
    collected so clients can use whichever header their MCP client exposes.
    """
    candidates = []
    for name, value in scope.get("headers", []):
        lowered = name.lower()
        if lowered == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                candidates.append(token.strip())
        elif lowered == b"x-api-key":
            candidates.append(value.decode("latin-1").strip())
    return candidates


class ApiKeyAuthMiddleware:
    """Pure-ASGI middleware enforcing an API key on the protected paths.

    Parameters
    ----------
    app:
        The ASGI app to wrap (usually the server's assembled Starlette app —
        wrapping in the builder, not in ``main()``, so every consumer of the
        builder gets the authenticated app).
    env_names:
        Env vars holding comma-separated valid keys. ALWAYS include
        ``UNIVERSAL_API_KEYS_ENV`` first; per-server names are aliases.
    protected:
        Optional ``callable(path) -> bool`` naming the paths that REQUIRE a
        key. When given it wins over ``public_paths`` (use it for
        allowlist-style servers like applygate, where ONLY /mcp is
        protected and everything else is public).
    public_paths:
        Paths that never require a key (default: the k8s probes). Used when
        ``protected`` is not given (denylist-style servers like workbench:
        everything except the probes is protected).
    """

    def __init__(self, app, env_names=("MCP_API_KEYS",), protected=None, public_paths=None):
        self.app = app
        self._env_names = tuple(env_names) or (UNIVERSAL_API_KEYS_ENV,)
        self._protected = protected
        self._public = frozenset(public_paths if public_paths is not None else DEFAULT_PUBLIC_PATHS)

    @property
    def routes(self):
        """Pass-through so callers/tests can introspect the wrapped app."""
        return self.app.routes

    def _needs_auth(self, path: str) -> bool:
        if self._protected is not None:
            return self._protected(path)
        return path not in self._public

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self._needs_auth(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        keys = configured_keys(self._env_names)
        if not keys:
            await self.app(scope, receive, send)  # auth disabled (dev mode)
            return
        for candidate in presented_keys(scope):
            for valid in keys:
                if hmac.compare_digest(candidate.encode("utf-8"), valid.encode("utf-8")):
                    await self.app(scope, receive, send)
                    return
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(UNAUTHORIZED_BODY)).encode("ascii")),
            (b"www-authenticate", b"Bearer"),
        ]
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": UNAUTHORIZED_BODY})


def warn_if_open(server_label: str, env_names=("MCP_API_KEYS",)) -> bool:
    """Loud one-time startup warning when no keys are configured.

    Returns True when auth is OPEN — call it in the server's HTTP-mode
    ``main()`` only (stdio dev use never needed auth).
    """
    if configured_keys(env_names):
        return False
    names = ", ".join(env_names)
    line = "=" * 72
    print(line)
    print(f"WARNING: none of [{names}] is set — {server_label}'s HTTP endpoints are OPEN")
    print("(local development mode). Configure keys from a Secret before any")
    print("shared or gateway-exposed deployment.")
    print(line)
    return True
