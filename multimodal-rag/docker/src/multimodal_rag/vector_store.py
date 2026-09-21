"""Standalone vector store layer for MultimodalRAG.

Replaces the former langchain-based vector store (langchain_core,
langchain_qdrant) with minimal local equivalents exposing only the
surface used by ``rag_system.py``.
Qdrant operations that ``rag_system`` performs directly via ``qdrant_client``
(upsert / scroll / query_batch_points / delete) are unchanged — this module
only supplies the container object holding ``_client`` / ``collection_name`` /
``vector_name`` plus the similarity-search methods used at retrieval time.
"""

import asyncio
import logging
import os
import time
import uuid
import weakref
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

import multimodal_rag.utils.bm25 as bm25_lane
from multimodal_rag.utils.general_tools import cosine_sim

logger = logging.getLogger(__name__)

# Dedicated thread pool for Qdrant I/O (batched searches, upserts, scrolls).
# The sync Qdrant client performs network I/O that used to run either on the
# event loop (freezing every concurrent request for the duration of a
# multi-MB upsert) or on the default executor (where long-running batch
# ingest jobs and ffmpeg subprocesses could starve every search flush).
# Keeping Qdrant calls here gives them their own bounded lane; sized via
# QDRANT_POOL_SIZE.  rag_system imports this pool for the same reason.
_QDRANT_IO_POOL = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("QDRANT_POOL_SIZE", "4"))),
    thread_name_prefix="qdrant-io",
)


def _lightweight_payload_selector():
    """Payload selector that excludes heavy base64 ``image``/``video`` keys.

    Used on the fast search path (no reranker, no VLM, base LLM doesn't
    support image/video) to avoid transferring megabytes of base64 data
    from Qdrant that would be immediately discarded and replaced with
    ``preprocessed_*`` file refs.
    """
    from qdrant_client.models import PayloadSelectorExclude

    return PayloadSelectorExclude(exclude=["metadata.image", "metadata.video"])


# ---------------------------------------------------------------------------
# Metadata-filtered search (roadmap feature 1)
# ---------------------------------------------------------------------------

# The public filter dict accepted by the REST/MCP search surfaces.  Every key
# is optional; conditions are AND-combined:
#
#   file_types:     list[str] — metadata.file_type is one of these
#                   (the ``_classify_file`` labels: pdf, image, video, audio,
#                   text, json, table, code, office, html, xml, yaml,
#                   notebook, ebook, log, unknown)
#   severities:     list[str] — metadata.severities (log entries) contains ANY
#   source_prefix:  str       — metadata.source starts with this prefix
#   date_from:      str       — metadata.timestamp_start >= this (ISO datetime)
#   date_to:        str       — metadata.timestamp_start <= this (ISO datetime)
#
# Docs without the relevant metadata (e.g. video segments carry float
# seconds, not datetimes) simply never match a condition on that key.


def build_payload_filter(filters: dict[str, Any] | None) -> Any:
    """Translate the public search-filter dict into a Qdrant ``Filter``.

    Returns ``None`` when *filters* is empty/None so callers can skip the
    filter entirely.  Unknown keys are ignored (forward compatibility).
    Raises ``ValueError`` on unparseable ``date_from``/``date_to`` values so
    API layers can surface a 400 instead of a silent no-op.
    """
    if not filters:
        return None
    from qdrant_client.models import DatetimeRange, FieldCondition, Filter, MatchAny, MatchPrefix

    must: list[Any] = []

    file_types = [str(v) for v in (filters.get("file_types") or []) if str(v).strip()]
    if file_types:
        must.append(FieldCondition(key="metadata.file_type", match=MatchAny(any=file_types)))

    severities = [str(v) for v in (filters.get("severities") or []) if str(v).strip()]
    if severities:
        must.append(FieldCondition(key="metadata.severities", match=MatchAny(any=severities)))

    prefix = filters.get("source_prefix")
    if isinstance(prefix, str) and prefix.strip():
        must.append(FieldCondition(key="metadata.source", match=MatchPrefix(prefix=prefix.strip())))

    date_from, date_to = filters.get("date_from"), filters.get("date_to")
    if date_from or date_to:
        from datetime import datetime

        def _parse(value: Any, label: str) -> str:
            try:
                datetime.fromisoformat(str(value))
            except ValueError as exc:
                raise ValueError(f"search filter '{label}' must be an ISO-8601 datetime, got {value!r}") from exc
            return str(value)

        range_kwargs: dict[str, str] = {}
        if date_from:
            range_kwargs["gte"] = _parse(date_from, "date_from")
        if date_to:
            range_kwargs["lte"] = _parse(date_to, "date_to")
        must.append(FieldCondition(key="metadata.timestamp_start", range=DatetimeRange(**range_kwargs)))

    if not must:
        return None
    return Filter(must=must)


def filters_to_predicate(filters: dict[str, Any] | None) -> Any:
    """Python counterpart of :func:`build_payload_filter` for in-memory stores.

    Returns ``None`` when *filters* is empty/None, else a predicate over a
    metadata dict.  Dates are compared as parsed datetimes; metadata values
    that do not parse as datetimes (e.g. video segment seconds) never match a
    date condition — mirroring the Qdrant behaviour for non-datetime payload
    values.
    """
    if not filters:
        return None

    file_types = {str(v).strip() for v in (filters.get("file_types") or []) if str(v).strip()}
    severities = {str(v).strip() for v in (filters.get("severities") or []) if str(v).strip()}
    prefix = filters.get("source_prefix")
    prefix = prefix.strip() if isinstance(prefix, str) else ""
    date_from, date_to = filters.get("date_from"), filters.get("date_to")

    from datetime import datetime

    def _parse_dt(value: Any) -> Any:
        if isinstance(value, (int, float)):
            return None  # media segment seconds are not datetimes
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    lo = _parse_dt(date_from) if date_from else None
    hi = _parse_dt(date_to) if date_to else None
    if (date_from and lo is None) or (date_to and hi is None):
        raise ValueError("search filter 'date_from'/'date_to' must be ISO-8601 datetimes")

    def predicate(meta: dict[str, Any]) -> bool:
        if file_types and str(meta.get("file_type")) not in file_types:
            return False
        if severities:
            values = meta.get("severities") or []
            if isinstance(values, str):
                values = [values]
            if not severities.intersection(str(v).strip() for v in values):
                return False
        if prefix and not str(meta.get("source") or "").startswith(prefix):
            return False
        if lo is not None or hi is not None:
            dt = _parse_dt(meta.get("timestamp_start"))
            if dt is None:
                return False
            if lo is not None and dt < lo:
                return False
            if hi is not None and dt > hi:
                return False
        return True

    return predicate


def ensure_search_payload_indexes(client: Any, collection_name: str) -> list[str]:
    """Create the payload indexes metadata-filtered search benefits from.

    Indexes only make filtering fast — absent indexes still filter correctly
    — so creation is best-effort: failures (older Qdrant without a schema
    type, transient unavailability) are logged and ignored.  Idempotent.
    """
    created: list[str] = []
    for field_name, schema in (
        ("metadata.file_type", "keyword"),
        ("metadata.severities", "keyword"),
        ("metadata.source", "keyword"),
        ("metadata.timestamp_start", "datetime"),
    ):
        try:
            client.create_payload_index(collection_name, field_name, field_schema=schema)
            created.append(field_name)
        except Exception as exc:  # pragma: no cover - depends on server version
            logger.debug("payload index %s on %s not created: %s", field_name, collection_name, exc)
    return created


@dataclass
class Document:
    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _points_to_docs(responses: Any) -> list[list[tuple[Document, float]]]:
    """Flatten ``query_batch_points`` responses into per-query result lists.

    Shared by every Qdrant query path so the payload → ``Document``
    reconstruction stays in one place.
    """
    all_results: list[list[tuple[Document, float]]] = []
    for resp in responses:
        results: list[tuple[Document, float]] = []
        for pt in resp.points:
            payload = pt.payload or {}
            meta = dict(payload.get("metadata", {}))
            doc = Document(page_content=payload.get("page_content", ""), metadata=meta)
            results.append((doc, float(pt.score)))
        all_results.append(results)
    return all_results


def _is_fusion_unsupported_error(exc: BaseException) -> bool:
    """True when *exc* unmistakably signals a backend without the fusion query API.

    Conservative by design: only a ``NotImplementedError``, or a
    request-shaped rejection (``ValueError`` from the embedded local mode,
    a 4xx ``UnexpectedResponse`` from a server) whose message mentions the
    query-API surface, counts as "fusion not supported".  Unrelated
    failures — network, auth, timeouts — must propagate, never silently
    downgrade every subsequent search to dense-only.
    """
    if isinstance(exc, NotImplementedError):
        return True
    from qdrant_client.http.exceptions import UnexpectedResponse

    if not isinstance(exc, (ValueError, UnexpectedResponse)):
        return False
    message = str(exc).lower()
    return any(
        marker in message for marker in ("fusion", "rrf", "prefetch", "not supported", "unsupported", "not implemented")
    )


class VectorStore:
    """Minimal base class so ``isinstance(vs, VectorStore)`` keeps working.

    Concrete implementations are :class:`InMemoryVectorStore` and
    :class:`QdrantVectorStore`. The shared retrieval surface
    (``similarity_search_with_score_by_vector`` /
    ``asimilarity_search_with_relevance_scores``) is declared here; scores are
    raw cosine similarity (higher = more similar) for both backends.
    """

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError

    async def aadd_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        raise NotImplementedError

    async def asimilarity_search_with_relevance_scores(
        self, query: str, k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError


class InMemoryVectorStore(VectorStore):
    """In-process store mirroring the former langchain ``InMemoryVectorStore`` surface.

    ``store`` maps ``doc_id -> {"document": Document, "vector": list[float] | None}``,
    matching the layout ``rag_system.py`` reads/writes directly.
    """

    def __init__(self, embedding: Any) -> None:
        self.embedding = embedding
        self.store: dict[str, dict[str, Any]] = {}

    async def aadd_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        ids: list[str] = []
        for doc in documents:
            doc_id = uuid.uuid4().hex
            self.store[doc_id] = {"document": doc, "vector": None}
            ids.append(doc_id)
        return ids

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        entries = [(s["document"], s["vector"]) for s in self.store.values() if s.get("vector") is not None]
        if entries and filters is not None:
            pred = filters_to_predicate(filters)
            if pred is not None:
                entries = [(d, v) for d, v in entries if pred(d.metadata)]
        if not entries:
            return []
        docs, vecs = zip(*entries)
        sims = cosine_sim(
            np.asarray(embedding, dtype=np.float32).reshape(1, -1),
            np.asarray(vecs, dtype=np.float32),
        )[0]
        k = min(k, len(docs))
        top = np.argsort(sims)[-k:][::-1]
        return [(docs[i], float(sims[i])) for i in top]

    async def asimilarity_search_with_relevance_scores(
        self, query: str, k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        emb = await self.embedding.aembed_query(query)
        return self.similarity_search_with_score_by_vector(emb, k, need_media=need_media, filters=filters)


class _QdrantBatcher:
    """Dynamic micro-batcher for concurrent Qdrant similarity searches.

    When multiple search requests call ``asimilarity_search_with_score_by_vector``
    concurrently, each normally fires its own gRPC ``query_batch_points`` call.
    While Qdrant is fast (~16ms/query), the per-call gRPC overhead and thread
    pool contention add up under high concurrency.

    This batcher collects ``(embedding, k, need_media, filters, query_text)``
    requests arriving within a short window (default 5 ms) or until
    ``max_batch_size`` accumulate, then sends
    them as a single ``query_batch_points`` call with N ``QueryRequest``
    objects.  Qdrant processes the batch in one round-trip and returns
    results in the same order as the requests.

    Each caller awaits a ``Future`` and receives its individual result list
    — the batching is transparent.
    """

    def __init__(
        self,
        search_fn: "Callable[[list[tuple[list[float], int, bool, dict[str, Any] | None, str | None]]], list[list[tuple[Document, float]]]]",
        max_batch_size: int = 32,
        max_wait_ms: float = 5.0,
    ) -> None:
        self._search_fn = search_fn
        self._max_batch_size = max(1, max_batch_size)
        self._max_wait = max(0.001, max_wait_ms / 1000.0)
        self._queue: list[
            tuple[
                list[float],
                int,
                bool,
                dict[str, Any] | None,
                str | None,
                asyncio.Future[list[tuple[Document, float]]],
            ]
        ] = []
        self._lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task[None] | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def submit(
        self,
        embedding: list[float],
        k: int,
        need_media: bool = True,
        filters: dict[str, Any] | None = None,
        query_text: str | None = None,
    ) -> list[tuple[Document, float]]:
        """Submit a single query and await its results.

        *query_text* is the raw text query when the caller has one (text
        retrieval) — it lets the store build the BM25 sparse query vector
        for hybrid fusion.  Vector-supplied queries (multimodal) pass
        ``None`` and stay dense-only.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[tuple[Document, float]]] = loop.create_future()
        flush_now = False

        async with self._get_lock():
            self._queue.append((embedding, k, need_media, filters, query_text, fut))
            if len(self._queue) >= self._max_batch_size:
                flush_now = True
            elif self._flush_task is None:
                self._flush_task = loop.create_task(self._timed_flush())

        if flush_now:
            await self._flush()

        return await fut

    async def _timed_flush(self) -> None:
        try:
            await asyncio.sleep(self._max_wait)
        except asyncio.CancelledError:
            return
        await self._flush(caller_task=asyncio.current_task())

    async def _flush(self, caller_task: "asyncio.Task[None] | None" = None) -> None:
        """Drain the queue and send one batched Qdrant query."""
        async with self._get_lock():
            if not self._queue:
                return
            batch = self._queue[:]
            self._queue.clear()
            if self._flush_task is not None and not self._flush_task.done() and self._flush_task is not caller_task:
                self._flush_task.cancel()
            self._flush_task = None

        queries = [(emb, k, need_media, filters, text) for emb, k, need_media, filters, text, _ in batch]
        futs = [f for _, _, _, _, _, f in batch]

        loop = asyncio.get_running_loop()
        exec_fut = loop.run_in_executor(_QDRANT_IO_POOL, self._search_fn, queries)

        # Settle every caller's future from the executor result — even if the
        # task that triggered this flush is cancelled (e.g. a client disconnect),
        # so no queued caller is left hanging forever.
        def _settle(f: "asyncio.Future[list[list[tuple[Document, float]]]]") -> None:
            try:
                results = f.result()
                if len(results) != len(futs):
                    raise RuntimeError(f"Batched Qdrant query returned {len(results)} results for {len(futs)} queries")
                for fut, result in zip(futs, results):
                    if not fut.done():
                        fut.set_result(result)
            except BaseException as exc:  # settle callers regardless
                for fut in futs:
                    if not fut.done():
                        fut.set_exception(exc)

        exec_fut.add_done_callback(_settle)
        await asyncio.shield(exec_fut)


class QdrantVectorStore(VectorStore):
    """Qdrant-backed store wrapping ``qdrant_client`` directly.

    Exposes ``_client`` / ``collection_name`` / ``vector_name`` for the direct
    upsert / scroll / query_batch_points / delete calls in ``rag_system.py``
    (and ``dataset_manager`` / ``api_server`` / ``mcp_server``), plus the
    similarity-search helpers used at retrieval time.

    The collection is expected to store payloads with ``page_content`` and
    ``metadata`` keys (the layout ``rag_system._build_qdrant_vector_store``
    writes), matching the former langchain-qdrant defaults.

    Hybrid retrieval (roadmap feature 2): when the collection carries the
    named ``bm25`` sparse vector, text queries are issued as RRF-fusion
    requests over a dense prefetch (``using=vector_name``) and a sparse
    prefetch (``using=bm25``).  ``bm25_stats_path`` points at the dataset's
    ``.bm25_stats.json`` sidecar (df counts the query vector is weighted
    with); ``None`` — e.g. stores built directly by tests — keeps the flat
    dense-only query, as do legacy unnamed-vector collections and
    vector-supplied (multimodal) queries.
    """

    def __init__(
        self,
        client: Any,
        collection_name: str,
        embedding: Any,
        vector_name: str | None = None,
        bm25_stats_path: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._client = client
        self.collection_name = collection_name
        self.embedding = embedding
        self.vector_name = vector_name
        self.bm25_stats_path = bm25_stats_path
        # Feature-detection caches (None = not probed yet).  Collection
        # schema never changes short of a drop+recreate, and a fusion
        # failure against a given backend is permanent — both are probed
        # once per store instance.
        self._bm25_capable: bool | None = None
        self._fusion_supported: bool | None = None
        # Negative capability results are cached only briefly: a store built
        # before a Recreate (on this or any other worker/pod) must re-probe
        # and adopt the rebuilt collection's bm25 lane without a restart.
        self._bm25_probed_at: float = 0.0
        self._reprobe_s = max(1, int(os.environ.get("RAG_HYBRID_REPROBE_S", "60")))
        # The dense lane's detected vector NAME on bm25-capable collections -
        # lets stores constructed against the legacy unnamed vector target the
        # named dense lane correctly after a Recreate.
        self._dense_vector_name: str | None = None
        # Accept and ignore legacy kwargs (content_payload_key,
        # metadata_payload_key, distance_strategy, ...) for forward-compat.
        self._extra_kwargs = kwargs
        # Dynamic query batcher for concurrent similarity searches.
        # One batcher per event loop — same pattern as the embedding batcher
        # in model_adapters.py.
        self._batcher_max_size = int(os.environ.get("QDRANT_QUERY_BATCH_SIZE", "32"))
        self._batcher_max_wait_ms = float(os.environ.get("QDRANT_QUERY_BATCH_WAIT_MS", "5"))
        self._batchers: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _QdrantBatcher] = (
            weakref.WeakKeyDictionary()
        )

    # -- hybrid (dense + BM25) support ---------------------------------------

    def supports_hybrid(self) -> bool:
        """True when the collection has a `bm25` sparse vector to fuse against.
        Probed via `get_collection` (one round trip, then cached).  The positive
        result is cached permanently - collections only ever GAIN the bm25
        lane through Recreate, which rebuilds it with the lane present.  The
        NEGATIVE result is re-probed after `RAG_HYBRID_REPROBE_S` (default
        60s): stores built before a Recreate - on this or any other
        worker/pod, whose RAG caches the recreate POST cannot clear - then
        self-heal onto the hybrid lane without a restart.

        Requires a *named* dense vector as well - the unnamed default vector
        of legacy collections cannot host a second lane, so those keep the
        flat dense query.  When a named dense vector is detected its NAME
        is remembered (_dense_vector_name) so stores constructed against a
        legacy collection keep targeting the correct lane after a
        Recreate.
        """
        now = time.time()
        if self._bm25_capable is not None and (self._bm25_capable or now - self._bm25_probed_at < self._reprobe_s):
            return self._bm25_capable
        capable = False
        dense_name: str | None = None
        try:
            params = self._client.get_collection(self.collection_name).config.params
            sparse = getattr(params, "sparse_vectors", None) or {}
            vectors = getattr(params, "vectors", None)
            if isinstance(vectors, dict):
                for name in vectors:
                    if name != bm25_lane.BM25_VECTOR_NAME:
                        dense_name = name
                        break
            capable = bool(sparse.get(bm25_lane.BM25_VECTOR_NAME)) and dense_name is not None
        except Exception as exc:
            logger.debug("Hybrid capability probe failed for %s: %s", self.collection_name, exc)
        self._bm25_capable = capable
        self._dense_vector_name = dense_name
        self._bm25_probed_at = now
        return capable

    def _dense_using(self) -> str | None:
        """The dense lane using-target: the detected named vector on
        bm25-capable collections; else the store's configured name (None
        = unnamed default on legacy collections).
        """
        return self._dense_vector_name or self.vector_name

    def _bm25_stats(self) -> dict[str, Any] | None:
        """Fresh df stats for the query weighting, or ``None`` when unusable.

        ``None`` for an empty index too: with no sparse vectors stored yet
        the sparse prefetch could only return nothing, so the request stays
        flat dense until the first ingest lands.
        """
        if not self.bm25_stats_path:
            return None
        try:
            stats = bm25_lane.load_stats(Path(self.bm25_stats_path))
        except Exception as exc:
            logger.debug("BM25 stats load failed for %s: %s", self.bm25_stats_path, exc)
            return None
        return stats if stats.get("n_docs", 0) > 0 else None

    def _query_requests(
        self, queries: list[tuple[list[float], int, bool, dict[str, Any] | None, str | None]], hybrid: bool
    ) -> list[tuple[Any, bool]]:
        """Build one ``QueryRequest`` per queued query.

        Returns ``(request, is_hybrid)`` pairs.  Hybrid requests are the
        fusion form — ``prefetch=[dense, sparse], fusion=RRF`` — with the
        filter pushed into *both* prefetches so each lane searches only the
        filtered subset; anything that cannot go hybrid (no query text, no
        usable stats, env off, fusion known-unsupported) stays the flat
        dense request.  Fusion is per-request, so a batch mixes freely.
        """
        from qdrant_client.models import Fusion, FusionQuery, Prefetch, QueryRequest

        stats = self._bm25_stats() if hybrid else None
        lightweight = _lightweight_payload_selector()
        out: list[tuple[Any, bool]] = []
        for emb, k, need_media, filters, query_text in queries:
            flt = build_payload_filter(filters)
            with_payload: Any = True if need_media else lightweight
            sparse = None
            if stats is not None and query_text:
                try:
                    sparse = bm25_lane.build_query_sparse_vector(query_text, stats)
                except Exception as exc:
                    # The lexical lane must never take retrieval down —
                    # degrade this one query to dense-only, audibly.
                    logger.warning("BM25 query vector build failed — dense-only for this query: %s", exc)
            if sparse is not None:
                out.append(
                    (
                        QueryRequest(
                            query=FusionQuery(fusion=Fusion.RRF),
                            prefetch=[
                                Prefetch(query=emb, using=self._dense_using(), limit=k, filter=flt),
                                Prefetch(query=sparse, using=bm25_lane.BM25_VECTOR_NAME, limit=k, filter=flt),
                            ],
                            limit=k,
                            with_payload=with_payload,
                            with_vector=False,
                        ),
                        True,
                    )
                )
            else:
                out.append(
                    (
                        QueryRequest(
                            query=emb,
                            using=self._dense_using(),
                            limit=k,
                            with_payload=with_payload,
                            with_vector=False,
                            filter=flt,
                        ),
                        False,
                    )
                )
        return out

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        """Flat dense-only search (no query text available on this path).

        Text queries go through :meth:`asimilarity_search_with_relevance_scores`
        / the batcher, which is where hybrid fusion happens; this direct
        vector-supplied entry point (multimodal queries, dedup probes) keeps
        today's single-lane request shape.
        """
        from qdrant_client.models import QueryRequest

        from multimodal_rag.utils.metrics import observe_qdrant

        with observe_qdrant("query_batch_points"):
            responses = self._client.query_batch_points(
                collection_name=self.collection_name,
                requests=[
                    QueryRequest(
                        query=embedding,
                        using=self._dense_using(),
                        limit=k,
                        with_payload=True,
                        with_vector=False,
                        filter=build_payload_filter(filters),
                    )
                ],
            )
        return _points_to_docs(responses)

    def similarity_search_with_score_by_vector_batch(
        self, queries: list[tuple[list[float], int, bool, dict[str, Any] | None, str | None]]
    ) -> list[list[tuple[Document, float]]]:
        """Batch-execute multiple queries in a single Qdrant gRPC call.

        *queries* is a list of ``(embedding, k, need_media, filters,
        query_text)`` tuples.  When *need_media* is False, heavy base64
        ``image``/``video`` payload keys are excluded from the Qdrant
        response to avoid transferring megabytes of data that would be
        discarded on the fast path.  Returns a list of result lists, one
        per query, in the same order.

        Hybrid: text queries on a bm25-capable collection (with
        ``RAG_HYBRID_SEARCH`` on and non-empty df stats) become RRF-fusion
        requests.  Backends without the fusion query API (older servers,
        older qdrant-client local mode) are detected ONCE — the first
        failure whose exception unmistakably signals the feature gap — and
        the store permanently degrades to dense-only instead of retrying
        per query; every other exception propagates.
        """
        from multimodal_rag.utils.metrics import SEARCH_HYBRID, observe_qdrant

        hybrid = self._fusion_supported is not False and self.supports_hybrid() and bm25_lane.hybrid_search_enabled()
        requests = self._query_requests(queries, hybrid)
        any_hybrid = any(flag for _, flag in requests)

        def _run(reqs: list[Any]) -> list[Any]:
            with observe_qdrant("query_batch_points"):
                return self._client.query_batch_points(collection_name=self.collection_name, requests=reqs)

        try:
            executed = requests
            responses = _run([req for req, _ in executed])
        except Exception as exc:
            if not any_hybrid or not _is_fusion_unsupported_error(exc):
                raise
            # Older backend without prefetch/fusion: mark the store
            # dense-only permanently (probe once, not per query) and retry.
            self._fusion_supported = False
            logger.warning(
                "Qdrant backend does not support prefetch/fusion queries (%s: %s) — "
                "collection '%s' falls back to dense-only retrieval",
                type(exc).__name__,
                exc,
                self.collection_name,
            )
            executed = self._query_requests(queries, False)
            responses = _run([req for req, _ in executed])

        # Count what actually ran — a hybrid-intent request that fell back is
        # a dense search (the fallback label would otherwise overstate fusion).
        for _, is_hybrid in executed:
            SEARCH_HYBRID.labels(mode="hybrid" if is_hybrid else "dense").inc()

        return _points_to_docs(responses)

    def _batcher(self) -> _QdrantBatcher:
        """Per-event-loop batcher (created lazily, one per running loop)."""
        loop = asyncio.get_running_loop()
        batcher = self._batchers.get(loop)
        if batcher is None:
            batcher = _QdrantBatcher(
                search_fn=self.similarity_search_with_score_by_vector_batch,
                max_batch_size=self._batcher_max_size,
                max_wait_ms=self._batcher_max_wait_ms,
            )
            self._batchers[loop] = batcher
        return batcher

    async def asimilarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        """Async batched similarity search by vector.

        Routes through a per-event-loop ``_QdrantBatcher`` so that
        concurrent searches share a single gRPC call.

        When *need_media* is False, heavy base64 ``image``/``video`` payload
        keys are excluded from the Qdrant response (fast path: no reranker,
        no VLM, base LLM doesn't support image/video).  Vector-supplied
        queries carry no text, so they never take the hybrid lane.
        """
        return await self._batcher().submit(embedding, k, need_media, filters, None)

    async def asimilarity_search_with_relevance_scores(
        self, query: str, k: int, need_media: bool = True, filters: dict[str, Any] | None = None
    ) -> list[tuple[Document, float]]:
        """Async batched search for a *text* query — the hybrid entry point.

        The raw query text travels with the embedding through the batcher so
        the executed ``QueryRequest`` can become an RRF-fusion request over
        the dense + BM25 lanes (bm25-capable collections only; everything
        else keeps the flat dense request).
        """
        emb = await self.embedding.aembed_query(query)
        return await self._batcher().submit(emb, k, need_media, filters, query if isinstance(query, str) else None)
