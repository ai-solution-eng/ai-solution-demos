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
import os
import re
from contextvars import ContextVar
from typing import NamedTuple

from multimodal_rag.utils.mcp_auth import configured_keys

CLIENTS_ENV = "RAG_API_KEY_CLIENTS"
ACLS_ENV = "RAG_DATASET_ACLS"
# REST admin key (single). The MCP key set comes from mcp_auth.configured_keys.
REST_API_KEY_ENV = "RAG_API_KEY"
ALL_DATASETS = "*"


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
    """

    kind: str
    name: str | None
    datasets: frozenset | None

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
    """Parse ``RAG_DATASET_ACLS`` into ``{name: frozenset(datasets)}``.

    Entries are ';'-separated: ``name:ds1,ds2`` — the special dataset ``*``
    grants everything; an entry with an empty dataset list (``name:``) grants
    nothing.  Names with NO entry get no datasets (fail-closed).
    """
    acls: dict[str, frozenset] = {}
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
        datasets = frozenset(d.strip() for d in datasets_raw.split(",") if d.strip())
        acls[name] = datasets
    return acls


# Fail loud at import on malformed configuration (the K8S-MCP convention —
# a typo'd registry must never silently degrade to "no ACLs enforced").
parse_clients(os.environ.get(CLIENTS_ENV, ""))
parse_acls(os.environ.get(ACLS_ENV, ""))


def registry_clients() -> dict:
    """``{key: name}`` for the configured registry (re-read per request)."""
    return parse_clients(os.environ.get(CLIENTS_ENV, ""))


def dataset_acls() -> dict:
    """``{name: frozenset(datasets)}`` (re-read per request)."""
    return parse_acls(os.environ.get(ACLS_ENV, ""))


def registry_configured() -> bool:
    """True when ``RAG_API_KEY_CLIENTS`` is set (D15 enforcement active).

    Read per request: configuring the registry enables enforcement without a
    restart; leaving it unset keeps the single-key behaviour byte-identical.
    """
    return bool(os.environ.get(CLIENTS_ENV, "").strip())


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


def resolve_presented(presented: list) -> "Identity | None":
    """Resolve presented key(s) to an :class:`Identity`, or ``None``.

    Admin keys win (deployment semantics), then registry keys.  A registry
    key with no ACL entry resolves to an identity with NO datasets
    (fail-closed).  Callers decide what ``None`` means for their surface —
    on a protected path it is 401.
    """
    presented = [k for k in (presented or []) if k]
    if not presented:
        return None
    admins = admin_keys()
    if admins and _match(presented, admins):
        return Identity(kind="admin", name=None, datasets=None)
    clients = registry_clients()
    for candidate in presented:
        for key, name in clients.items():
            if hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8")):
                acls = dataset_acls().get(name, frozenset())
                return Identity(kind="client", name=name, datasets=frozenset(acls))
    return None


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


def dataset_allowed(identity: "Identity | None", dataset_name: str) -> bool:
    """May *identity* touch *dataset_name*?

    ``identity is None`` → D15 inactive → allowed (default UX byte-identical).
    Admins → allowed.  Clients → dataset in their ACL, or ``*``.  Everything
    else → denied (fail-closed; an empty ACL grants nothing).
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
