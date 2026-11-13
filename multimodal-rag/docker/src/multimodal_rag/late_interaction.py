"""Multi-vector late interaction (Feature D) — MaxSim machinery + honest gating.

Late interaction (ColBERT-style) keeps ONE embedding vector per TOKEN and
scores a query/document pair as

    maxsim(q, d) = (1 / |Q|) * sum_i max_j ( q_i . d_j )

where Q is the query's token matrix and D the document's.  Unlike a single
pooled vector this preserves term-level alignment, at the cost of storing
tokens_per_doc vectors per document instead of one.

Honesty constraint (wave contract, section 5): the deployment's configured
document embedder is a SINGLE-vector OpenAI-style endpoint and cannot produce
token embeddings.  Nothing in this module fakes token vectors from it — no
hashed-token projection, no splitting of a pooled vector into pseudo-tokens.
When no token-level embedder is configured, resolve_token_embedder returns
:class:'UnconfiguredTokenEmbedder' and every operation that needs token
matrices raises :class:'LateInteractionUnavailable' with a remediation
message.  Features built on top (the vector_store.py sidecar, the search
rerank) fail closed the same way: enabling late interaction without a token
embedder is an error, and a search on a late-enabled dataset with no embedder
degrades to the ordinary dense+BM25 result stamped metadata
['_late'] = "unavailable" — never silently wrong, never a crash.

Token embedder configuration
----------------------------

'resolve_token_embedder' accepts an explicit config dict and falls back to
the environment:

    RAG_TOKEN_EMBEDDER_URL        base URL of an OpenAI-style /embeddings
                                  endpoint that returns TOKEN-level matrices
                                  (list[list[float]] per input)
    RAG_TOKEN_EMBEDDER_MODEL      model name sent in the request body
    RAG_TOKEN_EMBEDDER_API_KEY    optional bearer token
    RAG_TOKEN_EMBEDDER_TIMEOUT_S  request timeout (default 10)

:class:'HttpTokenEmbedder' validates the response shape: an endpoint that
returns one pooled list[float] per input is a single-vector embedder and is
REFUSED (:class:'LateInteractionUnavailable'), never repurposed.  Token
vectors are L2-normalized client-side so maxsim scores are cosine-bounded.

Sidecar collection (Lead-approved design, 2026-11-10)
-----------------------------------------------------

A dataset's main collection is created by rag_system.py (not this module),
and adding a named vector to an EXISTING collection is not supported by this
qdrant-client/server combination — update_collection returns without error
but silently keeps the old schema (verified locally 2026-11-10).  The
late-interaction lane therefore lives in a SIDECAR collection
'<dataset>__late' with a single named multivector 'late' (COSINE +
MultiVectorConfig(comparator=MultiVectorComparator.MAX_SIM)).  Sidecar points
mirror main-collection point ids and carry {'doc_id': <main point id>}
payloads, so the search-side rerank can fetch candidate token matrices by id.

Storage cost
------------

STORAGE_MULTIPLIER_NOTE (and :func:'storage_multiplier') COMPUTE the
per-document cost of the token matrices from token counts and dimension —
the multiplier is arithmetic, not a constant to believe.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

import numpy as np

logger = logging.getLogger(__name__)

#: Name of the multivector inside the sidecar collection.
LATE_VECTOR_NAME = "late"

# -- token-embedder configuration keys ---------------------------------------

TOKEN_EMBEDDER_URL_ENV = "RAG_TOKEN_EMBEDDER_URL"  # env-var NAME (not a credential) # secret-scan:allow
TOKEN_EMBEDDER_MODEL_ENV = "RAG_TOKEN_EMBEDDER_MODEL"
TOKEN_EMBEDDER_API_KEY_ENV = "RAG_TOKEN_EMBEDDER_API_KEY"  # env-var NAME (not a credential) # secret-scan:allow
TOKEN_EMBEDDER_TIMEOUT_ENV = "RAG_TOKEN_EMBEDDER_TIMEOUT_S"  # env-var NAME (not a credential) # secret-scan:allow

_REMEDIATION = (
    "No token-level embedder is configured, so late interaction cannot run. "
    "Set RAG_TOKEN_EMBEDDER_URL (and RAG_TOKEN_EMBEDDER_MODEL, optionally "
    "RAG_TOKEN_EMBEDDER_API_KEY) to an embeddings endpoint that returns "
    "TOKEN-level matrices - list[list[float]] per input, one vector per token "
    "(a ColBERT / late-interaction serving endpoint). The configured document "
    "embedder is single-vector and cannot be substituted; token vectors are "
    "not faked from it."
)


class LateInteractionUnavailable(RuntimeError):
    """Raised when late interaction is requested but cannot be served.

    Callers catch this to DEGRADE honestly (stamp metadata
    ["_late"] = "unavailable" and return the dense+BM25 result) rather than
    to fake a late-interaction score.
    """


class TokenEmbedder(Protocol):
    """Protocol for an endpoint that embeds text into TOKEN matrices.

    'embed_tokens(texts)' returns one matrix per input text: a list of token
    vectors, each a list[float].  Token counts may differ between texts
    (late interaction is ragged by nature); all token vectors of one text
    must share the same dimension.  Implementations should return unit-norm
    token vectors so maxsim scores are cosine-bounded.
    """

    def embed_tokens(self, texts: list[str]) -> list[list[list[float]]]: ...


class UnconfiguredTokenEmbedder:
    """Sentinel implementing :class:'TokenEmbedder' by FAILING CLOSED.

    Every call raises :class:'LateInteractionUnavailable' with the remediation
    message - nothing is scored, nothing is faked.
    """

    def embed_tokens(self, texts: list[str]) -> list[list[list[float]]]:
        raise LateInteractionUnavailable(_REMEDIATION)


class HttpTokenEmbedder:
    """Token embedder backed by an OpenAI-style 'POST {base_url}/embeddings'.

    The endpoint MUST return token-level matrices (data[i].embedding is a
    list[list[float]]).  A response whose embeddings are flat list[float]
    vectors is a single-vector embedder and is REFUSED with
    :class:'LateInteractionUnavailable' - a pooled vector cannot be turned
    into token vectors without inventing data, which this module does not do.
    """

    def __init__(
        self,
        base_url: str,
        model: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 10.0,
    ):
        base_url = str(base_url or "").strip()
        if not base_url:
            raise ValueError("HttpTokenEmbedder requires a non-empty base_url")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"HttpTokenEmbedder base_url must be http(s), got {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.model = (model or "").strip() or None
        self.api_key = (api_key or "").strip() or None
        self.timeout_s = max(0.1, float(timeout_s))

    def label(self) -> str:
        return f"{self.model or 'unspecified-model'}@{self.base_url}"

    def embed_tokens(self, texts: list[str]) -> list[list[list[float]]]:
        texts = [str(t) for t in texts]
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # pragma: no cover - body already consumed
                pass
            raise LateInteractionUnavailable(
                f"token-embedder endpoint {self.base_url}/embeddings returned HTTP {exc.code}: {detail}"
            ) from exc
        except Exception as exc:
            raise LateInteractionUnavailable(
                f"token-embedder endpoint {self.base_url}/embeddings failed: {type(exc).__name__}: {exc}"
            ) from exc

        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list) or len(data) != len(texts):
            got = len(data) if isinstance(data, list) else type(data).__name__
            raise LateInteractionUnavailable(
                f"token-embedder endpoint {self.base_url}/embeddings returned "
                f"{got} entries for {len(texts)} input(s); refusing to guess "
                "the alignment"
            )
        # Honour the documented 'index' field when present (OpenAI shape);
        # otherwise the response order is taken as-is.
        if all(isinstance(item, dict) and isinstance(item.get("index"), int) for item in data):
            data = sorted(data, key=lambda item: item["index"])

        return [self._token_matrix(item) for item in data]

    def _token_matrix(self, item: Any) -> list[list[float]]:
        embedding = item.get("embedding") if isinstance(item, dict) else None
        if not isinstance(embedding, list):
            raise LateInteractionUnavailable(
                f"token-embedder endpoint {self.base_url}/embeddings returned "
                f"{type(embedding).__name__} where a token matrix was expected"
            )
        if not embedding:
            # A text with zero tokens scores -inf in maxsim (ranks last); the
            # shape is legal, so pass it through rather than inventing rows.
            return []
        first = embedding[0]
        if not isinstance(first, list):
            # A pooled single vector: the endpoint is NOT token-level.  Fail
            # closed - this is the honesty constraint of the whole feature.
            raise LateInteractionUnavailable(
                f"embeddings endpoint {self.base_url}/embeddings returned a "
                f"single pooled vector per input (list[float], dim "
                f"{len(embedding)}); late interaction requires TOKEN-level "
                "matrices (list[list[float]]). Point RAG_TOKEN_EMBEDDER_URL at "
                "a late-interaction/ColBERT-style endpoint - the configured "
                "document embedder cannot be substituted and token vectors "
                "will not be faked from pooled vectors."
            )
        dim = len(first)
        matrix: list[list[float]] = []
        for row in embedding:
            if not isinstance(row, list) or len(row) != dim:
                raise LateInteractionUnavailable(
                    f"token-embedder endpoint {self.base_url}/embeddings "
                    "returned a ragged token matrix (token vectors of differing "
                    "length); late interaction requires a shared dimension "
                    "per text"
                )
            matrix.append(_l2_normalized([float(v) for v in row]))
        return matrix


def _l2_normalized(vec: list[float]) -> list[float]:
    """*vec* scaled to unit L2 norm; a zero vector is returned unchanged."""
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    if not np.isfinite(norm) or norm == 0.0:
        return [0.0] * len(vec)
    scaled = arr / np.float32(norm)
    return [float(v) for v in scaled]


def resolve_token_embedder(config: dict[str, Any] | None = None) -> TokenEmbedder:
    """Resolve a :class:'TokenEmbedder' from *config* or the environment.

    *config* keys (each falling back to the matching env var when absent):
    'url', 'model', 'api_key', 'timeout_s'.  When no URL is configured the
    returned embedder is :class:'UnconfiguredTokenEmbedder' - the honest
    sentinel: the deployment's document embedder is single-vector and is
    never used to fake token embeddings.
    """
    cfg = dict(config or {})

    def _pick(key: str, env: str) -> str:
        value = cfg.get(key)
        if value is None or str(value).strip() == "":
            value = os.environ.get(env)
        return str(value or "").strip()

    url = _pick("url", TOKEN_EMBEDDER_URL_ENV)
    if not url:
        return UnconfiguredTokenEmbedder()
    raw_timeout = _pick("timeout_s", TOKEN_EMBEDDER_TIMEOUT_ENV)
    try:
        timeout_s = float(raw_timeout or 10.0)
    except ValueError as exc:
        raise ValueError(f"{TOKEN_EMBEDDER_TIMEOUT_ENV} must be a number, got {raw_timeout!r}") from exc
    return HttpTokenEmbedder(
        url,
        model=_pick("model", TOKEN_EMBEDDER_MODEL_ENV) or None,
        api_key=_pick("api_key", TOKEN_EMBEDDER_API_KEY_ENV) or None,
        timeout_s=timeout_s,
    )


def embedder_label(embedder: TokenEmbedder) -> str:
    """Human-readable identity of *embedder* for the persisted feature block."""
    label = getattr(embedder, "label", None)
    if callable(label):
        return str(label())
    return type(embedder).__name__


# -- MaxSim -------------------------------------------------------------------


def maxsim(query_tokens: list[list[float]], doc_tokens: list[list[float]]) -> float:
    """ColBERT MaxSim between one query token matrix and one document matrix.

    score = (1/|Q|) * sum_i max_j (q_i . d_j) over plain dot products, so
    callers passing unit-norm token vectors get a score in [-1, 1].  The
    score is the MEAN over query tokens; Qdrant's server-side MAX_SIM reports
    the SUM - same ordering for a fixed query, different magnitude.

    Conventions: an empty query scores 0.0 (nothing to satisfy); a document
    with no tokens scores -inf (unscoreable - ranks last, never NaN).  Raises
    ValueError when query and document token dimensions differ (a dimension
    mismatch is a configuration error, not a low score).
    """
    if not query_tokens:
        return 0.0
    if not doc_tokens:
        return float("-inf")
    q = np.asarray(query_tokens, dtype=np.float32)
    d = np.asarray(doc_tokens, dtype=np.float32)
    if q.ndim != 2 or d.ndim != 2:
        raise ValueError(f"maxsim expects 2-D token matrices, got query {q.shape} / document {d.shape}")
    if q.shape[1] != d.shape[1]:
        raise ValueError(
            f"token dimension mismatch: query tokens are {q.shape[1]}-dim, document tokens are {d.shape[1]}-dim"
        )
    # (n_query, n_doc) dot products; max per query token, mean over query.
    sims = q @ d.T
    return float(sims.max(axis=1).sum() / q.shape[0])


def score_documents(
    query_tokens: list[list[float]],
    docs_tokens: list[list[list[float]]],
) -> list[float]:
    """MaxSim of *query_tokens* against each document matrix, input order kept."""
    return [maxsim(query_tokens, doc_tokens) for doc_tokens in docs_tokens]


# -- Sidecar collection schema -------------------------------------------------


def sidecar_collection_name(main_collection: str) -> str:
    """Sidecar collection carrying the 'late' multivector for *main_collection*.

    Idempotent under repetition: a name that already ends in '__late' maps to
    itself, so sidecar-name derivation can never stack the suffix.
    """
    name = str(main_collection)
    if name.endswith("__late"):
        return name
    return name + "__late"


def build_late_vector_params(dim: int) -> Any:
    """VectorParams for the sidecar's named 'late' multivector.

    COSINE + MultiVectorConfig(comparator=MultiVectorComparator.MAX_SIM).
    Built lazily so importing this module never requires qdrant_client.
    """
    dim = int(dim)
    if dim < 1:
        raise ValueError(f"token_dim must be a positive integer, got {dim}")
    from qdrant_client.models import (
        Distance,
        MultiVectorComparator,
        MultiVectorConfig,
        VectorParams,
    )

    return VectorParams(
        size=dim,
        distance=Distance.COSINE,
        multivector_config=MultiVectorConfig(comparator=MultiVectorComparator.MAX_SIM),
    )


def _sidecar_late_params(client: Any, sidecar: str) -> tuple[Any | None, Any | None]:
    """(late vector params, raw error) for *sidecar*; (None, None) when absent."""
    try:
        collection = client.get_collection(sidecar)
    except Exception as exc:  # absent (or unreachable) - caller distinguishes
        return None, exc
    params = getattr(getattr(collection, "config", None), "params", None)
    vectors = getattr(params, "vectors", None)
    late_params = vectors.get(LATE_VECTOR_NAME) if isinstance(vectors, dict) else None
    return late_params, None


def ensure_late_sidecar(client: Any, main_collection: str, dim: int) -> str:
    """Create the '<main>__late' sidecar if absent; validate it if present.

    Idempotent.  Returns the sidecar name.  Raises
    :class:'LateInteractionUnavailable' when a sidecar exists with an
    incompatible shape (missing 'late' vector, different token dimension):
    an existing-but-wrong sidecar must never be silently reused - its stored
    matrices would mis-score every rerank.
    """
    name = sidecar_collection_name(main_collection)
    params = build_late_vector_params(dim)
    late_params, probe_error = _sidecar_late_params(client, name)
    if probe_error is not None:
        # Absent (or unreachable): create fresh.  A create racing another
        # creator surfaces as the backend's own "already exists" error.
        client.create_collection(name, vectors_config={LATE_VECTOR_NAME: params})
        return name
    if late_params is None:
        # PRESENT but without the late vector - never re-create over it
        # (that would silently destroy an existing sidecar's data shape).
        raise LateInteractionUnavailable(
            f"sidecar collection '{name}' exists but has no '{LATE_VECTOR_NAME}' "
            "named vector; delete the collection and re-enable late interaction"
        )
    stored_dim = getattr(late_params, "size", None)
    if stored_dim is not None and int(stored_dim) != int(dim):
        raise LateInteractionUnavailable(
            f"sidecar collection '{name}' was built with token dim {stored_dim}, "
            f"but token_dim={dim} was requested; the stored matrices would "
            "mis-score every rerank. Delete the sidecar collection and "
            "re-ingest the dataset."
        )
    return name


def late_sidecar_dim(client: Any, main_collection: str) -> int | None:
    """The sidecar's stored token dimension, or None when absent/unknown."""
    late_params, _probe_error = _sidecar_late_params(client, sidecar_collection_name(main_collection))
    size = getattr(late_params, "size", None)
    return int(size) if size is not None else None


def delete_late_sidecar(client: Any, main_collection: str) -> bool:
    """Delete the sidecar collection.  True when it existed, False when absent."""
    name = sidecar_collection_name(main_collection)
    try:
        client.get_collection(name)
    except Exception:
        return False
    client.delete_collection(name)
    return True


def delete_late_sidecar_points(client: Any, main_collection: str, point_ids: list[str]) -> None:
    """Remove *point_ids* from the sidecar (mirrors main-collection deletes).

    Best-effort by contract: an absent sidecar is a no-op; transport errors
    propagate so callers can decide whether to log or fail.
    """
    if not point_ids:
        return
    from qdrant_client.models import PointIdsList

    name = sidecar_collection_name(main_collection)
    try:
        client.get_collection(name)
    except Exception:
        return
    client.delete(name, points_selector=PointIdsList(points=list(point_ids)))


# -- Per-dataset state file ----------------------------------------------------

#: Retrieval-plane enablement marker.  The dataset meta's
#: 'features.late_interaction' block (written by the set_late_interaction
#: surfaces via DatasetManager.update_dataset) is the operator-facing record;
#: this small JSON file in the dataset directory is what the retrieval plane
#: (vector_store.py) probes per search, so the retrieval layer never has to
#: parse dataset meta.json.  The set-late-interaction surfaces keep the two in
#: sync; the file is authoritative for search.
LATE_STATE_FILENAME = ".late_interaction.json"


def late_state_path(dataset_dir: str | Path) -> Path:
    return Path(dataset_dir) / LATE_STATE_FILENAME


def read_late_state(dataset_dir: str | Path) -> dict[str, Any]:
    """Read the late-interaction state file, or {} when absent/unreadable.

    A corrupt or wrong-shaped file disables late interaction for the dataset
    (fail closed) rather than raising into the search path.
    """
    try:
        with open(late_state_path(dataset_dir), encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def write_late_state(dataset_dir: str | Path, payload: dict[str, Any] | None) -> None:
    """Write (or, with None, remove) the late-interaction state file."""
    path = late_state_path(dataset_dir)
    if payload is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


# -- Storage multiplier (COMPUTED, never asserted) ------------------------------


def storage_bytes_dense(dim: int, dtype_bytes: int = 4) -> int:
    """Bytes for the ONE pooled dense vector of a document (no quantization)."""
    return int(dim) * int(dtype_bytes)


def storage_bytes_late(tokens_per_doc: int, dim: int, dtype_bytes: int = 4) -> int:
    """Bytes for ONE document's token matrix (tokens x dim, no quantization)."""
    return int(tokens_per_doc) * int(dim) * int(dtype_bytes)


def storage_multiplier(tokens_per_doc: int, dim: int, dtype_bytes: int = 4) -> float:
    """late-bytes / dense-bytes for one document = tokens_per_doc (equal dtype)."""
    dense = storage_bytes_dense(dim, dtype_bytes)
    if dense == 0:
        raise ValueError("dim * dtype_bytes must be non-zero to form a ratio")
    return storage_bytes_late(tokens_per_doc, dim, dtype_bytes) / dense


def storage_note(tokens_per_doc: int = 512, dim: int = 4096, dtype_bytes: int = 4) -> str:
    """The storage arithmetic for one document, COMPUTED from its inputs."""
    dense = storage_bytes_dense(dim, dtype_bytes)
    late = storage_bytes_late(tokens_per_doc, dim, dtype_bytes)
    ratio = storage_multiplier(tokens_per_doc, dim, dtype_bytes)
    return (
        f"Late-interaction storage is COMPUTED, not asserted: one document "
        f"holding {tokens_per_doc} tokens at dim {dim} stores "
        f"{tokens_per_doc} x {dim} x {dtype_bytes} B = {late:,} B "
        f"({late / 1024 / 1024:.1f} MiB) of token matrix, versus "
        f"{dim} x {dtype_bytes} B = {dense:,} B ({dense / 1024:.1f} KiB) for "
        f"the single pooled dense vector - a multiplier of {tokens_per_doc} x "
        f"{dim} x {dtype_bytes} / ({dim} x {dtype_bytes}) = {ratio:.1f}x per "
        f"document. At equal dtype the multiplier equals the token count, so "
        f"it tracks the chunker: the wave contract's ~205x corresponds to "
        f"~205-token chunks, a 2048-token chunk costs 2048x. Assumptions: "
        f"float32 storage, no quantization/compression on the multivector "
        f"lane, tokens counted per stored chunk (the retrieval unit), sidecar "
        f"overhead (point ids, payload) not included."
    )


#: The contract's note - generated by :func:'storage_note' from arithmetic for
#: the representative document (512 tokens at dim 4096), not a hand-written 205x.
STORAGE_MULTIPLIER_NOTE: str = storage_note()
