"""OIDC JWT sign-in → the same per-user registry identity (fleet decision D21).

A verified OIDC JWT (e.g. a Keycloak realm access token) resolves to the
SAME registry identity as that user's minted API key: ``name`` comes from a
JWT claim, datasets come from the identical D15/D17 ACL machinery, so both
credentials are interchangeable views of one identity — same
``Identity.client_id``, same unlock/throttle buckets, same /access page row.
A JWT-only user (no minted key, no env entry) still resolves, with ZERO
datasets (fail-closed) until an admin grants datasets or the user
self-selects.  A JWT is NEVER an admin identity.

Configuration (all read PER REQUEST — a config change needs no restart):

* ``RAG_OIDC_ENABLED``               — ``1``/``true``/``yes`` activates the
  resolver.
* ``RAG_OIDC_ISSUER``                — required when enabled; must equal the
  token's ``iss`` claim (e.g. a Keycloak realm URL).  Enabled WITHOUT an
  issuer keeps the resolver inert (fail-closed) and
  :func:`warn_if_misconfigured` — called from each server's HTTP startup —
  screams once.
* ``RAG_OIDC_AUDIENCE``              — default ``"ua"``; validated against
  the token's ``azp`` (exact match when present) or its ``aud`` (string, or
  list membership).
* ``RAG_OIDC_IDENTITY_CLAIM``        — default ``"preferred_username"``;
  fallback chain: that claim → ``sub``.  The resulting name must satisfy
  the registry name rules (``admin_registry._NAME_RE`` semantics) after
  whitespace stripping — a present-but-unsuitable primary claim REJECTS the
  token rather than silently falling back (the fallback exists for absent
  claims, not for forged or malformed ones).
* ``RAG_OIDC_JWKS_URL``              — optional explicit JWKS URL; default
  ``{issuer}/protocol/openid-connect/certs`` (the Keycloak convention).
  There is deliberately NO OIDC discovery in v1 (one less network
  dependency, easier testing) — point this env at another IdP's JWKS
  endpoint when the issuer is not Keycloak-shaped.
* ``RAG_OIDC_JWKS_REFRESH_SECONDS``  — default 3600; the JWKS cache TTL.  On
  an unknown ``kid`` one immediate refetch happens before failing, so a
  signing-key rotation is picked up without waiting out the TTL.
* ``RAG_OIDC_OBSERVED_TTL``          — default 86400; the minimum interval
  between ``last_seen`` updates per name (bounds writes to observed.json).
* ``RAG_OIDC_FETCH_TIMEOUT_SECONDS`` — default 3; the JWKS HTTP timeout.
* ``RAG_OIDC_CLOCK_SKEW_SECONDS``    — default 60; leeway for ``exp``/``nbf``.

Security posture:

* Verification is RS256 ONLY (hardcoded): ``none`` and the ``HS*`` family
  are refused outright — the classic alg-confusion guard (a JWKS public
  key must never be abused as an HMAC secret).
* Every failure returns ``None`` (fail-closed) and logs a short reason code
  (``signature``, ``issuer``, ``audience``, ``expired``, …) WITHOUT any
  token material; the identity name appears at debug level at most.
* The JWKS is fetched with urllib (stdlib), cached module-level under a
  lock with a TTL; a network/parse failure arms a short (60 s) negative
  cache so a down IdP cannot be hammered per-request.
* Blocked: an overlay client entry carrying ``"blocked": true`` (matched by
  name, or by an explicit ``"oidc"`` alias) resolves to ``None`` — 401
  downstream.  Env-registry names cannot be blocked (env has no place to
  say it; remove the env entry instead).
* ``{DATA_PATH}/access/observed.json`` (sibling of the D17 clients.json,
  same 0600/atomic/fcntl-locked posture) records ``{first_seen, last_seen}``
  per resolved name — best effort, TTL-throttled — so the /access page can
  show JWT-only users that have never been minted an entry.

Routing is caller-driven: ``clients_registry.resolve_presented`` consults
this module only for candidates that pass :func:`is_jwt_format` — an opaque
API key is never parsed as a JWT, and a JWT is never compared against the
key registry (disjoint credential spaces).  ``clients_registry`` /
``admin_registry`` are imported lazily INSIDE functions (they import this
module lazily in turn) so the graph has no cycle at module load.

Shared from pcai_utils (hardlink mesh — identical bytes in every consumer); stdlib-only EXCEPT ``cryptography`` (RS256
verification — the one genuine dependency, in docker/requirements.txt); it
never logs or stores token material.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    try:
        from multimodal_rag.utils.clients_registry import Identity  # RAG-native path
    except ImportError:  # standalone (pcai_utils source tree / other consumers)
        # annotations only
        Identity = object  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


def _sibling(mod_name: str, *, required: bool = True):
    """Import a pcai_utils sibling module from wherever THIS file was imported.

    The hardlink mesh plants this file at ``<consumer>/src/<pkg>/utils/``; in
    the RAG-native checkout it lives at ``src/multimodal_rag/utils/``; in
    pcai_utils itself there is no package at all.  Resolve against
    ``__package__`` first (both packaged cases), then fall back to a bare
    import from this file's own directory.

    ``required=True`` (default) keeps the historical shape: a missing sibling
    raises ImportError at the point of use.  ``required=False`` returns None
    when the consumer ships no registry package — the resolve_jwt pipeline
    then fails CLOSED (None -> 401) instead of raising, so a verification-only
    consumer stays safe.  A consumer that DOES ship a registry is unaffected:
    its sibling resolves through the normal paths and this branch never runs.
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
    try:
        return _importlib.import_module(mod_name)
    except ImportError:
        if required:
            raise
        return None


ENABLED_ENV = "RAG_OIDC_ENABLED"
ISSUER_ENV = "RAG_OIDC_ISSUER"
AUDIENCE_ENV = "RAG_OIDC_AUDIENCE"
IDENTITY_CLAIM_ENV = "RAG_OIDC_IDENTITY_CLAIM"
JWKS_URL_ENV = "RAG_OIDC_JWKS_URL"
JWKS_REFRESH_ENV = "RAG_OIDC_JWKS_REFRESH_SECONDS"
OBSERVED_TTL_ENV = "RAG_OIDC_OBSERVED_TTL"
FETCH_TIMEOUT_ENV = "RAG_OIDC_FETCH_TIMEOUT_SECONDS"
CLOCK_SKEW_ENV = "RAG_OIDC_CLOCK_SKEW_SECONDS"

DEFAULT_AUDIENCE = "ua"
DEFAULT_IDENTITY_CLAIM = "preferred_username"
DEFAULT_JWKS_REFRESH = 3600.0
DEFAULT_OBSERVED_TTL = 86400.0
DEFAULT_FETCH_TIMEOUT = 3.0
DEFAULT_CLOCK_SKEW = 60.0

# The identity-claim fallback chain's last resort (stable, unique, opaque).
_FALLBACK_CLAIM = "sub"
# Down-IdP backoff: after a failed JWKS fetch, fail fast for this long.
_NEGATIVE_TTL = 60.0
# Response cap — a real JWKS is a few KB; anything larger is hostile.
_JWKS_MAX_BYTES = 1_048_576
# JWT segments are unpadded base64url (optional trailing '=' tolerated).
_JWT_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")

_TRUE = ("1", "true", "yes")


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


def oidc_enabled() -> bool:
    """True when the OIDC resolver is both switched on and fully configured.

    ``RAG_OIDC_ENABLED`` alone is not enough: without ``RAG_OIDC_ISSUER``
    there is nothing to verify tokens against, so the resolver stays inert
    (fail-closed) and :func:`warn_if_misconfigured` screams once at startup.
    Read per request — enabling OIDC needs no restart.
    """
    return _flag_set() and bool(_env_str(ISSUER_ENV))


def warn_if_misconfigured(server_label: str) -> bool:
    """Loud one-shot startup warning: enabled but unusable.

    Returns True when ``RAG_OIDC_ENABLED`` is set without a usable issuer —
    the state in which the resolver is inert and every JWT 401s (D21
    fail-closed).  Mirrors ``mcp_auth.warn_if_open``: call from the server's
    HTTP-mode startup only (stdio dev use never verifies tokens).
    """
    if not _flag_set() or _env_str(ISSUER_ENV):
        return False
    line = "=" * 72
    print(line)
    print(f"WARNING: {ENABLED_ENV} is set but {ISSUER_ENV} is empty — {server_label} will NOT")
    print("accept OIDC JWTs (fail-closed). Set the issuer (e.g. a Keycloak realm URL) to")
    print(f"activate JWT sign-in, or clear {ENABLED_ENV} to silence this warning.")
    print(line)
    return True


def oidc_status() -> dict:
    """Small introspection helper for the /access page and docs — no secrets.

    ``enabled`` reports the EFFECTIVE state (``oidc_enabled``): a deployment
    that set the flag but no issuer reads ``enabled: false`` with an empty
    ``issuer``, which is exactly the diagnosis the page should render.
    """
    return {
        "enabled": oidc_enabled(),
        "issuer": _env_str(ISSUER_ENV) or None,
        "audience": _env_str(AUDIENCE_ENV) or DEFAULT_AUDIENCE,
        "identity_claim": _env_str(IDENTITY_CLAIM_ENV) or DEFAULT_IDENTITY_CLAIM,
    }


# ---------------------------------------------------------------------------
# Token shape + verification
# ---------------------------------------------------------------------------


def is_jwt_format(token: str) -> bool:
    """Cheap pre-check: three dot-separated base64url segments (a JWT's shape).

    Opaque API keys are (with overwhelming probability) not JWT-shaped, so
    callers route only plausible JWTs into the verification pipeline — an
    ordinary key never costs a parse, and a JWT is never compared against
    the key registry.  Deliberately decodes and verifies NOTHING.
    """
    if not isinstance(token, str):
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    return all(part and _JWT_SEGMENT_RE.match(part) for part in parts)


def _b64url_bytes(segment: str) -> bytes:
    if not _JWT_SEGMENT_RE.match(segment):
        raise ValueError("segment is not base64url")
    return base64.urlsafe_b64decode((segment + "=" * (-len(segment) % 4)).encode("ascii"))


def _b64url_uint(segment: str) -> int:
    return int.from_bytes(_b64url_bytes(segment), "big")


def _b64url_json(segment: str) -> dict | None:
    try:
        doc = json.loads(_b64url_bytes(segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _fail(reason: str, detail: str | None = None) -> None:
    """Fail closed with a short reason code (never any token material)."""
    logger.debug("OIDC JWT rejected: %s%s", reason, f" ({detail})" if detail else "")


def _jwks_url() -> str:
    explicit = _env_str(JWKS_URL_ENV)
    if explicit:
        return explicit
    return f"{_env_str(ISSUER_ENV).rstrip('/')}/protocol/openid-connect/certs"


# JWKS cache (per JWKS URL): stamp → keys, plus a negative cache so a down
# IdP cannot be hammered per request.  Fetches happen under the lock —
# serialized (bounded by the fetch timeout) instead of thundering-herd.
_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, dict[str, tuple[int, int]]]] = {}
_jwks_negative: dict[str, float] = {}


def _fetch_jwks(url: str, timeout: float) -> dict[str, tuple[int, int]]:
    """Fetch + parse the JWKS into ``{kid: (n, e)}`` (raises on failure).

    Cert verification stays ON; when ``REMOTE_CA_BUNDLE`` points at a
    platform CA bundle (the PCAI private-CA case), it is trusted via a
    dedicated SSL context — the D22 hotfix pattern from ``oidc_sso`` — so
    the EXTERNAL https issuer URL (e.g. the ezaf-gateway front) becomes
    usable from the pod, not just the in-cluster plain-HTTP endpoint.
    """
    import ssl
    import urllib.parse

    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "multimodal-rag"})
    handlers: list = []
    ca_bundle = os.environ.get("REMOTE_CA_BUNDLE", "").strip()
    if url.lower().startswith("https://") and ca_bundle and os.path.exists(ca_bundle):
        try:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_bundle)))
        except Exception:
            pass  # unusable bundle → default verification (fail closed as usual)
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith((".svc", ".svc.cluster.local", ".local")) or host == "localhost":
        # in-cluster: never route through an ambient proxy
        handlers.append(urllib.request.ProxyHandler({}))
    if handlers:
        # build_opener takes handlers POSITIONALLY (*handlers) — the
        # handlers= kwarg raised TypeError on every fetch, killing the
        # whole D21/D22 JWT path (found live on G2, 2026-10-02).
        with urllib.request.build_opener(*handlers).open(req, timeout=timeout) as resp:
            payload = json.loads(resp.read(_JWKS_MAX_BYTES).decode("utf-8"))
    else:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read(_JWKS_MAX_BYTES).decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("keys"), list):
        raise TypeError("JWKS payload is not a {keys: [...]} document")
    keys: dict[str, tuple[int, int]] = {}
    for jwk in payload["keys"]:
        if not isinstance(jwk, dict):
            continue
        kid = jwk.get("kid")
        pair = _rsa_pair_from_jwk(jwk)
        if isinstance(kid, str) and kid and pair is not None:
            keys[kid] = pair
    if not keys:
        raise ValueError("JWKS carries no usable RSA signing keys")
    return keys


def _rsa_pair_from_jwk(jwk: dict) -> tuple[int, int] | None:
    """``(n, e)`` for one RSA/signature JWK, else None (non-RSA skipped)."""
    if jwk.get("kty") != "RSA" or jwk.get("use") not in (None, "sig"):
        return None
    try:
        return _b64url_uint(str(jwk["n"])), _b64url_uint(str(jwk["e"]))
    except (KeyError, ValueError):
        return None


def _jwks_keys(url: str, *, force: bool = False) -> dict[str, tuple[int, int]] | None:
    """``{kid: (n, e)}`` from the TTL-cached JWKS (fetch when stale/forced).

    ``force=True`` is the unknown-``kid`` rotation path: ONE immediate
    refetch.  A failed fetch (network/parse) arms the negative cache —
    forced or not, ``None`` comes back until the backoff elapses, so a down
    IdP sees at most one attempt per backoff window per replica.
    """
    ttl = _env_float(JWKS_REFRESH_ENV, DEFAULT_JWKS_REFRESH)
    timeout = _env_float(FETCH_TIMEOUT_ENV, DEFAULT_FETCH_TIMEOUT)
    now = time.time()
    with _jwks_lock:
        cached = _jwks_cache.get(url)
        if not force and cached and (now - cached[0]) < ttl:
            return cached[1]
        negative_until = _jwks_negative.get(url)
        if negative_until and now < negative_until:
            return None
        try:
            keys = _fetch_jwks(url, timeout)
        except Exception as exc:  # network/parse failure — fail closed, back off
            _jwks_negative[url] = time.time() + _NEGATIVE_TTL
            logger.warning(
                "OIDC JWKS fetch failed (%s: %s) — JWT verification is briefly unavailable",
                type(exc).__name__,
                exc,
            )
            return None
        _jwks_negative.pop(url, None)
        _jwks_cache[url] = (time.time(), keys)
        return keys


def _verify_rs256(signing_input: bytes, signature: bytes, key: tuple[int, int]) -> bool:
    """PKCS1v15/SHA256 verification against one JWKS RSA public key."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    n, e = key
    public_key = rsa.RSAPublicNumbers(e, n).public_key()
    try:
        public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return False
    return True


def _audience_matches(claims: dict) -> bool:
    audience = _env_str(AUDIENCE_ENV) or DEFAULT_AUDIENCE
    azp = claims.get("azp")
    if azp is not None:
        return azp == audience
    aud = claims.get("aud")
    if isinstance(aud, str):
        return aud == audience
    if isinstance(aud, list):
        return audience in [a for a in aud if isinstance(a, str)]
    return False


def _claims_valid(claims: dict) -> bool:
    """iss/exp/nbf/azp-aud validation (fail-closed, reason-coded)."""
    if claims.get("iss") != _env_str(ISSUER_ENV):
        _fail("issuer")
        return False
    skew = _env_float(CLOCK_SKEW_ENV, DEFAULT_CLOCK_SKEW)
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or float(exp) <= now - skew:
        _fail("expired", "exp missing or in the past (clock-skew leeway applied)")
        return False
    nbf = claims.get("nbf")
    if nbf is not None and (not isinstance(nbf, (int, float)) or float(nbf) > now + skew):
        _fail("nbf", "nbf in the future (clock-skew leeway applied)")
        return False
    if not _audience_matches(claims):
        _fail("audience")
        return False
    return True


def verify_and_decode(token: str) -> dict | None:
    """Verify *token* and return its claims dict, or ``None``.

    Pipeline: header decode → alg MUST be ``RS256`` (hardcoded; ``none`` /
    ``HS*`` rejected — alg-confusion guard) → ``kid`` lookup in the cached
    JWKS (one forced refetch on an unknown kid) → RS256 PKCS1v15/SHA256
    signature verification → claim validation (``iss`` equality, ``exp``
    leeway, optional ``nbf``, ``azp``/``aud`` audience).  ANY failure —
    malformed token, wrong alg, unknown key, bad signature, bad claims,
    disabled resolver, unreachable IdP — returns ``None`` (fail-closed) and
    logs a short reason code; token material is never logged or stored.
    """
    if not oidc_enabled() or not is_jwt_format(token):
        _fail("disabled-or-format")
        return None
    header, payload_segment, signature_segment = token.split(".")
    header_doc = _b64url_json(header)
    claims = _b64url_json(payload_segment)
    if header_doc is None or claims is None:
        _fail("format")
        return None
    alg = header_doc.get("alg")
    if alg != "RS256":
        _fail("alg", f"rejected alg {alg!r} (RS256 only — alg-confusion guard)")
        return None
    kid = header_doc.get("kid")
    if not isinstance(kid, str) or not kid:
        _fail("kid", "header carries no kid")
        return None
    try:
        signature = _b64url_bytes(signature_segment)
    except ValueError:
        _fail("format")
        return None
    keys = _jwks_keys(_jwks_url())
    if not keys:
        _fail("jwks", "no usable JWKS (fetch failed or negative-cached)")
        return None
    key = keys.get(kid)
    if key is None:
        # Unknown kid: ONE immediate refetch before failing (rotation pickup).
        key = (_jwks_keys(_jwks_url(), force=True) or {}).get(kid)
    if key is None:
        _fail("kid")
        return None
    if not _verify_rs256(f"{header}.{payload_segment}".encode("ascii"), signature, key):
        _fail("signature")
        return None
    if not _claims_valid(claims):
        return None
    return claims


# ---------------------------------------------------------------------------
# Name resolution + the resolve_jwt pipeline
# ---------------------------------------------------------------------------


def _identity_name(claims: dict) -> str | None:
    """The registry name for *claims* — identity claim first, then ``sub``.

    The name must satisfy the registry name rules (admin_registry's
    ``_NAME_RE``) after whitespace stripping.  A primary claim that is
    present but unsuitable REJECTS the token (no silent fallback — the
    fallback chain exists for absent claims only).
    """
    admin_registry = _sibling("admin_registry", required=False)
    if admin_registry is None:
        _fail("registry", "no registry module ships with this consumer")
        return None

    claim_name = _env_str(IDENTITY_CLAIM_ENV) or DEFAULT_IDENTITY_CLAIM
    primary = claims.get(claim_name)
    if isinstance(primary, str) and primary.strip():
        name = primary.strip()
        if not admin_registry.valid_name(name):
            _fail("identity", f"{claim_name} value does not satisfy the registry name rules")
            return None
        return name
    sub = claims.get(_FALLBACK_CLAIM)
    if isinstance(sub, str) and sub.strip():
        name = sub.strip()
        if not admin_registry.valid_name(name):
            _fail("identity", "sub value does not satisfy the registry name rules")
            return None
        return name
    _fail("identity", f"neither {claim_name} nor {_FALLBACK_CLAIM} is a usable string")
    return None


def _overlay_entry_for(jwt_name: str) -> tuple[str, dict | None]:
    """``(final identity name, matching overlay entry or None)``.

    D21 alias resolution: an overlay client whose explicit ``"oidc"`` field
    names the JWT identity owns the mapping — the identity takes the
    overlay client's NAME (and therefore its datasets).  Without an alias
    match, plain name-convention matching applies (the JWT name must equal
    an overlay/env registry name to pick up that entry's context; datasets
    still come from the merged ACLs either way).  Deterministic order when
    two clients alias the same JWT name (operator error): sorted by name.
    """
    admin_registry = _sibling("admin_registry", required=False)
    if admin_registry is None:
        return jwt_name, None

    entries = admin_registry.overlay_entries()
    for name in sorted(entries):
        alias = entries[name].get("oidc")
        if isinstance(alias, str) and alias.strip() == jwt_name:
            return name, entries[name]
    return jwt_name, entries.get(jwt_name)


def resolve_jwt(token: str) -> "Identity | None":  # noqa: UP037 — lazy-imported type
    """Full pipeline: verified JWT → a registry CLIENT identity (or ``None``).

    Format check → :func:`verify_and_decode` → name resolution → blocked
    check → ACL lookup → :class:`clients_registry.Identity`.  The result is
    ALWAYS ``kind="client"`` (a JWT is never an admin identity) with the
    name's merged datasets (env ∪ overlay) — an unknown name gets an EMPTY
    dataset set (fail-closed).  A blocked overlay entry (by name or alias)
    resolves to ``None`` (401 downstream).  On success the identity is
    stamped into observed.json (best effort, TTL-throttled).
    """
    if not oidc_enabled():
        return None
    clients_registry = _sibling("clients_registry", required=False)
    if clients_registry is None:
        # Verification-only consumer: no registry package ships here, so no
        # identity can resolve — fail CLOSED (None -> 401), never raise.
        _fail("registry", "no registry module ships with this consumer")
        return None
    claims = verify_and_decode(token)
    if claims is None:
        return None
    jwt_name = _identity_name(claims)
    if jwt_name is None:
        return None
    name, entry = _overlay_entry_for(jwt_name)
    if entry is not None and entry.get("blocked") is True:
        _fail("blocked")
        return None
    grants = clients_registry.dataset_acls().get(name, {})
    identity = clients_registry.Identity(
        kind="client",
        name=name,
        datasets=frozenset(grants),
        grants=dict(grants),
    )
    record_observed(name)
    logger.debug("OIDC JWT resolved to registry identity '%s' (%d dataset(s))", name, len(grants))
    return identity


# ---------------------------------------------------------------------------
# Observed JWT identities ({DATA_PATH}/access/observed.json)
# ---------------------------------------------------------------------------

_observed_throttle: dict[str, float] = {}
_observed_throttle_lock = threading.Lock()
_observed_cache: dict[str, tuple[int, int, dict]] = {}
_observed_cache_lock = threading.Lock()


def _observed_path() -> Path:
    return Path(os.environ.get("DATA_PATH", "/data")) / "access" / "observed.json"


def record_observed(name: str) -> None:
    """Best-effort ``{first_seen, last_seen}`` stamp for a resolved identity.

    Throttled per name by ``RAG_OIDC_OBSERVED_TTL`` (per process — bounds
    the overlay writes without a per-request NFS round trip; first sight of
    a name always writes).  NEVER raises: an unwritable sidecar must not
    fail an otherwise-authenticated request.
    """
    admin_registry = _sibling("admin_registry", required=False)
    if admin_registry is None:
        return

    name = str(name).strip()
    if not admin_registry.valid_name(name):
        return
    now = time.time()
    with _observed_throttle_lock:
        last = _observed_throttle.get(name)
        if last is not None and (now - last) < _env_float(OBSERVED_TTL_ENV, DEFAULT_OBSERVED_TTL):
            return
        _observed_throttle[name] = now
    try:
        _write_observed(name)
    except Exception:  # best-effort telemetry — never fail the request
        logger.debug("observed.json write skipped for '%s'", name)


def _write_observed(name: str) -> None:
    """Read-modify-write observed.json — the admin_registry overlay posture:
    0600, atomic temp+replace, in-process writer lock then cross-process
    fcntl lock on the shared PVC."""
    admin_registry = _sibling("admin_registry")
    _cross_process_lock = _sibling("access_store")._cross_process_lock

    path = _observed_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")

    def _apply(doc: dict) -> None:
        identities = doc.setdefault("identities", {})
        entry = identities.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["first_seen"] = entry.get("first_seen") or stamp
        entry["last_seen"] = stamp
        identities[name] = entry

    key = str(path)
    with admin_registry._writer_lock(key), _cross_process_lock(path.with_suffix(".lock")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            doc = {}
        if not isinstance(doc, dict) or not isinstance(doc.get("identities"), dict):
            doc = {"identities": {}}
        _apply(doc)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        st = path.stat()
        with _observed_cache_lock:
            _observed_cache[key] = (st.st_mtime_ns, st.st_size, doc)


def observed_identities() -> list[dict]:
    """``[{name, first_seen, last_seen}]`` from observed.json (mtime-cached).

    The /access page's JWT-only rows: names that authenticated with a valid
    token at least once.  Missing/corrupt file → empty list (fail-soft —
    this is telemetry, not authorization).
    """
    path = _observed_path()
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        with _observed_cache_lock:
            _observed_cache.pop(key, None)
        return []
    stamp = (st.st_mtime_ns, st.st_size)
    with _observed_cache_lock:
        cached = _observed_cache.get(key)
        if cached and (cached[0], cached[1]) == stamp:
            return _observed_list(cached[2])
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        doc = {}
    if not isinstance(doc, dict) or not isinstance(doc.get("identities"), dict):
        doc = {"identities": {}}
    with _observed_cache_lock:
        _observed_cache[key] = (stamp[0], stamp[1], doc)
    return _observed_list(doc)


def _observed_list(doc: dict) -> list[dict]:
    out: list[dict] = []
    for name, entry in sorted(doc.get("identities", {}).items()):
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "name": str(name),
                "first_seen": entry.get("first_seen"),
                "last_seen": entry.get("last_seen"),
            }
        )
    return out
