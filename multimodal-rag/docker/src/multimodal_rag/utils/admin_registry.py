"""File-backed admin overlay for the D15 clients registry (fleet decision D17).

The operator-side counterpart to the D16 access store: per-user keys are
minted and granted from the ``/access`` page (with an ADMIN key) instead of
hand-editing env vars — which a pod cannot do.  The overlay is a single JSON
file on the shared PVC:

    {DATA_PATH}/access/clients.json
    {
      "clients": {
        "alice": {"key": "…", "datasets": ["reports", "notes"]},
        "bob":   {"key": "…", "datasets": ["*"]}
      }
    }

D21 — OIDC sign-in fields (optional, per client entry):

* ``"oidc": "<alias>"`` — binds this client to the JWT identity whose
  ``RAG_OIDC_IDENTITY_CLAIM`` (or ``sub``) resolves to ``<alias>``: the
  verified token then adopts the CLIENT's name and datasets (both
  credentials, one identity — same client_id).  A JWT whose name matches a
  client's name by convention needs no alias.  Must satisfy the registry
  name rules when present.
* ``"blocked": true`` — the identity (by name or alias) resolves to NOTHING:
  a minted key stops authenticating and a matching JWT 401s downstream.
  Env-registry names cannot be blocked (env has no place to say it).

Resolution (the union is computed by ``clients_registry.resolve_presented``
via :func:`overlay_clients` / :func:`overlay_acls` — both re-read per call):

    effective registry = RAG_API_KEY_CLIENTS env  ∪  overlay file
    effective ACLs     = RAG_DATASET_ACLS env     ∪  overlay file

An overlay entry NEVER widens admin power: only the deployment keys
(``RAG_API_KEY`` + the MCP key set) are admin — overlay clients are registry
clients with an ACL, exactly like env clients.  A name present in BOTH env
and overlay keeps its ENV key (env wins on conflict — the operator's
hand-written config is authoritative; delete the env entry to let the page
own the name).  ACLs are merged per name (env ∪ overlay datasets) rather
than replaced, so neither source silently revokes the other.

Security posture:

* The file holds PLAINTEXT keys — the same trust posture as
  ``RAG_API_KEY_CLIENTS`` (which is env/Secret material anyway) and the D16
  store's saved passwords.  0600, atomic temp+replace under the
  cross-process fcntl lock, mtime-cached reads.
* Key generation uses ``secrets.token_urlsafe`` (128-bit entropy).
* Key material is NEVER returned by any listing endpoint in full — mint
  responses carry it ONCE (the only time the operator can copy it);
  listings show a masked prefix (``0HeC9a…``).
* ``RAG_ACCESS_ADMIN_FILE`` (default: enabled when ``RAG_ACCESS_STORE`` is
  on) gates the whole module; when off it is inert and resolves nothing.

This module is RAG-local, stdlib-only, dependency-free; it never logs key
material.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from multimodal_rag.utils import access_store as _base
from multimodal_rag.utils.clients_registry import ALL_DATASETS, GRANT_RO, GRANT_RW

ADMIN_FILE_ENABLED_ENV = "RAG_ACCESS_ADMIN_FILE"
CLIENTS_FILE = "clients.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_mtime_cache: dict[str, tuple[int, int, dict]] = {}
_mtime_lock = threading.Lock()
_mtime_stamp: dict[str, tuple[int, int]] = {}

# Keep the writer lock machinery from access_store (per-path threading lock
# serializing in-process writers before they reach the fcntl lock).
_writer_locks: dict[str, threading.Lock] = {}
_writer_locks_guard = threading.Lock()


def admin_file_enabled() -> bool:
    """True when the admin overlay is switched on.

    Default: follows ``RAG_ACCESS_STORE`` (the D16 knob) — the overlay is
    meaningless without the per-identity store directory anyway.  An
    explicit ``RAG_ACCESS_ADMIN_FILE=0`` turns it off even then.
    """
    raw = os.environ.get(ADMIN_FILE_ENABLED_ENV, "").strip().lower()
    if raw in ("0", "false", "no"):
        return False
    if raw in ("1", "true", "yes"):
        return True
    return _base.store_enabled()


def _file_path() -> Path:
    return Path(os.environ.get("DATA_PATH", "/data")) / "access" / CLIENTS_FILE


def _validate_name(name: str) -> str:
    name = str(name).strip()
    if not _NAME_RE.match(name):
        raise ValueError("client name must be 1-64 chars of letters, digits, '.', '_' or '-' (must start alphanumeric)")
    return name


def valid_name(name: str) -> bool:
    """True when *name* satisfies the registry name rules (D21: the JWT
    identity claim must yield a name of exactly this shape — shared rules,
    not a parallel vocabulary)."""
    try:
        return bool(_NAME_RE.match(str(name).strip()))
    except Exception:
        return False


def _validate_oidc_alias(alias: str) -> str:
    alias = str(alias).strip()
    if not valid_name(alias):
        raise ValueError(
            "the 'oidc' alias must be 1-64 chars of letters, digits, '.', '_' or '-' "
            "(must start alphanumeric) — it names the JWT identity it binds"
        )
    return alias


def _load() -> dict:
    """Load the overlay file (mtime-cached; missing/corrupt → empty doc)."""
    path = _file_path()
    try:
        st = path.stat()
    except OSError:
        with _mtime_lock:
            _mtime_cache.pop(str(path), None)
        return {}
    key = str(path)
    stamp = (st.st_mtime_ns, st.st_size)
    with _mtime_lock:
        cached = _mtime_cache.get(key)
        if cached and (cached[0], cached[1]) == stamp:
            return cached[2]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        doc = {}
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("clients"), dict):
        doc["clients"] = {}
    with _mtime_lock:
        _mtime_cache[key] = (stamp[0], stamp[1], doc)
    return doc


def _writer_lock(key: str) -> threading.Lock:
    with _writer_locks_guard:
        lk = _writer_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _writer_locks[key] = lk
        return lk


def _mutate(fn) -> dict:
    """Read-modify-write the overlay under the file lock (atomic replace)."""
    path = _file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path)
    with _writer_lock(key), _base._cross_process_lock(path.with_suffix(".lock")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            doc = {}
        except (json.JSONDecodeError, OSError):
            doc = {}
        if not isinstance(doc, dict) or not isinstance(doc.get("clients"), dict):
            doc = {"clients": {}}
        if not fn(doc):
            return doc
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            st = path.stat()
            with _mtime_lock:
                _mtime_cache[key] = (st.st_mtime_ns, st.st_size, doc)
        except OSError:
            pass
        return doc


# ---------------------------------------------------------------------------
# Public surface: the overlay view of the registry (merged into clients_registry)
# ---------------------------------------------------------------------------


def overlay_clients() -> dict:
    """``{key: name}`` from the overlay file (empty when disabled).

    Merged into the env registry by callers — on key conflicts with the env
    registry the ENV key wins (callers apply env first; a duplicate key with
    a different name is resolved by the env registry taking precedence).

    D21 (lead fix, 2026-10): a ``"blocked": true`` entry is FILTERED OUT —
    the docstring has always promised a blocked client's minted key stops
    authenticating, but the key previously survived the merge and kept its
    identity + datasets (runtime-verified gap; the JWT path was correctly
    blocked).  Fail-closed wins: block means BOTH credentials die.
    """
    if not admin_file_enabled():
        return {}
    clients: dict[str, str] = {}
    for name, entry in _load().get("clients", {}).items():
        if isinstance(entry, dict) and entry.get("key"):
            if entry.get("blocked") is True:
                continue
            clients[str(entry["key"])] = str(name)
    return clients


def overlay_acls() -> dict:
    """``{name: {dataset: mode}}`` from the overlay file (empty when off).

    D26: each ``datasets`` element is EITHER a plain string (``"reports"``
    = rw — every pre-D26 file unchanged, byte-compatible) OR an object
    ``{"name": "reports", "mode": "ro" | "rw"}``.  Unknown modes FAIL
    CLOSED (the dataset is dropped from the grants) — a hand-edited file
    can never widen a key's write power by typo.  The ``*`` wildcard is a
    plain string or ``{"name": "*", "mode": ...}`` exactly like any other
    token.
    """
    if not admin_file_enabled():
        return {}
    acls: dict[str, dict] = {}
    for name, entry in _load().get("clients", {}).items():
        if not isinstance(entry, dict):
            continue
        ds = entry.get("datasets")
        grants: dict[str, str] = {}
        if isinstance(ds, list):
            for d in ds:
                if isinstance(d, dict):
                    ds_name = str(d.get("name") or "").strip()
                    if not ds_name:
                        continue
                    mode = str(d.get("mode") or "").strip().lower()
                    if mode == GRANT_RO:
                        grants[ds_name] = GRANT_RO
                    elif mode == GRANT_RW:
                        grants[ds_name] = GRANT_RW
                    else:
                        grants.pop(ds_name, None)  # unknown mode → no grant (fail-closed)
                elif d:
                    grants[str(d)] = GRANT_RW
        acls[str(name)] = grants
    return acls


def overlay_entries() -> dict:
    """``{name: entry}`` — the overlay client entries verbatim (D21).

    The resolver's view of the D21 fields (``oidc`` alias, ``blocked`` flag):
    it matches by alias BEFORE falling back to plain name-convention
    matching, and treats ``blocked: true`` as a resolve-to-nothing.  Empty
    when the overlay is disabled.  Entries are returned as loaded (a
    hand-edited file's odd fields stay inert — only the documented fields
    are ever written by mint/patch).
    """
    if not admin_file_enabled():
        return {}
    return {str(name): e for name, e in _load().get("clients", {}).items() if isinstance(e, dict)}


# ---------------------------------------------------------------------------
# Admin operations (the /access page's admin panel)
# ---------------------------------------------------------------------------


def generate_key() -> str:
    """A fresh 128-bit client key (the only time key material is handed out)."""
    return secrets.token_urlsafe(18)


# ---------------------------------------------------------------------------
# D25 SSO self-mint — the identity mints its OWN key
# ---------------------------------------------------------------------------

SELF_MINT_ENV = "RAG_ACCESS_SELF_MINT"


def self_mint_enabled() -> bool:
    """True when RAG_ACCESS_SELF_MINT opts the deployment in (D25).

    Read per call (the house convention — flip without a restart).  The
    overlay must be enabled too: there is nowhere to write the self-minted
    entry otherwise.  Default unset = the feature is INERT (byte-identical
    behaviour).
    """
    if not admin_file_enabled():
        return False
    return os.environ.get(SELF_MINT_ENV, "").strip().lower() in ("1", "true", "yes")


def own_key_view(name: str) -> dict:
    """The OWNER-SAFE view of an identity's overlay entry (D25).

    Everything the identity itself may learn about its own key: whether one
    exists (``has_key``), the MASKED prefix (never the material — even the
    owner re-reads the masked form; the full key is returned exactly once,
    at mint time), its grants, and the D21 flags that bear on it.  Unlike
    the admin listing this refuses NOTHING for being env-authoritative —
    the view is read-only; only the mint/rotate WRITE refuses env names.
    An empty-key entry (auto-minted by a D23 grant or a flag PATCH) reads
    ``has_key: False`` — it authenticates nothing, so the user may mint.
    """
    if not admin_file_enabled():
        raise _base.SelectionDenied("The admin key registry is not enabled on this deployment.")
    name = _validate_name(name)
    from multimodal_rag.utils.clients_registry import CLIENTS_ENV, dataset_acls, parse_clients

    env_names = set(parse_clients(os.environ.get(CLIENTS_ENV, "")).values())
    entry = _load().get("clients", {}).get(name)
    if not isinstance(entry, dict):
        entry = {}
    key = str(entry.get("key") or "")
    grants = dataset_acls().get(name, {})
    return {
        "name": name,
        "has_key": bool(key),
        "key_masked": (key[:6] + "…" + key[-2:]) if len(key) > 10 else ("…" if key else None),
        "datasets": sorted(grants),
        "grants": [{"name": n, "mode": m} for n, m in sorted(grants.items())],
        "oidc": str(entry.get("oidc")) if entry.get("oidc") else None,
        "blocked": bool(entry.get("blocked")),
        "env_managed": name in env_names,
        "created": entry.get("created"),
    }


def self_mint_key(name: str, rotate: bool = False) -> dict:
    """Mint (or deliberately rotate) the key of the CALLER'S OWN identity.

    The D25 write half.  Rules, all fail-closed:

    * *name* comes from the VERIFIED identity (a JWT/cookie/proxy claim),
      never from user input — a caller can only ever mint for itself.
    * Grants are NEVER touched: the minted key carries exactly the
      identity's current ACLs (env ∪ overlay — the merged registry resolves
      them on the next request).  Self-mint widens nothing; it hands an
      EXISTING identity a second credential.
    * An ENV-authoritative name refuses (the overlay cannot shadow the env
      registry — the operator owns that key).
    * A ``blocked: true`` entry refuses (a blocked identity resolves to
      nothing; it certainly cannot mint).
    * Minting over an EXISTING usable key requires ``rotate=True`` — the
      deliberate, confirm-guarded path (the old key stops authenticating
      immediately).  An empty-key entry (auto-minted by grants/flags) is
      mintable without the flag: it never authenticated anything.
    * The generated key is returned ONCE (the only full-key response in the
      D25 surface); every later view is masked.
    """
    if not self_mint_enabled():
        raise _base.SelectionDenied("SSO self-mint is not enabled on this deployment (RAG_ACCESS_SELF_MINT).")
    name = _validate_name(name)
    from multimodal_rag.utils.clients_registry import CLIENTS_ENV, parse_clients

    env_names = set(parse_clients(os.environ.get(CLIENTS_ENV, "")).values())
    if name in env_names:
        # Env-authoritative name — refuse REGARDLESS of whether an overlay
        # entry exists: the env registry owns that key's lifecycle, and a
        # self-mint could otherwise rotate only the overlay half while the
        # env key keeps authenticating for the same identity (the operator
        # decides; own_key_view reports env_managed so the UI hides the
        # mint/rotate affordances for these names).
        raise KeyError(
            f"Identity '{name}' is managed by the env registry (RAG_API_KEY_CLIENTS) — "
            "ask an operator to mint/rotate its key."
        )
    result: dict = {}

    def _apply(doc: dict) -> bool:
        entry = doc["clients"].get(name)
        if not isinstance(entry, dict):
            entry = {"key": "", "datasets": []}
            doc["clients"][name] = entry
        if entry.get("blocked") is True:
            raise PermissionError(f"Identity '{name}' is blocked — it cannot mint a key.")
        existing_key = str(entry.get("key") or "")
        if existing_key and not rotate:
            raise FileExistsError(f"Identity '{name}' already has an API key — pass rotate to replace it.")
        new_key = generate_key()
        entry["key"] = new_key
        entry.setdefault("datasets", [])
        entry["created"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result["created"] = True
        result["key"] = new_key
        return True

    _mutate(_apply)
    return {
        "name": name,
        "key": result["key"],
        "rotated": rotate,
        "created": result.get("created", False),
    }


def mint_client(name: str, datasets: list | None = None, key: str | None = None) -> dict:
    """Create (or rotate the key of) an overlay client.

    *name* must pass :func:`_validate_name`.  *datasets* is the client's ACL
    (list; ``["*"]`` = everything; empty = nothing until granted).  D26: each
    element is a plain string (rw) OR ``{"name": ..., "mode": "ro"|"rw"}``;
    a ``"name:mode"`` STRING (``"reports:ro"``) is accepted as the env-style
    shorthand and normalized to the object form.  When *key* is given it
    replaces the stored one (rotation — e.g. the operator pastes a known
    value); otherwise a fresh key is generated and returned in the result
    dict (the ONLY copy the caller ever gets).
    """
    if not admin_file_enabled():
        raise _base.SelectionDenied("The admin key registry is not enabled on this deployment.")
    name = _validate_name(name)
    ds = _normalize_grants(datasets or [])
    new_key = (str(key).strip() if key else "") or generate_key()
    if ":" in new_key or ";" in new_key or len(new_key) < 8:
        raise ValueError("custom key must be >=8 chars without ':' or ';'")
    result: dict = {}

    def _apply(doc: dict) -> bool:
        existing = doc["clients"].get(name)
        rotated = bool(existing and existing.get("key") != new_key)
        entry = {"key": new_key, "datasets": ds}
        if isinstance(existing, dict):
            # Rotation must not silently drop the D21 OIDC binding/block —
            # key rotation is about the KEY, not the identity's JWT wiring.
            if "oidc" in existing:
                entry["oidc"] = existing["oidc"]
            if "blocked" in existing:
                entry["blocked"] = existing["blocked"]
        doc["clients"][name] = entry
        result["rotated"] = rotated
        return True

    _mutate(_apply)
    return {"name": name, "key": new_key, "datasets": ds, "rotated": result.get("rotated", False)}


def set_client_flags(name: str, *, oidc: str | None = None, blocked: bool | None = None) -> dict:
    """Set (or clear) the D21 fields on an overlay client.

    Pass ``oidc=""`` to CLEAR the alias, ``blocked=False`` to UNBLOCK.
    ``None`` leaves the field untouched.  The key and dataset grants are
    untouched.  Used by the REST PATCH handler (extend-grant shape) — no new
    route; a JWT-only blocked name that was never minted gets a minted entry
    with an empty grant so the block survives anywhere ``blocked`` can live.
    """
    if not admin_file_enabled():
        raise _base.SelectionDenied("The admin key registry is not enabled on this deployment.")
    name = _validate_name(name)
    alias: str | None = None
    if oidc is not None:
        alias = _validate_oidc_alias(oidc) if str(oidc).strip() else ""

    def _apply(doc: dict) -> bool:
        entry = doc["clients"].get(name)
        if not isinstance(entry, dict):
            # Mint an empty-grant entry so a JWT-only name can be blocked /
            # alias-bound without hand-crafting a key first.
            entry = {"key": generate_key(), "datasets": []}
            doc["clients"][name] = entry
        if alias is not None:
            if alias:
                entry["oidc"] = alias
            else:
                entry.pop("oidc", None)
        if blocked is not None:
            entry["blocked"] = bool(blocked)
        return True

    _mutate(_apply)
    doc = _load().get("clients", {}).get(name, {})
    return {
        "name": name,
        "oidc": doc.get("oidc") or None,
        "blocked": bool(doc.get("blocked")),
    }


def revoke_client(name: str) -> bool:
    """Remove an overlay client entirely (its key stops authenticating on the
    next request — everything is re-read per call).  Returns True when the
    name existed.  An ENV client with the same name is NOT touched (the env
    registry keeps authority over its own entries)."""
    if not admin_file_enabled():
        raise _base.SelectionDenied("The admin key registry is not enabled on this deployment.")
    name = _validate_name(name)
    removed = {"v": False}

    def _apply(doc: dict) -> bool:
        if name in doc["clients"]:
            del doc["clients"][name]
            removed["v"] = True
            return True
        return False

    _mutate(_apply)
    return removed["v"]


def grant_datasets(name: str, datasets: list | None = None) -> dict:
    """Replace an overlay client's dataset grant (the checkbox set).

    D23 (lead fix, 2026-10): an UNKNOWN name is AUTO-MINTED (empty key,
    the saved grants as its ACL) instead of 404ing — observed JWT-only
    identities (D21 ``observed.json`` rows) are users: granting them IS
    minting them, and the admin panel offers Grants… on observed rows.
    The auto-mint is fail-closed by construction: no key material exists
    until the operator explicitly mints/rotates one (the empty-key entry
    authenticates NOTHING), and ``set_client_flags`` already used this
    exact pattern for Block/OIDC-bind on observed rows.  ENV-authoritative
    names still refuse here (the overlay cannot shadow the env registry —
    delete the env entry to manage the name from the page).

    D26: *datasets* accepts plain strings (rw), ``{"name", "mode"}``
    objects, and ``"name:mode"`` shorthand strings; the stored grant is
    normalized to objects so modes survive the save and the merged
    registry honors them on the next request.
    """
    if not admin_file_enabled():
        raise _base.SelectionDenied("The admin key registry is not enabled on this deployment.")
    name = _validate_name(name)
    ds = _normalize_grants(datasets or [])

    def _apply(doc: dict) -> bool:
        entry = doc["clients"].get(name)
        if not isinstance(entry, dict):
            if name in os.environ.get("RAG_API_KEY_CLIENTS", ""):
                # Env-authoritative name: the overlay must not shadow it.
                raise KeyError(name)
            entry = {"key": "", "datasets": ds, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            doc["clients"][name] = entry
            return True
        entry["datasets"] = ds
        return True

    try:
        _mutate(_apply)
    except KeyError:
        raise KeyError(f"Client '{name}' does not exist in the admin registry (mint it first).")
    return {"name": name, "datasets": ds}


def _validate_dataset_token(d: str) -> None:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", d) and d != "*":
        raise ValueError(f"invalid dataset token {d!r}")


def _normalize_grants(datasets: list | None) -> list[dict | str]:
    """Normalize a REST/PATCH grants payload to the D26 stored form.

    Accepted inputs: a plain name string (→ rw string, wire-legacy form),
    a ``"name:mode"`` shorthand string (→ ``{"name", "mode"}``), and a
    ``{"name": ..., "mode": "ro"|"rw"}`` object.  ``*`` keeps the string
    form.  Duplicate names: LAST entry wins (deterministic).  Raises
    ``ValueError`` on unknown modes / malformed tokens — the REST PATCH
    maps that to 400 (fail loud, the D15 convention).
    """
    out: list[dict | str] = []
    seen: dict[str, dict | str] = {}
    for raw in datasets or []:
        if isinstance(raw, dict):
            ds_name = str(raw.get("name") or "").strip()
            mode = str(raw.get("mode") or "rw").strip().lower()
            if not ds_name:
                raise ValueError("grant object requires a 'name'")
            if mode not in (GRANT_RO, GRANT_RW):
                raise ValueError(f"invalid grant mode {mode!r} (expected 'ro' or 'rw')")
            _validate_dataset_token(ds_name)
            seen[ds_name] = {"name": ds_name, "mode": mode}
        elif isinstance(raw, str):
            token = raw.strip()
            if not token:
                continue
            base, msep, mode = token.partition(":")
            base = base.strip()
            if msep:
                if base != ALL_DATASETS:
                    _validate_dataset_token(base)
                if mode not in (GRANT_RO, GRANT_RW):
                    raise ValueError(f"invalid grant mode {mode!r} in token {token!r}")
                if base == ALL_DATASETS:
                    seen[ALL_DATASETS] = {"name": ALL_DATASETS, "mode": mode}
                else:
                    seen[base] = {"name": base, "mode": mode}
            else:
                if token != ALL_DATASETS:
                    _validate_dataset_token(token)
                seen[token] = token  # legacy wire form for plain-rw grants
        else:
            # A TYPE problem (neither string nor object) — TRY004: TypeError,
            # not ValueError.  The REST PATCH maps both to 400.
            raise TypeError(f"invalid dataset grant {raw!r} (expected a name string or {{name, mode}})")
    for key in sorted(seen):
        out.append(seen[key])
    return out


def list_clients() -> list:
    """The overlay registry for the admin UI — key material MASKED.

    D21: entries carry their ``oidc`` alias / ``blocked`` flag when set, and
    OBSERVED JWT-only identities (authenticated with a valid token but never
    minted an entry — ``{DATA_PATH}/access/observed.json``) appear as
    separate rows ``{"name", "source": "observed", "key": null,
    "datasets": [], "last_seen": …}`` so the /access page can mint or block
    them.  A name that already exists in the overlay or env registry is not
    repeated as observed (its row carries the information instead).
    """
    from multimodal_rag.utils.clients_registry import CLIENTS_ENV, dataset_acls, parse_clients

    env_clients = parse_clients(os.environ.get(CLIENTS_ENV, ""))
    doc = _load()
    out = []
    for name in sorted(doc.get("clients", {}).keys()):
        entry = doc["clients"][name]
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "")
        env_name = env_clients.get(key)
        grants = dataset_acls().get(name, {})
        row = {
            "name": name,
            "key_masked": (key[:6] + "…" + key[-2:]) if len(key) > 10 else "…",
            "datasets": sorted(grants),
            # D26: the mode-carrying form — legacy flat list kept alongside
            # for older consumers.
            "grants": [{"name": n, "mode": m} for n, m in sorted(grants.items())],
            "source": "env+overlay" if env_name == name else "overlay",
            "created": entry.get("created"),
        }
        if entry.get("oidc"):
            row["oidc"] = str(entry["oidc"])
        if entry.get("blocked"):
            row["blocked"] = True
        out.append(row)
    # Env-only clients are listed too (visible, not editable here — the env
    # is authoritative for them).
    for key, env_name in env_clients.items():
        if env_name in doc.get("clients", {}):
            continue
        env_grants = dataset_acls().get(env_name, {})
        out.append(
            {
                "name": env_name,
                "key_masked": (key[:6] + "…" + key[-2:]) if len(key) > 10 else "…",
                "datasets": sorted(env_grants),
                "grants": [{"name": n, "mode": m} for n, m in sorted(env_grants.items())],
                "source": "env",
                "created": None,
            }
        )
    # D21: JWT-only users — observed at least once, never minted/granted.
    try:
        from multimodal_rag.utils.oidc_identity import observed_identities

        known = {row["name"] for row in out}
        for obs in observed_identities():
            if obs.get("name") in known:
                continue
            out.append(
                {
                    "name": obs.get("name"),
                    "key": None,
                    "key_masked": None,
                    "datasets": [],
                    "source": "observed",
                    "created": None,
                    "last_seen": obs.get("last_seen"),
                }
            )
    except Exception:  # telemetry sidecar broken → the registry still lists
        pass
    return sorted(out, key=lambda c: c["name"])


def verify_client_key(name: str, key: str) -> bool:
    """Constant-time check that *key* is the stored key of overlay *name*
    (used by tests; the servers authenticate via the merged registry)."""
    entry = _load().get("clients", {}).get(name)
    if not isinstance(entry, dict):
        return False
    return hmac.compare_digest(str(entry.get("key") or ""), str(key))
