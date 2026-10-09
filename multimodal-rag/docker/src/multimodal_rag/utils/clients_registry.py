"""Multi-user API-key registry → dataset ACLs (fleet decision D15, opt-in).

Mirrors K8S-MCP's clients-registry pattern (``K8S_MCP_CLIENTS``): instead of
one shared deployment key, an operator may mint per-user keys and bind each
one to a set of datasets.  Capabilities travel with the credential — a key
holder can never widen their own access.

Configuration (both env vars are read PER REQUEST — a config change needs no
restart, matching the fleet's key-rotation convention):

* ``RAG_API_KEY_CLIENTS`` — ``"name:key;name:key"`` — ';'-separated entries,
  ``name:key`` (K8S_MCP_CLIENTS field rules: keys must not contain ``:`` or
  ``;``).  The same registry serves BOTH the MCP server and the REST API.
* ``RAG_DATASET_ACLS`` — ``"name:ds1,ds2;name2:*"`` — ';'-separated entries,
  ``name:dataset[,dataset...]``.  The special dataset ``*`` grants all
  datasets.  An entry with an empty dataset list grants nothing.

Semantics (ratified D15 — explicitly OPT-IN):

* A key in the registry resolves to a per-key identity; dataset access
  (list/read/search/unlock/manage) is enforced against that identity's ACL.
* A registry key that matches NO ACL entry gets NO datasets (fail-closed).
* The plain deployment keys — ``RAG_API_KEY`` plus the MCP key set
  (``MCP_API_KEYS`` / ``RAG_API_KEYS``, the fleet one-address wiring) — keep
  FULL access (admin semantics).
* DEFAULT (no ``RAG_API_KEY_CLIENTS``): today's single-key behaviour,
  byte-identical — nothing in this module enforces anything.

This module is RAG-local (not part of the pcai_utils hardlink mesh): it is
stdlib-only and dependency-free, and it never logs or returns key material.
"""

import hmac
import json
import os
import re
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import NamedTuple

from multimodal_rag.utils.mcp_auth import configured_keys

CLIENTS_ENV = "RAG_API_KEY_CLIENTS"
ACLS_ENV = "RAG_DATASET_ACLS"
# REST admin key (single). The MCP key set comes from mcp_auth.configured_keys.
REST_API_KEY_ENV = "RAG_API_KEY"
ALL_DATASETS = "*"

# D26 grant modes. A grant is either rw (read+write — the historical default,
# byte-compatible) or ro (read-only: list/read/search/select/bind only; every
# document/memory WRITE refuses). Modes travel with the credential exactly
# like the grant itself — a key holder can never widen ro to rw.
GRANT_RO = "ro"
GRANT_RW = "rw"
_GRANT_MODES = (GRANT_RO, GRANT_RW)


class ClientConfigError(ValueError):
    """A malformed RAG_API_KEY_CLIENTS / RAG_DATASET_ACLS entry (fail loud)."""


class DatasetAccessDenied(PermissionError):
    """The caller's key identity may not touch this dataset (D15, fail-closed)."""


class Identity(NamedTuple):
    """Resolved caller identity for one request.

    ``kind``  — ``"admin"`` (deployment keys, unrestricted) or ``"client"``
                (registry key, ACL-bound).
    ``name``  — registry entry name for clients, ``None`` for admins.
    ``datasets`` — frozenset of allowed dataset names; ``None`` = unrestricted
                (admins).  An empty frozenset = NO datasets (fail-closed).
    ``grants``  — D26: ``{dataset_name: mode}`` for the SAME dataset set
                (``mode`` is :data:`GRANT_RO` or :data:`GRANT_RW`) — or
                ``None`` = unrestricted/unmodelled (admins, and any caller
                constructed before modes existed; ``grant_mode`` treats a
                missing entry or a ``None`` map as rw).  The ``datasets``
                frozenset REMAINS the authoritative inclusion set — every
                existing predicate (``dataset_allowed``, listings, the
                middleware) keeps reading it; only the WRITE gate consults
                ``grants``.  A frozenset-built identity (all pre-D26 tests)
                resolves to all-rw.
    """

    kind: str
    name: str | None
    datasets: frozenset | None
    grants: "frozenset | dict | None" = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def client_id(self) -> str:
        """Stable per-key identity string (D10 throttle/unlock machinery)."""
        return "admin" if self.is_admin else f"key:{self.name}"


def parse_clients(raw: str) -> dict:
    """Parse ``RAG_API_KEY_CLIENTS`` into ``{key: name}``.

    Entries are ';'-separated, fields ':'-separated: ``name:key``.  Keys must
    not contain ``:`` or ``;`` (same rule as K8S_MCP_CLIENTS).  Duplicate
    keys: last entry wins (deterministic, no error — rotation friendliness).
    """
    entries: dict[str, str] = {}
    raw = (raw or "").strip()
    if not raw:
        return entries
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            raise ClientConfigError(
                f"invalid client entry {chunk!r} in {CLIENTS_ENV}: expected name:key (keys must not contain ':' or ';')"
            )
        name, key = parts[0].strip(), parts[1].strip()
        entries[key] = name
    return entries


def parse_acls(raw: str) -> dict:
    """Parse ``RAG_DATASET_ACLS`` into ``{name: {dataset: mode}}``.

    Entries are ';'-separated.  D26 accepted dataset-token forms:

    * ``ds1``          — rw (the historical form, byte-compatible)
    * ``ds1:rw``       — explicit rw
    * ``ds1:ro``       — READ-ONLY: every write surface refuses for it
    * ``*``            — rw all (historical)
    * ``*:ro``         — ro all (globals stay writable only via explicit
                          per-dataset rw tokens)

    A bare ``name:ds1,ds2`` list (no ``:mode`` suffixes) keeps the exact
    historical meaning — all-rw.  The ``:ro``/``:rw`` suffix is only taken
    when it is EXACTLY one of the two mode words (a dataset literally named
    ``weird:rw``-shaped names cannot occur — dataset names never contain
    ``:``, enforced by ``_validate_dataset_token``-equivalents at creation
    and by the name regex everywhere else).  Malformed entries raise
    ``ClientConfigError`` at import — the fail-loud convention (a typo'd
    registry must never silently degrade to "no ACLs enforced").

    Names with NO entry get no datasets (fail-closed).
    """
    acls: dict[str, dict] = {}
    raw = (raw or "").strip()
    if not raw:
        return acls
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, sep, datasets_raw = chunk.partition(":")
        name = name.strip()
        if not sep or not name:
            raise ClientConfigError(f"invalid ACL entry {chunk!r} in {ACLS_ENV}: expected name:dataset[,dataset...]")
        grants: dict[str, str] = {}
        for token in datasets_raw.split(","):
            token = token.strip()
            if not token:
                continue
            # D26: an exact ":ro"/":rw" suffix on a token carries the mode.
            # One partition only — a dataset NAME can never contain ':' so
            # there is nothing to mis-split, and an empty base ('':ro'')
            # is rejected by the name check below.
            base, msep, mode = token.partition(":")
            base = base.strip()
            if not base:
                raise ClientConfigError(f"invalid ACL entry {chunk!r} in {ACLS_ENV}: empty dataset token {token!r}")
            if not msep:
                grants[base] = GRANT_RW
            elif mode == GRANT_RO:
                grants[base] = GRANT_RO
            elif mode == GRANT_RW:
                grants[base] = GRANT_RW
            else:
                raise ClientConfigError(
                    f"invalid ACL entry {chunk!r} in {ACLS_ENV}: unknown mode {mode!r} "
                    f"on token {token!r} (expected 'ro' or 'rw')"
                )
        acls[name] = grants
    return acls


# Fail loud at import on malformed configuration (the K8S-MCP convention —
# a typo'd registry must never silently degrade to "no ACLs enforced").
parse_clients(os.environ.get(CLIENTS_ENV, ""))
parse_acls(os.environ.get(ACLS_ENV, ""))


def registry_clients() -> dict:
    """``{key: name}`` for the configured registry (re-read per request).

    D17: the env registry is UNIONED with the file-backed admin overlay
    (``utils/admin_registry.py`` — entries minted from the /access page with
    an admin key).  An env key that also appears in the overlay keeps its
    ENV name (the operator's hand-written config is authoritative on
    conflict).  When the overlay is disabled this is exactly the env parse.
    """
    env = parse_clients(os.environ.get(CLIENTS_ENV, ""))
    try:
        from multimodal_rag.utils import admin_registry

        overlay = admin_registry.overlay_clients()
    except Exception:  # overlay broken/absent → env-only (fail-open to env authority)
        return env
    merged = dict(overlay)
    merged.update(env)
    return merged


def dataset_acls() -> dict:
    """``{name: {dataset: mode}}`` (re-read per request).

    D17: env ACLs are UNIONED per name with the overlay's grants (an overlay
    client with no env entry is added wholesale; a name in BOTH keeps the
    union — neither source can silently revoke the other).  D26 modes merge
    by dataset with the STRONGEST restriction winning across sources: env rw
    ∪ overlay ro ⇒ ro; env ro ∪ overlay rw ⇒ ro (an operator ro is a floor
    the admin panel cannot silently lift, mirroring the D17 union rule that
    neither source silently revokes the other).  Disabled overlay → exactly
    the env parse.
    """
    env = parse_acls(os.environ.get(ACLS_ENV, ""))
    try:
        from multimodal_rag.utils import admin_registry

        overlay = admin_registry.overlay_acls()
    except Exception:
        return env
    merged = {name: dict(ds) for name, ds in env.items()}
    for name, ds in overlay.items():
        target = merged.setdefault(name, {})
        for ds_name, mode in ds.items():
            if target.get(ds_name) == GRANT_RW and mode == GRANT_RO:
                target[ds_name] = GRANT_RO
            else:
                target.setdefault(ds_name, mode)
    return merged


def registry_configured() -> bool:
    """True when multi-user key enforcement is active (D15/D17).

    Either the env registry is set OR the D17 admin overlay is enabled (an
    admin must resolve an identity even on a fresh deployment whose overlay
    file is still empty — the /access mint panel needs the admin identity).
    Read per request: configuring either source enables enforcement without
    a restart; leaving both unset keeps the single-key behaviour
    byte-identical.

    D21: a fully configured OIDC resolver (``RAG_OIDC_ENABLED`` +
    ``RAG_OIDC_ISSUER``) counts as configured too — a JWT-only deployment
    must resolve identities (the D20 anonymous branch must NOT engage there),
    and once this returns True JWTs resolve through oidc_identity below.

    Audit 2026-10-02 (cross-validation 3-α): a D16-only deployment
    (``RAG_ACCESS_STORE=1``, no env registry / overlay / OIDC) is a live
    multi-user self-service world — every identity that resolves is a
    per-user client.  It counts as configured (the embedded-key legacy
    fallback on the public pages must not engage there: the SPA's key-entry
    / SSO flows are the safe paths).
    """
    if bool(os.environ.get(CLIENTS_ENV, "").strip()):
        return True
    try:
        from multimodal_rag.utils import admin_registry

        if admin_registry.admin_file_enabled():
            return True
    except Exception:
        pass
    try:
        from multimodal_rag.utils import access_store

        if access_store.store_enabled():
            return True
    except Exception:
        pass
    # D24 (cross-validation 3-β): a trust-proxy deployment resolves
    # proxy-injected per-user identities (every visitor is a per-user client
    # world) — the embedded-key legacy fallback must not engage there either.
    if os.environ.get("RAG_TRUST_PROXY_IDENTITY", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        from multimodal_rag.utils import oidc_identity

        return oidc_identity.oidc_enabled()
    except Exception:
        return False


def admin_keys() -> list:
    """The deployment keys that keep FULL (admin) access under D15.

    ``RAG_API_KEY`` (REST) plus the MCP key set ``MCP_API_KEYS`` /
    ``RAG_API_KEYS`` (fleet one-address wiring) — deduplicated, order kept.
    """
    keys: list[str] = []
    raw_rest = os.environ.get(REST_API_KEY_ENV, "").strip()
    if raw_rest:
        keys.append(raw_rest)
    for k in configured_keys(("MCP_API_KEYS", "RAG_API_KEYS")):
        if k not in keys:
            keys.append(k)
    return keys


def _match(presented: list, valid: list) -> bool:
    """Constant-time membership test (treat keys like passwords)."""
    for candidate in presented:
        for v in valid:
            if hmac.compare_digest(candidate.encode("utf-8"), v.encode("utf-8")):
                return True
    return False


def resolve_presented(presented: list, presenters: "list | None" = None) -> "Identity | None":
    """Resolve presented key(s) to an :class:`Identity`, or ``None``.

    Admin keys win (deployment semantics), then registry keys.  A registry
    key with no ACL entry resolves to an identity with NO datasets
    (fail-closed).  Callers decide what ``None`` means for their surface —
    on a protected path it is 401.

    Delegation precedence (fleet decision D19, 2026-09-24): when *presenters*
    is provided it carries, per candidate key, the header that presented it
    (``"x-api-key"`` or ``"authorization"``).  A key presented via
    ``X-API-Key`` takes precedence over a key presented via
    ``Authorization: Bearer`` WHEN the two disagree: ``Authorization`` is
    transport/platform auth (a proxy or LLM gateway forwards its OWN admin
    token next to the caller's chosen ``X-API-Key``), and an explicit
    ``X-API-Key`` is the caller's DELEGATED service identity.  Resolution
    then follows the X-API-Key candidate alone — an admin-token holder can
    only ever DE-ESCALATE itself by also sending an X-API-Key (it could use
    that client key directly anyway), never escalate a client key to admin,
    so the precedence is safe.

    Without *presenters* (legacy callers, single-header requests) the
    historical admin-first order applies unchanged.

    D21 — OIDC JWTs as a second credential for the same registry identity:
    a candidate that is JWT-shaped (three base64url segments) may resolve
    through ``oidc_identity.resolve_jwt`` — to the SAME registry identity as
    that user's minted key (same name, same ACLs, same ``client_id``), or to
    a zero-dataset identity for an unknown-but-valid user.  Routing is
    shape-based, never a registry lookup: an opaque key is never parsed as a
    JWT, and a JWT is never compared against key material.  A JWT can only
    ever yield a ``kind="client"`` identity — never admin.  In delegation
    mode only the X-API-Key candidate takes the JWT path (Authorization is
    transport auth, D19 — its Bearer JWT stays transport-only there).  The
    D22 SSO cookie candidate carries source ``"cookie"`` — it participates
    ONLY in the fall-through paths below (never delegation, never admin).
    """
    presented = [k for k in (presented or []) if k]
    if not presented:
        return None
    if presenters is not None and len(presenters) == len(presented):
        xkey = [k for k, via in zip(presented, presenters) if via == "x-api-key"]
        if xkey:
            # Delegation mode: the X-API-Key candidate IS the caller's
            # chosen identity — resolve on it alone (never escalate to
            # admin from the co-forwarded Authorization token).
            return _resolve_registry_only(xkey) or _resolve_jwt_candidates(xkey) or _admin_identity_for(xkey)
    admins = admin_keys()
    if admins and _match(presented, admins):
        return Identity(kind="admin", name=None, datasets=None)
    # D24 (the Clearwing/DSH pattern): a "proxy-identity" candidate is the
    # ENFORCING proxy's injected identity NAME (not a secret) — it resolves
    # DIRECTLY to the registry identity by name (the caller cannot choose
    # it; the gateway overwrites the header per request behind the
    # AuthorizationPolicy).  LOWEST precedence: any resolvable explicit
    # credential above wins; a proxy-identity name that matches no
    # registry/ACL entry yields a zero-dataset identity (fail-closed) so an
    # edge-authenticated user lands in the SAME registry world as
    # everyone else.  Never admin.
    if presenters is not None and "proxy-identity" in presenters:
        proxied = [k for k, via in zip(presented, presenters) if via == "proxy-identity"]
        for name in proxied:
            candidate = str(name).strip()
            if not candidate:
                continue
            return _client_identity_for(candidate)
    return _resolve_registry_only(presented) or _resolve_jwt_candidates(presented)


def _resolve_jwt_candidates(candidates: list) -> "Identity | None":
    """D21: resolve the FIRST JWT-shaped candidate via oidc_identity.

    Opaque keys (the normal case) fail the cheap shape pre-check and cost
    nothing here.  ``None`` on every miss — the caller decides (401).
    """
    try:
        from multimodal_rag.utils import oidc_identity
    except Exception:
        return None
    if not oidc_identity.oidc_enabled():
        return None
    for candidate in candidates:
        if not oidc_identity.is_jwt_format(candidate):
            continue
        ident = oidc_identity.resolve_jwt(candidate)
        if ident is not None:
            return ident
    return None


def _admin_identity_for(candidates: list) -> "Identity | None":
    """Admin identity iff one of *candidates* IS an admin key (used after
    delegation resolution finds no registry match — an X-API-Key carrying
    the deployment key is still a legitimate admin presentation)."""
    admins = admin_keys()
    if admins and _match(candidates, admins):
        return Identity(kind="admin", name=None, datasets=None)
    return None


def _resolve_registry_only(candidates: list) -> "Identity | None":
    """Match *candidates* against the REGISTRY only (never admin_keys)."""
    clients = registry_clients()
    for candidate in candidates:
        for key, name in clients.items():
            if hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8")):
                return _client_identity_for(name)
    return None


def _client_identity_for(name: str) -> Identity:
    """The registry identity for one name (D26): grants carry modes,
    ``datasets`` stays the flat inclusion frozenset every legacy predicate
    reads."""
    grants = dataset_acls().get(name, {})
    return Identity(
        kind="client",
        name=name,
        datasets=frozenset(grants),
        grants=dict(grants),
    )


# ---------------------------------------------------------------------------
# Per-request identity (contextvar — set by each server's auth middleware)
# ---------------------------------------------------------------------------

_identity_ctx: ContextVar = ContextVar("rag_key_identity", default=None)


def set_current_identity(identity: "Identity | None"):
    """Bind the resolved identity for the current request (returns a token)."""
    return _identity_ctx.set(identity)


def reset_current_identity(token) -> None:
    _identity_ctx.reset(token)


def current_identity() -> "Identity | None":
    """The request's registry identity — ``None`` = D15 not active for it."""
    return _identity_ctx.get()


# ---------------------------------------------------------------------------
# Public datasets (2026-10, revised): a per-dataset meta.json flag making
# the dataset AVAILABLE for password-free self-selection by every minted
# key — deliberately NOT an automatic grant (a user's checkbox set is
# their world; public datasets are opt-in). Admin-only toggle (the REST
# surface is /api/admin/datasets/{name}/public); a password-protected
# dataset can never carry the flag (enforced at write AND read time).
# Read per call with an mtime/size-checked cache (the admin_registry
# pattern): a toggle takes effect on the next request from any replica
# (meta.json lives on the shared RWX PVC) without a per-request NFS read
# on the hot path.  Missing/corrupt meta -> private (fail-closed).
# ---------------------------------------------------------------------------

_PUBLIC_META_CACHE: dict[str, tuple[int, int, bool]] = {}
_public_meta_lock = threading.Lock()

# D23: the same meta.json files carry the creator stamp ("created_by") the
# ownership surfaces enforce on.  Listings annotate every row with it (plus
# "owned_by_me" for the caller), so the reads ride a second mtime/size-
# checked cache — one NFS read per dataset per stamp change, never per
# request.  Missing/corrupt meta / no stamp -> None (pre-D23: no provable
# creator; the delete/public gates treat that as admin-only).
_CREATED_BY_CACHE: dict[str, tuple[int, int, "str | None"]] = {}
_created_by_lock = threading.Lock()


def is_public_dataset(dataset_name: str) -> bool:
    """True when *dataset_name* is stamped "public" in its meta.json.

    Defense in depth: a meta that ALSO carries a password_hash is never
    public, regardless of the flag - a hand-edited meta cannot publish a
    password-gated dataset.
    """
    path = Path(os.environ.get("DATA_PATH", "/data")) / "datasets" / dataset_name / "meta.json"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    key = str(path)
    if stamp is not None:
        with _public_meta_lock:
            cached = _PUBLIC_META_CACHE.get(key)
            if cached and (cached[0], cached[1]) == stamp:
                return cached[2]
        is_public = False
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            is_public = bool(isinstance(meta, dict) and meta.get("public") and not meta.get("password_hash"))
        except (json.JSONDecodeError, OSError):
            is_public = False
        with _public_meta_lock:
            _PUBLIC_META_CACHE[key] = (stamp[0], stamp[1], is_public)
        return is_public
    with _public_meta_lock:
        _PUBLIC_META_CACHE.pop(key, None)
    return False


def created_by_dataset(dataset_name: str) -> "str | None":
    """The dataset's ``created_by`` stamp (D23), mtime/size-cached.

    Mirrors :func:`is_public_dataset`'s read discipline (stat → stamp
    compare → read → cache).  ``None`` for a missing/unreadable meta AND
    for a meta without the stamp (pre-D23) — callers treat None as "no
    provable creator" (admin-only manage, never non-admin public).
    """
    path = Path(os.environ.get("DATA_PATH", "/data")) / "datasets" / dataset_name / "meta.json"
    try:
        st = path.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    key = str(path)
    if stamp is not None:
        with _created_by_lock:
            cached = _CREATED_BY_CACHE.get(key)
            if cached and (cached[0], cached[1]) == stamp:
                return cached[2]
        creator: str | None = None
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(meta, dict):
                creator = str(meta.get("created_by") or "").strip() or None
        except (json.JSONDecodeError, OSError):
            creator = None
        with _created_by_lock:
            _CREATED_BY_CACHE[key] = (stamp[0], stamp[1], creator)
        return creator
    with _created_by_lock:
        _CREATED_BY_CACHE.pop(key, None)
    return None


def dataset_allowed(identity: "Identity | None", dataset_name: str) -> bool:
    """May *identity* touch *dataset_name*?

    ``identity is None`` → D15 inactive → allowed (default UX byte-identical).
    Admins → allowed.  Clients → dataset in their ACL, or ``*``.  Everything
    else → denied (fail-closed; an empty ACL grants nothing).

    NOTE (2026-10): the public-dataset flag is NOT a grant — it is
    availability (password-free self-selection, enforced in access_store),
    so it deliberately does not appear here.
    """
    if identity is None or identity.is_admin:
        return True
    datasets = identity.datasets or frozenset()
    return dataset_name in datasets or ALL_DATASETS in datasets


def require_dataset_access(identity: "Identity | None", dataset_name: str) -> None:
    """Raise :class:`DatasetAccessDenied` when access is not allowed."""
    if dataset_allowed(identity, dataset_name):
        return
    raise DatasetAccessDenied(
        f"Dataset '{dataset_name}' is not permitted for this API key (dataset ACLs are configured — D15)."
    )


# ---------------------------------------------------------------------------
# D26 write gate — grant modes
# ---------------------------------------------------------------------------


def grant_mode(identity: "Identity | None", dataset_name: str) -> "str | None":
    """The caller's effective D26 mode for *dataset_name*, or ``None``.

    ``None`` means "no mode semantics for this caller" — the write gate
    must ALLOW (byte-identical default):

    * ``identity is None`` (D15/D26 inactive — single-user, store-off);
    * an ADMIN identity (deployment keys / MCP keyset — D23's "admin full
      power" principle; modes are a registry-identity concept);
    * an identity with ``grants is None`` or an entry missing for an
      otherwise-allowed dataset (a frozenset-built identity — every
      pre-D26 construction site and test — resolves all-rw).

    Otherwise the mode of the MOST SPECIFIC grant wins: an explicit
    per-dataset entry outranks the ``*`` wildcard in either direction
    (``*:ro`` + ``ds1:rw`` ⇒ ds1 is rw; ``*:rw`` + ``ds1:ro`` ⇒ ds1 is ro).
    """
    if identity is None or identity.is_admin:
        return None
    grants = identity.grants
    if not isinstance(grants, dict):
        return None
    specific = grants.get(dataset_name)
    if specific in _GRANT_MODES:
        return specific
    wildcard = grants.get(ALL_DATASETS)
    if wildcard in _GRANT_MODES:
        return wildcard
    return None


def dataset_write_allowed(identity: "Identity | None", dataset_name: str) -> bool:
    """May *identity* WRITE to *dataset_name*? (D26 — the ro gate.)

    True unless the caller resolves to an explicit ``ro`` mode
    (:func:`grant_mode` returns ``GRANT_RO``).  Everything else — admin,
    identity-less, pre-D26 grants, rw — writes as before.  Callers layer
    their EXISTING gates on top (dataset manage stays ``*``/owner-or-admin;
    document writes stay ACL-inclusion-checked first); this predicate adds
    ONLY the mode check.
    """
    return grant_mode(identity, dataset_name) != GRANT_RO


def require_dataset_write(identity: "Identity | None", dataset_name: str) -> None:
    """Raise :class:`DatasetAccessDenied` when the caller's mode is ro."""
    if dataset_write_allowed(identity, dataset_name):
        return
    raise DatasetAccessDenied(
        f"Dataset '{dataset_name}' is read-only for this API key (a ro grant cannot be written — D26)."
    )


def filter_dataset_names(identity: "Identity | None", names) -> tuple:
    """Split *names* into ``(visible, hidden_count)`` under *identity*."""
    visible = [n for n in names if dataset_allowed(identity, str(n))]
    return visible, len(names) - len(visible)


# ---------------------------------------------------------------------------
# REST path helpers (the MCP tools pass dataset names as arguments)
# ---------------------------------------------------------------------------

_DATASET_PATH_RE = re.compile(r"^/api/datasets/([^/]+)(?:/.*)?$")


def dataset_name_from_path(path: str) -> "str | None":
    """Dataset name embedded in a REST path, or ``None``.

    ``/api/datasets`` (the listing) and everything outside
    ``/api/datasets/...`` return ``None``.  The segment is URL-decoded to
    match FastAPI's path-parameter handling.
    """
    from urllib.parse import unquote

    m = _DATASET_PATH_RE.match(path or "")
    if not m:
        return None
    return unquote(m.group(1))
