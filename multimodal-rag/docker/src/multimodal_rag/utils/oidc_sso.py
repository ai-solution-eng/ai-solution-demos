"""Browser SSO — the OIDC authorization-code flow (fleet decision D22, 2026-10).

Mirrors the Open WebUI deployment's SSO shape (``configure_oidc.sh`` +
``security.oidc`` chart wiring in the frameworks repo): the app is an OIDC
CLIENT.  ``GET /oauth/login`` redirects the browser to the realm's
authorization endpoint; ``GET /oauth/oidc/callback`` exchanges the code for
tokens (form POST to the token endpoint), verifies the access token with the
EXACT D21 machinery (RS256/JWKS, iss/aud/exp — no new trust), and sets an
HttpOnly cookie carrying the access token.  The auth middleware then treats
that cookie as ONE MORE presented credential — the lowest-priority envelope —
so SSO sessions flow through the identical fail-closed registry resolution
(same identity as the user's minted key, same zero-dataset default, never
admin).

Design notes:

* **Stateless across replicas**: the CSRF ``state`` rides a short-lived
  HttpOnly cookie (no server-side session store), and the session itself is
  the token cookie — any pod can serve any request (the streamable-http
  house rule).
* **In-cluster provider**: like OWUI, the chart points the OIDC discovery
  URL at the in-cluster Keycloak service (plain HTTP) — the external https
  issuer URL is CLAIM-COMPARED only, never fetched, so the platform-CA TLS
  gap cannot break sign-in.
* **Discovery, minimally**: the authorization/token endpoints come from the
  provider's ``.well-known/openid-configuration`` document (cached with a
  TTL, negative-cached on failure); explicit env overrides win when an IdP
  is not Keycloak-shaped.
* **No token material in logs or pages**: the cookie is HttpOnly (page JS
  never sees it), errors are reason-coded, and the whoami endpoint reports
  identity/datasets only.
* **Inert by default**: without ``RAG_OIDC_SSO_ENABLED`` + client secret +
  the D21 issuer, every route 404s and no cookie is ever accepted — the
  deployment is byte-identical to pre-D22.

Shared from pcai_utils (hardlink mesh — identical bytes in every consumer); stdlib-only; it never logs token material.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def _sibling(mod_name: str):
    """Import a pcai_utils sibling module from wherever THIS file was imported.

    The hardlink mesh plants this file at ``<consumer>/src/<pkg>/utils/``; in
    the RAG-native checkout it lives at ``src/multimodal_rag/utils/``; in
    pcai_utils itself there is no package at all.  Resolve against
    ``__package__`` first (both packaged cases), then fall back to a bare
    import from this file's own directory.
    """
    import importlib as _importlib
    import sys as _sys
    from pathlib import Path as _Path

    if __package__:
        try:
            return _importlib.import_module(f"{__package__}.{mod_name}")
        except ImportError:
            pass
    here = str(_Path(__file__).resolve().parent)
    if here not in _sys.path:
        _sys.path.insert(0, here)
    return _importlib.import_module(mod_name)


ENABLED_ENV = "RAG_OIDC_SSO_ENABLED"
CLIENT_ID_ENV = "RAG_OIDC_SSO_CLIENT_ID"
CLIENT_SECRET_ENV = "RAG_OIDC_SSO_CLIENT_SECRET"
REDIRECT_URI_ENV = "RAG_OIDC_SSO_REDIRECT_URI"
PROVIDER_URL_ENV = "RAG_OIDC_SSO_PROVIDER_URL"
AUTHZ_ENDPOINT_ENV = "RAG_OIDC_SSO_AUTHORIZATION_ENDPOINT"
TOKEN_ENDPOINT_ENV = "RAG_OIDC_SSO_TOKEN_ENDPOINT"
SCOPES_ENV = "RAG_OIDC_SSO_SCOPES"
COOKIE_NAME_ENV = "RAG_OIDC_SSO_COOKIE_NAME"
COOKIE_MAX_AGE_ENV = "RAG_OIDC_SSO_COOKIE_MAX_AGE"
COOKIE_SECURE_ENV = "RAG_OIDC_SSO_COOKIE_SECURE"
FETCH_TIMEOUT_ENV = "RAG_OIDC_SSO_FETCH_TIMEOUT_SECONDS"

DEFAULT_CLIENT_ID = "ua"
DEFAULT_SCOPES = "openid profile email"
DEFAULT_COOKIE_NAME = "pcai-sso"
DEFAULT_COOKIE_MAX_AGE = 43200  # 12 h — capped by the token's own exp anyway
STATE_COOKIE_SUFFIX = "-state"
STATE_MAX_AGE = 600  # 10 min to complete the round trip

_TRUE = ("1", "true", "yes")

# Well-known discovery cache (per URL): stamp → endpoints, plus a negative
# cache so a down IdP is not hammered per request (the D21 JWKS pattern).
_discovery_lock = threading.Lock()
_discovery_cache: dict[str, tuple[float, dict]] = {}
_discovery_negative: dict[str, float] = {}
_DISCOVERY_TTL = 3600.0
_DISCOVERY_NEGATIVE_TTL = 60.0
_DISCOVERY_MAX_BYTES = 1_048_576


def _env_str(name: str) -> str:
    return os.environ.get(name, "").strip()


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _flag_set() -> bool:
    return _env_str(ENABLED_ENV).lower() in _TRUE


def client_id() -> str:
    return _env_str(CLIENT_ID_ENV) or DEFAULT_CLIENT_ID


def scopes() -> str:
    return _env_str(SCOPES_ENV) or DEFAULT_SCOPES


def cookie_name() -> str:
    return _env_str(COOKIE_NAME_ENV) or DEFAULT_COOKIE_NAME


def state_cookie_name() -> str:
    return cookie_name() + STATE_COOKIE_SUFFIX


def cookie_secure() -> bool:
    return _env_str(COOKIE_SECURE_ENV).lower() not in ("0", "false", "no")


def cookie_max_age() -> int:
    raw = _env_str(COOKIE_MAX_AGE_ENV)
    try:
        val = int(raw) if raw else DEFAULT_COOKIE_MAX_AGE
    except ValueError:
        return DEFAULT_COOKIE_MAX_AGE
    return val if val > 0 else DEFAULT_COOKIE_MAX_AGE


def redirect_uri() -> str:
    return _env_str(REDIRECT_URI_ENV)


def sso_enabled() -> bool:
    """True when the SSO flow is fully configured and usable.

    Requires: the flag, a client secret, a redirect URI, AND the D21 OIDC
    resolver (issuer) — the callback verifies the token with that machinery,
    so SSO without it is meaningless.  Read per request; a config change
    needs no restart.
    """
    if not _flag_set():
        return False
    oidc_identity = _sibling("oidc_identity")

    return bool(_env_str(CLIENT_SECRET_ENV)) and bool(redirect_uri()) and oidc_identity.oidc_enabled()


def warn_if_misconfigured(server_label: str) -> bool:
    """Loud one-shot startup warning: the flag is set but SSO is unusable.

    Returns True when ``RAG_OIDC_SSO_ENABLED`` is set without the rest of
    the configuration (mirrors ``oidc_identity.warn_if_misconfigured``).
    """
    if not _flag_set() or sso_enabled():
        return False
    oidc_identity = _sibling("oidc_identity")

    missing = []
    if not _env_str(CLIENT_SECRET_ENV):
        missing.append(CLIENT_SECRET_ENV)
    if not redirect_uri():
        missing.append(REDIRECT_URI_ENV)
    if not oidc_identity.oidc_enabled():
        missing.append("RAG_OIDC_ISSUER (the D21 resolver verifies the tokens)")
    line = "=" * 72
    print(line)
    print(f"WARNING: {ENABLED_ENV} is set but SSO is INERT — missing: {', '.join(missing)}")
    print(f"  {server_label} will NOT serve /oauth/* and no SSO cookie is accepted.")
    print("  Complete the values (see README § Browser SSO) or clear the flag.")
    print(line)
    return True


# ---------------------------------------------------------------------------
# Provider endpoints (well-known discovery, cached)
# ---------------------------------------------------------------------------


def _provider_url() -> str:
    explicit = _env_str(PROVIDER_URL_ENV)
    if explicit:
        return explicit

    issuer = _env_str("RAG_OIDC_ISSUER")
    return f"{issuer.rstrip('/')}/.well-known/openid-configuration" if issuer else ""


def _fetch_discovery(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "multimodal-rag"})
    with _urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read(_DISCOVERY_MAX_BYTES).decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("discovery document is not a JSON object")
    if not payload.get("authorization_endpoint") or not payload.get("token_endpoint"):
        raise ValueError("discovery document lacks the authorization/token endpoints")
    return payload


# ---------------------------------------------------------------------------
# Ambient-proxy isolation (D22 hotfix, 2026-10): PCAI pods routinely inherit
# HTTP_PROXY/HTTPS_PROXY (the platform sets them fleet-wide — the DSH pod's
# env proves it), and urllib honours them for http:// URLs — routing
# IN-CLUSTER IdP calls through the corporate proxy, which times out (the
# observed "code exchange failed (URLError: <urlopen error timed out>)"
# loop).  Fix: for private/loopback targets, bypass proxies entirely via a
# no-proxy opener.  Public hosts keep the default behavior (their proxy may
# be REQUIRED for egress).
def _is_private_target(host: str) -> bool:
    import ipaddress
    import socket

    host = (host or "").strip().lower()
    if not host:
        return False
    if host == "localhost" or host.endswith((".svc", ".svc.cluster.local", ".local")):
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if addr.is_private or addr.is_loopback:
            return True
    return False


def _urlopen(req, timeout: float):
    """urllib.urlopen with ambient-proxy isolation for private targets AND
    platform-CA trust for https (the D22 hotfix + the 2026-10 external-IdP
    option).

    Two layers:

    * **Private targets** (in-cluster .svc / RFC1918 / loopback): bypass any
      ambient proxy (a fleet-wide HTTP_PROXY must never carry in-cluster
      calls) and, for https, optionally trust ``REMOTE_CA_BUNDLE`` — the
      platform's private CA — via a dedicated SSL context (verified
      handshake, still verified: no verification relaxation).
    * **Public targets**: the default opener (ambient proxy allowed — egress
      may require it), same REMOTE_CA_BUNDLE trust when configured.
    """
    import ssl

    target = urllib.parse.urlparse(req.full_url).hostname or ""
    ca_bundle = _env_str("REMOTE_CA_BUNDLE")
    ssl_ctx = None
    if req.full_url.lower().startswith("https://") and ca_bundle and os.path.exists(ca_bundle):
        try:
            ssl_ctx = ssl.create_default_context(cafile=ca_bundle)
        except Exception:
            ssl_ctx = None
    if _is_private_target(target):
        handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
        if ssl_ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ssl_ctx))
        opener = urllib.request.build_opener(*handlers)
        return opener.open(req, timeout=timeout)
    if ssl_ctx is not None:
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ssl_ctx))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def provider_config() -> tuple[str, str] | None:
    """``(authorization_endpoint, token_endpoint)`` — env overrides win, then
    the cached well-known document.  ``None`` = unavailable (fail closed).

    Network/parse failures arm a short negative cache so a down IdP sees at
    most one attempt per window per replica.
    """
    authz_env, token_env = _env_str(AUTHZ_ENDPOINT_ENV), _env_str(TOKEN_ENDPOINT_ENV)
    if authz_env and token_env:
        return authz_env, token_env
    url = _provider_url()
    if not url:
        return None
    now = time.time()
    with _discovery_lock:
        cached = _discovery_cache.get(url)
        if cached and (now - cached[0]) < _DISCOVERY_TTL:
            doc = cached[1]
        else:
            negative_until = _discovery_negative.get(url)
            if negative_until and now < negative_until:
                return None
            try:
                doc = _fetch_discovery(url, _env_float(FETCH_TIMEOUT_ENV, 3.0))
            except Exception as exc:
                _discovery_negative[url] = time.time() + _DISCOVERY_NEGATIVE_TTL
                logger.warning(
                    "OIDC SSO discovery fetch failed (%s: %s) — /oauth/* briefly unavailable",
                    type(exc).__name__,
                    exc,
                )
                return None
            _discovery_negative.pop(url, None)
            _discovery_cache[url] = (time.time(), doc)
    return (
        authz_env or str(doc["authorization_endpoint"]),
        token_env or str(doc["token_endpoint"]),
    )


# ---------------------------------------------------------------------------
# Flow helpers (URL building, code exchange, cookie attributes)
# ---------------------------------------------------------------------------


def build_authorization_url(state: str) -> str | None:
    """The realm authorization URL for one login attempt (``None`` = inert)."""
    cfg = provider_config()
    if not cfg:
        return None
    params = urllib.parse.urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": scopes(),
            "state": state,
        }
    )
    return f"{cfg[0]}?{params}"


def exchange_code(code: str) -> dict | None:
    """Exchange one authorization code at the token endpoint (form POST).

    Returns the parsed token response (``access_token`` / ``expires_in`` /
    ``id_token``) or ``None`` on ANY failure (fail closed).  The response is
    never logged.
    """
    cfg = provider_config()
    if not cfg or not code:
        return None
    data = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": client_id(),
            "client_secret": _env_str(CLIENT_SECRET_ENV),
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        cfg[1],
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "multimodal-rag",
        },
        method="POST",
    )
    try:
        with _urlopen(req, timeout=_env_float(FETCH_TIMEOUT_ENV, 8.0)) as resp:
            payload = json.loads(resp.read(_DISCOVERY_MAX_BYTES).decode("utf-8"))
    except Exception as exc:
        logger.warning("OIDC SSO code exchange failed (%s: %s)", type(exc).__name__, exc)
        return None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        logger.warning("OIDC SSO code exchange returned no access_token")
        return None
    return payload


def cookie_max_age_for(token: str) -> int:
    """The session cookie's max-age: the token's own remaining life, capped
    by the configured ceiling (a cookie outliving its token is useless)."""
    oidc_identity = _sibling("oidc_identity")

    cap = cookie_max_age()
    try:
        claims = oidc_identity.verify_and_decode(token)
    except Exception:
        return cap
    if not claims:
        return cap
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return max(60, min(cap, int(exp - time.time())))
    return cap


def new_state() -> str:
    import secrets

    return secrets.token_urlsafe(24)


def state_matches(cookie_state: str, query_state: str) -> bool:
    """Constant-time CSRF check: the callback's ``state`` must equal the one
    the login attempt planted.  Both empty → no match (fail closed)."""
    if not cookie_state or not query_state:
        return False
    import hmac

    return hmac.compare_digest(
        hashlib.sha256(cookie_state.encode()).hexdigest(),
        hashlib.sha256(query_state.encode()).hexdigest(),
    )


def safe_next_path(raw: str) -> str:
    """Only same-site absolute paths may be a post-login redirect target
    (an open redirect would launder the auth code).  Everything else → /."""
    if raw.startswith("/") and not raw.startswith("//") and "\\" not in raw:
        return raw
    return "/"
