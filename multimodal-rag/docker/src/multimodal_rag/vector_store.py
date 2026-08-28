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
import uuid
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from multimodal_rag.utils.general_tools import cosine_sim

logger = logging.getLogger(__name__)


def _lightweight_payload_selector():
    """Payload selector that excludes heavy base64 ``image``/``video`` keys.

    Used on the fast search path (no reranker, no VLM, base LLM doesn't
    support image/video) to avoid transferring megabytes of base64 data
    from Qdrant that would be immediately discarded and replaced with
    ``preprocessed_*`` file refs.
    """
    from qdrant_client.models import PayloadSelectorExclude

    return PayloadSelectorExclude(exclude=["metadata.image", "metadata.video"])


@dataclass
class Document:
    page_content: str
    metadata: dict[str, Any] = field(default_factory=dict)


class VectorStore:
    """Minimal base class so ``isinstance(vs, VectorStore)`` keeps working.

    Concrete implementations are :class:`InMemoryVectorStore` and
    :class:`QdrantVectorStore`. The shared retrieval surface
    (``similarity_search_with_score_by_vector`` /
    ``asimilarity_search_with_relevance_scores``) is declared here; scores are
    raw cosine similarity (higher = more similar) for both backends.
    """

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        raise NotImplementedError

    async def aadd_documents(self, documents: list[Document], **kwargs: Any) -> list[str]:
        raise NotImplementedError

    async def asimilarity_search_with_relevance_scores(
        self, query: str, k: int, need_media: bool = True
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
        self, embedding: list[float], k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        entries = [(s["document"], s["vector"]) for s in self.store.values() if s.get("vector") is not None]
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
        self, query: str, k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        emb = await self.embedding.aembed_query(query)
        return self.similarity_search_with_score_by_vector(emb, k, need_media=need_media)


class _QdrantBatcher:
    """Dynamic micro-batcher for concurrent Qdrant similarity searches.

    When multiple search requests call ``asimilarity_search_with_score_by_vector``
    concurrently, each normally fires its own gRPC ``query_batch_points`` call.
    While Qdrant is fast (~16ms/query), the per-call gRPC overhead and thread
    pool contention add up under high concurrency.

    This batcher collects ``(embedding, k)`` pairs arriving within a short
    window (default 5 ms) or until ``max_batch_size`` accumulate, then sends
    them as a single ``query_batch_points`` call with N ``QueryRequest``
    objects.  Qdrant processes the batch in one round-trip and returns
    results in the same order as the requests.

    Each caller awaits a ``Future`` and receives its individual result list
    — the batching is transparent.
    """

    def __init__(
        self,
        search_fn: "Callable[[list[tuple[list[float], int, bool]]], list[list[tuple[Document, float]]]]",
        max_batch_size: int = 32,
        max_wait_ms: float = 5.0,
    ) -> None:
        self._search_fn = search_fn
        self._max_batch_size = max(1, max_batch_size)
        self._max_wait = max(0.001, max_wait_ms / 1000.0)
        self._queue: list[tuple[list[float], int, bool, asyncio.Future[list[tuple[Document, float]]]]] = []
        self._lock: asyncio.Lock | None = None
        self._flush_task: asyncio.Task[None] | None = None

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def submit(self, embedding: list[float], k: int, need_media: bool = True) -> list[tuple[Document, float]]:
        """Submit a single query and await its results."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[list[tuple[Document, float]]] = loop.create_future()
        flush_now = False

        async with self._get_lock():
            self._queue.append((embedding, k, need_media, fut))
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

        queries = [(emb, k, need_media) for emb, k, need_media, _ in batch]
        futs = [f for _, _, _, f in batch]

        loop = asyncio.get_running_loop()
        exec_fut = loop.run_in_executor(None, self._search_fn, queries)

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
    """

    def __init__(
        self,
        client: Any,
        collection_name: str,
        embedding: Any,
        vector_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._client = client
        self.collection_name = collection_name
        self.embedding = embedding
        self.vector_name = vector_name
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

    def similarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        from qdrant_client.models import QueryRequest

        responses = self._client.query_batch_points(
            collection_name=self.collection_name,
            requests=[
                QueryRequest(
                    query=embedding,
                    using=self.vector_name,
                    limit=k,
                    with_payload=True,
                    with_vector=False,
                )
            ],
        )
        out: list[tuple[Document, float]] = []
        for resp in responses:
            for pt in resp.points:
                payload = pt.payload or {}
                meta = dict(payload.get("metadata", {}))
                doc = Document(page_content=payload.get("page_content", ""), metadata=meta)
                out.append((doc, float(pt.score)))
        return out

    def similarity_search_with_score_by_vector_batch(
        self, queries: list[tuple[list[float], int, bool]]
    ) -> list[list[tuple[Document, float]]]:
        """Batch-embed multiple queries in a single Qdrant gRPC call.

        *queries* is a list of ``(embedding, k, need_media)`` tuples.
        When *need_media* is False, heavy base64 ``image``/``video`` payload
        keys are excluded from the Qdrant response to avoid transferring
        megabytes of data that would be discarded on the fast path.
        Returns a list of result lists, one per query, in the same order.
        """
        from qdrant_client.models import QueryRequest

        lightweight = _lightweight_payload_selector()
        requests = [
            QueryRequest(
                query=emb,
                using=self.vector_name,
                limit=k,
                with_payload=True if need_media else lightweight,
                with_vector=False,
            )
            for emb, k, need_media in queries
        ]
        responses = self._client.query_batch_points(
            collection_name=self.collection_name,
            requests=requests,
        )
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

    async def asimilarity_search_with_score_by_vector(
        self, embedding: list[float], k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        """Async batched similarity search by vector.

        Routes through a per-event-loop ``_QdrantBatcher`` so that
        concurrent searches share a single gRPC call.

        When *need_media* is False, heavy base64 ``image``/``video`` payload
        keys are excluded from the Qdrant response (fast path: no reranker,
        no VLM, base LLM doesn't support image/video).
        """
        loop = asyncio.get_running_loop()
        batcher = self._batchers.get(loop)
        if batcher is None:
            batcher = _QdrantBatcher(
                search_fn=self.similarity_search_with_score_by_vector_batch,
                max_batch_size=self._batcher_max_size,
                max_wait_ms=self._batcher_max_wait_ms,
            )
            self._batchers[loop] = batcher
        return await batcher.submit(embedding, k, need_media)

    async def asimilarity_search_with_relevance_scores(
        self, query: str, k: int, need_media: bool = True
    ) -> list[tuple[Document, float]]:
        emb = await self.embedding.aembed_query(query)
        return await self.asimilarity_search_with_score_by_vector(emb, k, need_media)
