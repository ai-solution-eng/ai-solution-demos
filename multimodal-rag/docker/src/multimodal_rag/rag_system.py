import asyncio
import base64
import contextvars
import os
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cached_property
from io import BytesIO
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np

import multimodal_rag.utils.bm25 as bm25_lane
from multimodal_rag.utils.general_tools import (
    cosine_sim,
    list_chunker,
    retry_call,
    sync_wrapper_safe,
)
from multimodal_rag.utils.logging_utils import logging
from multimodal_rag.utils.model_adapters import (
    MultiModalEmbeddings,
    MultiModalReranker,
)
from multimodal_rag.utils.pcai_model_classes import (
    ChatModel,
    EmbeddingModel,
    RerankerModel,
    VoiceModel,
)
from multimodal_rag.vector_store import (
    _QDRANT_IO_POOL,
    Document,
    InMemoryVectorStore,
    QdrantVectorStore,
    VectorStore,
    ensure_search_payload_indexes,
    filters_to_predicate,
)

logger = logging.getLogger(__name__)

# Dedicated pool for media-side subprocess/CPU work (ffmpeg resize/transcode,
# ffmpeg probe) inside async ingest.  Previously this shared the *default*
# executor with the Qdrant search batcher, so concurrent uploads could stall
# every search on the pod; it now has its own bounded lane (MEDIA_POOL_SIZE).
_MEDIA_POOL = ThreadPoolExecutor(
    max_workers=max(1, int(os.environ.get("MEDIA_POOL_SIZE", "2"))),
    thread_name_prefix="media-io",
)

__all__ = ["MultiModalRAGSystem", "MultimodalRAG", "Postprocessor", "Preprocessor"]

# Non-fatal captioning warnings for the CURRENT ingest request, surfaced to
# the web UI.  The API layer sets a list per request; the ASR/VLM failure
# paths append to it (no-op when unset, e.g. during retrieval).
_ingest_warnings: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("ingest_warnings", default=None)


def _record_ingest_warning(msg: str) -> None:
    """Record a non-fatal captioning failure for the active ingest request."""
    bucket = _ingest_warnings.get()
    if bucket is not None:
        bucket.append(msg)


# ---------------------------------------------------------------------------
# Federated multi-dataset search helpers (roadmap feature 8)
# ---------------------------------------------------------------------------


class _DedupItem(NamedTuple):
    """One scored result travelling through the generic dedup pass.

    ``doc``/``score`` are the retrieval pair; ``dataset`` is ``None`` for the
    single-dataset twin collapse and the dataset name for the federated merge
    (where the identity key is dataset-qualified).
    """

    doc: Any
    score: float
    dataset: str | None = None


def federated_identity_key(dataset: str, doc: Any) -> tuple | None:
    """Dataset-qualified twin-identity key: ``(dataset, source, page, chunk_index, time-window)``.

    The same identity ``MultimodalRAG._dedup_twins`` uses inside one dataset,
    extended with the dataset name so two datasets that happen to store files
    under the same path (or literally the same backup restored twice) are NOT
    collapsed against each other — each hit keeps its own dataset label.
    Returns ``None`` for docs that cannot collide (not a dict, or no source).
    """
    if isinstance(doc, dict):
        src = doc.get("source", "")
        if src:
            return (
                dataset,
                src,
                doc.get("page"),
                doc.get("chunk_index"),
                (doc.get("timestamp_start"), doc.get("timestamp_end")),
            )
    return None


def resolve_federated_targets(
    dm: Any,
    datasets: "list[str] | str",
    is_unlocked: "Callable[[str], bool] | None" = None,
) -> tuple[list[str], list[dict[str, str]], list[dict[str, str]]]:
    """Resolve a federated-search ``datasets`` argument to concrete names.

    Returns ``(targets, skipped, errors)``:

    * ``targets`` — dataset names to search, de-duplicated, order preserved.
    * ``skipped`` — password-protected datasets NOT unlocked for this
      caller, as ``{"dataset": …, "reason": …}`` notes.  Federated search
      deliberately accepts no ``password`` argument (the v3.0.0 decision to
      keep passwords out of tool signatures) — a locked dataset is skipped
      with a note, never a hard failure.
    * ``errors`` — named datasets that do not exist, as
      ``{"dataset": …, "error": …}`` notes.

    ``datasets`` accepts a list of names, a single name, or the string
    ``"all"`` — which expands to every dataset that is readable WITHOUT a
    password: no password set, or unlocked per the *is_unlocked* predicate
    (the caller's in-process unlock cache, scoped to the requesting client).
    Locked datasets are silently absent from the ``"all"`` expansion (they
    are listed under ``skipped`` so the caller knows why results are
    missing).

    ``dm`` needs only ``get_dataset(name)``, ``list_datasets()`` and
    ``has_password(name)`` — the ``DatasetManager`` API.  Raises
    ``ValueError`` when *datasets* is none of the accepted shapes.

    Blocking I/O (NFS meta reads) — callers offload this to a thread pool.
    """
    check_unlocked = is_unlocked or (lambda name: False)

    targets: list[str] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    def _consider(name: str, *, from_all: bool = False) -> None:
        name = (name or "").strip()
        if not name or name in targets:
            return
        try:
            dm.get_dataset(name, sync_count=False)
        except FileNotFoundError:
            if not from_all:
                errors.append({"dataset": name, "error": f"Dataset '{name}' not found."})
            return
        if dm.has_password(name) and not check_unlocked(name):
            skipped.append(
                {
                    "dataset": name,
                    "reason": (
                        "Password protected and not unlocked for this session "
                        "(unlock it first; federated search accepts no password argument)."
                    ),
                }
            )
            return
        targets.append(name)

    if isinstance(datasets, str):
        if datasets.strip().lower() == "all":
            for meta in dm.list_datasets():
                _consider(str(meta.get("name", "")), from_all=True)
        else:
            _consider(datasets)
    elif isinstance(datasets, (list, tuple)):
        for entry in datasets:
            if isinstance(entry, str) and entry.strip().lower() == "all":
                for meta in dm.list_datasets():
                    _consider(str(meta.get("name", "")), from_all=True)
            elif isinstance(entry, str):
                _consider(entry)
            elif entry is not None:
                errors.append({"dataset": str(entry), "error": "Dataset names must be strings."})
    else:
        raise TypeError("'datasets' must be a list of dataset names or the string 'all'.")

    return targets, skipped, errors


def dedup_federated_results(entries: list[tuple[str, Any, float]]) -> list[tuple[str, Any, float]]:
    """Cross-dataset twin/text dedup over a merged multi-dataset pool.

    *entries* are ``(dataset_name, doc, score)`` triples.  Collapsing uses the
    dataset-qualified identity key (:func:`federated_identity_key`), so twins
    and duplicate chunks collapse *within* a dataset exactly as
    ``MultimodalRAG._dedup_twins`` does, while the same identity in two
    different datasets survives as two labelled hits.
    """
    if not entries:
        return []
    items = [_DedupItem(doc=doc, score=score, dataset=ds) for ds, doc, score in entries]
    out = MultimodalRAG._dedup_by_identity(items, lambda item: federated_identity_key(item.dataset, item.doc))
    return [(item.dataset, item.doc, item.score) for item in out]


def merge_federated_results(
    entries: list[tuple[str, Any, float]],
    *,
    dedup: bool = True,
) -> list[tuple[str, Any, float]]:
    """Merge per-dataset result pools into one ranked, dataset-labelled pool.

    Dedups on the dataset-qualified identity key (optional) and sorts by
    score, descending — scores are embedder cosine similarities from the same
    server-side embedder, so they are comparable across datasets.  Ties keep
    the caller's dataset order (Python's stable sort).
    """
    if not entries:
        return []
    if dedup:
        entries = dedup_federated_results(entries)
    return sorted(entries, key=lambda e: e[2], reverse=True)


async def _arerank_with(
    rank: Any,
    extract_doc: "Callable[[Any], Any]",
    query: str | dict[str, Any],
    results: list[tuple[Any, float]],
    reranker_top_k: int = 3,
) -> list[tuple[Any, float]]:
    """Cross-encoder rerank over ``(doc, score)`` pairs using an existing
    reranker instance (``rank.arerank``) and doc extractor.

    The shared implementation behind :meth:`MultimodalRAG._arerank_results`
    (single dataset) and the federated merged-pool rerank in the MCP/API
    servers — the reranker is content-based, so it does not care which
    dataset a candidate came from.
    """
    reranked = await rank.arerank(query, [extract_doc(d) for d, _ in results])
    # reranked[0] is a list of result dicts, e.g.
    # [{"index": 2, "relevance_score": 0.95, ...},
    #  {"index": 0, "relevance_score": 0.87, ...}]
    # Map each index back to its reranker score, then attach
    # both the embedding score and the reranker score to each doc.
    score_by_idx: dict[int, float] = {}
    for r in reranked[0] if reranked else []:
        score_by_idx[r.get("index", -1)] = r.get("relevance_score", r.get("score", 0.0))
    paired: list[tuple[Any, float]] = []
    for i, (d, emb_score) in enumerate(results):
        rerank_score = score_by_idx.get(i, 0.0)
        if isinstance(d, dict):
            # Copy the doc before annotating so we never mutate the
            # caller's dicts (e.g. the `documents=` path passes the
            # caller's own objects through here).
            d = dict(d)
            d["_embedding_score"] = round(emb_score, 4)
            d["_reranker_score"] = round(rerank_score, 4)
        paired.append((d, rerank_score))
    paired.sort(key=lambda x: x[1], reverse=True)
    return paired[:reranker_top_k]


def _qdrant_prefer_grpc() -> bool:
    """Whether Qdrant clients should prefer gRPC. Defaults to False (HTTP)
    to preserve the single-replica chart's behaviour; the scale chart sets
    ``QDRANT_PREFER_GRPC=true`` for higher throughput at high QPS."""
    return os.environ.get("QDRANT_PREFER_GRPC", "false").lower() in ("true", "1", "yes")


def _qdrant_client_timeout():
    """Hard timeout (seconds) for Qdrant client calls. Defaults to None
    (no timeout) to preserve the original behaviour; the scale chart sets
    ``QDRANT_CLIENT_TIMEOUT=30`` so a hung Qdrant cannot pin a sync_pool
    thread indefinitely."""
    raw = os.environ.get("QDRANT_CLIENT_TIMEOUT")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _qdrant_quantization_config():
    """Scalar quantization config for new Qdrant collections.

    Defaults to int8 (4x memory reduction, ~1-2% recall loss, faster
    search). Set ``QDRANT_QUANTIZATION=none`` to disable, or
    ``QDRANT_QUANTIZATION=int8`` to be explicit. No rescoring is used
    — the 1-2% recall loss is acceptable without the extra full-
    precision reranking pass.

    ``QDRANT_QUANTIZATION_ALWAYS_RAM`` (default ``true``) controls whether
    quantized vectors are pinned in RAM for fast search. Disable
    (``false``) for very large datasets (2M+ points) to avoid OOM —
    quantized vectors will be read from disk per-search instead.

    Only applies to *newly created* collections; existing collections
    keep their original config.
    """
    mode = os.environ.get("QDRANT_QUANTIZATION", "int8").lower().strip()
    if mode in ("none", "off", "false", "0"):
        return None

    always_ram = os.environ.get("QDRANT_QUANTIZATION_ALWAYS_RAM", "true").lower() in ("true", "1", "yes")

    if mode not in ("int8", "scalar", "true", "1", ""):
        logger.warning("Unknown QDRANT_QUANTIZATION='%s', falling back to int8", mode)

    from qdrant_client.models import ScalarQuantization, ScalarQuantizationConfig, ScalarType

    return ScalarQuantization(
        scalar=ScalarQuantizationConfig(
            type=ScalarType.INT8,
            quantile=0.99,
            always_ram=always_ram,
        ),
    )


# ---------------------------------------------------------------------------
# Media helpers (shared by Preprocessor & Postprocessor)
# ---------------------------------------------------------------------------


def _as_url_list(val: Any) -> list[str]:
    if val is None:
        return []
    return [val] if isinstance(val, str) else list(val)


def _split_media(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Return {audio: [...], image: [...], video: [...]} from a doc dict."""
    return {k: _as_url_list(doc.get(k)) for k in ("audio", "image", "video")}


def _preferred_media(doc: dict[str, Any]) -> dict[str, list[str]]:
    """Like :func:`_split_media` but prefer tier-2 ``preprocessed_*`` refs.

    Each media key (``image`` / ``video`` / ``audio``) holds the tier-3
    model-ready data URL used for embedding (e.g. a 1 fps, ≤720×720 video
    segment).  A parallel ``preprocessed_image`` / ``preprocessed_video`` /
    ``preprocessed_audio`` key (when present) points at the tier-2
    preprocessed file on the PVC (e.g. the full video at ≤720p @ 24 fps),
    which is what the base LLM and end user should see by default.

    Use this helper at *retrieval* time when surfacing media URLs to the LLM
    or end user.  VLM captioning and ASR should keep using
    :func:`_split_media` so they operate on the tier-3 data URL already
    stored in Qdrant — no file read or re-resize needed.
    """
    out: dict[str, list[str]] = {}
    for k in ("audio", "image", "video"):
        preproc = doc.get(f"preprocessed_{k}")
        if preproc:
            out[k] = _as_url_list(preproc)
        else:
            out[k] = _as_url_list(doc.get(k))
    return out


# -- Text-only twin gating -----------------------------------------------------
# A text-only twin is only useful when the document carries *real* extracted
# text (e.g. a PDF page).  Documents whose text is merely a media placeholder
# ("[Image: DSC01391.JPG]") or an ingest-time VLM/ASR caption ("[Image
# description]: …", "[Audio transcription]: …") have nothing meaningful for a
# text-only embedding to capture, so they should NOT get a twin.
_CAPTION_LINE_RE = re.compile(
    r"\[\s*(?:Image description|Video description|Audio transcription|Video audio transcription)\s*\][^\n]*(?:\n(?!\s*(?:Image description|Video description|Audio transcription|Video audio transcription)\s*\]).*)*",
    re.IGNORECASE,
)
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"\[\s*(?:Image|Video|Audio)\s*:[^\]]*\]\s*(?:\([^)]*\))?"
    r"(?:\s*\[\s*\d+(?:\.\d+)?\s*s\s*(?:-|–|—)\s*\d+(?:\.\d+)?\s*s\s*\])?",
    re.IGNORECASE,
)


def _has_real_text(text: str) -> bool:
    """True if *text* contains any content beyond captions/placeholders."""
    remaining = _CAPTION_LINE_RE.sub("", text)
    remaining = _MEDIA_PLACEHOLDER_RE.sub("", remaining)
    return bool(remaining.strip())


def _strip_captions(text: str) -> str:
    """Remove auto-generated caption blocks (VLM/ASR) from *text*.

    The caption lines added by the Preprocessor ([Image description]: ...,
    [Video description]: ..., [Audio transcription]: ...) are metadata
    for retrieval-time LLM consumption; they are not part of the media
    content and should not steer the embedding. _CAPTION_LINE_RE also
    matches the trailing newline, so concatenating remaining parts is clean.
    """
    return _CAPTION_LINE_RE.sub("", text)


def _strip_embed_caption(doc: dict, supported: set[str]) -> dict:
    """Return a copy of *doc* with auto-caption text stripped for embedding.

    Caption stripping applies ONLY to pure media docs — those whose text
    carries no real extracted content (just a "[Image: x.JPG]" placeholder
    and/or a VLM/ASR caption).  For those, the raw image/video is what the
    embedder should encode, not the caption wording, so the caption lines
    are removed from the embedding input while the stored payload keeps
    them for retrieval-time LLM consumption.

    Docs that DO carry real text (e.g. a PDF page that also has an image)
    are returned unchanged: the embedder should receive that extracted text
    together with the VLM caption.  Docs whose media modality is not
    supported by the embedder (e.g. audio today) are also unchanged — the
    caption text is the only embeddable content.
    """
    supported_media = [m for m in ("image", "video", "audio") if doc.get(m)]
    if not supported_media or not any(m in supported for m in supported_media):
        return doc
    if _has_real_text(doc.get("text") or ""):
        return doc
    out = dict(doc)
    out["text"] = _strip_captions(doc.get("text") or "")
    return out


def _has_caption(text: str) -> bool:
    """True if *text* contains any ingest-time VLM/ASR caption line."""
    return bool(_CAPTION_LINE_RE.search(text or ""))


# ---------------------------------------------------------------------------
# Hybrid dense + BM25 retrieval — ingest-side helpers (roadmap feature 2)
# ---------------------------------------------------------------------------
# The lexical lane is computed client-side per stored document and stored as
# a named sparse vector next to the dense one; the per-dataset df counts it
# is weighted with live in a ``.bm25_stats.json`` sidecar (dataset_manager).


def _bm25_indexable_text(text: str) -> str:
    """The text worth indexing in the BM25 lane for one document.

    Bare media placeholders ("[Image: x.jpg]", "[Video: v.mp4] [0s – 32s]")
    carry no tokens worth indexing and are stripped; ingest-time VLM/ASR
    caption text is KEPT — it is the only searchable content of caption
    twins and exactly what text queries match against.  (This differs from
    :func:`_has_real_text`, which strips captions too: that gate decides
    embedding inputs, this one decides lexical index inputs.)
    """
    return _MEDIA_PLACEHOLDER_RE.sub("", text or "").strip()


def _detect_dense_vector_name(client: Any, collection_name: str) -> str | None:
    """Name of the dense vector of an existing collection, or ``None``.

    Legacy collections (created before the hybrid schema) hold a single
    *unnamed* default vector — Qdrant reports that as a bare
    ``VectorParams`` rather than a mapping — and must be queried flat, with
    ``using=None``.  Named-vector collections (hybrid schema) report a dict
    whose first entry is the dense lane (``"dense"``).
    """
    try:
        vectors = client.get_collection(collection_name).config.params.vectors
        if isinstance(vectors, dict) and vectors:
            return next(iter(vectors))
    except Exception as exc:
        logger.debug("Vector-name detection failed for %s: %s", collection_name, exc)
    return None


def _bm25_ingest_context(vs: VectorStore) -> dict[str, Any] | None:
    """Per-ingest BM25 state, or ``None`` when the lexical lane doesn't apply.

    Applies only to Qdrant stores whose collection is bm25-capable (named
    ``dense`` + ``bm25`` vectors — see ``QdrantVectorStore.supports_hybrid``)
    with a stats sidecar path and ``RAG_HYBRID_SEARCH`` on.  The df stats
    snapshot is loaded once per ingest call and updated in memory per
    sub-batch; ``dirty`` accumulates the per-doc term counts so the sidecar
    is persisted ONCE at the end (serialising the whole vocabulary per
    sub-batch would repeat the O(n²) write pattern the .hashes.json
    deferred-write fix removed).
    """
    if not isinstance(vs, QdrantVectorStore):
        return None
    stats_path = getattr(vs, "bm25_stats_path", None)
    if not stats_path:
        return None
    if not vs.supports_hybrid() or not bm25_lane.hybrid_search_enabled():
        return None
    return {
        "stats_path": Path(stats_path),
        # Working copy: load_stats returns the mtime-cached object, and this
        # snapshot is mutated per sub-batch — mutating the cache itself would
        # make the end-of-ingest locked persist re-merge the same counts.
        "stats": bm25_lane.copy_stats(bm25_lane.load_stats(Path(stats_path))),
        "dirty": [],
    }


def _bm25_sparse_vectors(sub_docs: list[Document], ctx: dict[str, Any] | None) -> list[Any]:
    """One BM25 ``SparseVector`` per stored doc (``None`` where not applicable).

    Only docs with indexable text get a lane.  Term counts also fold into
    the in-memory df snapshot BEFORE weighting, so the vectors of this batch
    see the same df view later points will (a term appearing in every doc of
    a tiny corpus is correctly weighted as uninformative).
    """
    out: list[Any] = [None] * len(sub_docs)
    if ctx is None:
        return out
    for i, doc in enumerate(sub_docs):
        text = _bm25_indexable_text(doc.page_content)
        if not text:
            continue
        tf = bm25_lane.term_counts(text)
        if not tf:
            continue
        bm25_lane.merge_doc(ctx["stats"], tf)
        ctx["dirty"].append(tf)
        out[i] = bm25_lane.to_sparse_vector(bm25_lane.bm25_doc_weights(tf, ctx["stats"]))
    return out


def _bm25_persist_stats(ctx: dict[str, Any] | None) -> None:
    """Persist the ingest's df deltas under the cross-process lock (once).

    A crash before this point loses only the df counts of the stored prefix
    — the sparse vectors are already on the points, so idf drifts slightly
    until the stats catch up (never corrupt: idf stays positive).
    """
    if ctx is not None and ctx["dirty"]:
        try:
            bm25_lane.record_documents(ctx["stats_path"], ctx["dirty"])
        except Exception as exc:
            logger.warning("Could not persist BM25 df stats (%s) — idf weighting may drift: %s", ctx["stats_path"], exc)


def _has_embeddable_content(doc: dict, embed_modalities: set[str]) -> bool:
    """True if *doc* carries anything that can actually be embedded.

    A doc is embeddable when it still holds a media modality the embedder can
    ingest, or non-placeholder text (a VLM/ASR caption that conversion
    produced, or real extracted content).  A doc reduced to mere media
    placeholders ("[Image: x.jpg]", "[Video: game.mp4] [0s – 32s]", …) is
    *not* embeddable and is dropped.

    This is the unified "skip entirely" rule across all media types: if the
    embedder can't ingest the media and no VLM/ASR was able to convert it to
    caption text, the sample is omitted rather than stored as a dead vector.
    """
    if any(k in embed_modalities and doc.get(k) for k in ("image", "video", "audio")):
        return True
    text = (doc.get("text") or "").strip()
    remaining = _MEDIA_PLACEHOLDER_RE.sub("", text)
    return bool(remaining.strip())


def _media_caption_twin_needed(doc: dict, supported: set[str]) -> bool:
    """Whether a pure-media doc should also get a *caption twin* embedding.

    The base embedding of a pure-media doc (an image/video whose text is
    only a "[Image: x.JPG]" placeholder plus an optional ingest-time VLM/ASR
    caption) keys off the raw media — the caption lines are stripped from the
    embedding input by :func:`_strip_embed_caption` so the vector is driven by
    the pixels, not the caption wording.  A caption twin is an *additional*
    embedding of the same media **with** the caption text, so text queries
    that match the caption wording can still retrieve the point even when the
    base media-only embedding misses.

    A caption twin is only worth creating when all of these hold:

    * the doc carries at least one media modality the embedder supports
      natively (otherwise the Preprocessor has already collapsed the media to
      caption text and that text embedding *is* the caption-only path),
    * the doc has no real extracted text (pure media placeholder — docs that
      carry real text already embed text+caption together and get a
      text-only twin instead),
    * at least one caption line is present (otherwise the twin would embed
      byte-identical input to the base and add nothing).

    When the embedder does NOT support the doc's media and no VLM/ASR is
    available, the Preprocessor drops the media entirely and nothing is
    embedded — matching the "skip it entirely" rule.
    """
    if not any(doc.get(m) for m in ("image", "video", "audio")):
        return False
    if not any(m in supported for m in ("image", "video", "audio") if doc.get(m)):
        return False
    if _has_real_text(doc.get("text") or ""):
        return False
    return _has_caption(doc.get("text") or "")


async def _afetch_media_bytes(url: str, async_client=None) -> bytes:
    if url.startswith(("http://", "https://")):
        if async_client is not None:
            resp = await async_client.get(url, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
        import httpx

        # Bounded timeout: this per-call client previously had none, so a
        # hung media server would hang the enclosing asyncio.gather forever.
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as c:
            resp = await c.get(url, follow_redirects=True)
            resp.raise_for_status()
            return resp.content
    if url.startswith("data:"):
        import re

        m = re.match(r"data:[^;]+;base64,(.+)", url)
        return base64.b64decode(m.group(1)) if m else base64.b64decode(url.split(",", 1)[1])
    path = url.removeprefix("file://")
    with open(path, "rb") as f:
        return f.read()


def _file_url_to_data_url(url: str) -> str:
    """Convert a ``file://`` path to an inline ``data:`` URL.

    HTTP/S and ``data:`` URLs are returned unchanged.  This lets the
    remote VLM consume files stored on the local PVC.

    If the local file does not exist the original URL is returned so
    callers can decide how to handle the missing file.
    """
    if url.startswith(("http://", "https://", "data:")):
        return url
    import mimetypes
    import os

    path = url.removeprefix("file://")
    if not os.path.exists(path):
        logger.warning("File not found, returning URL as-is: %s", path)
        return url
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _infer_mime(url: str) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(url)
    return mime or "application/octet-stream"


def _pvc_to_http_url(url: str) -> str:
    """Convert a ``file://`` or PVC path to a clickable HTTP URL.

    Uses the ``MEDIA_BASE_URL`` environment variable (set via Helm
    ConfigMap) to build an API-accessible URL.  When ``MEDIA_BASE_URL`` is
    not set the original *url* is returned unchanged.

    Examples::

        file:///data/datasets/my-ds/files/abc.jpg
        → https://rag.example.com/api/datasets/my-ds/files/abc.jpg

        /data/datasets/my-ds/files/abc.jpg
        → https://rag.example.com/api/datasets/my-ds/files/abc.jpg
    """
    media_base = os.environ.get("MEDIA_BASE_URL", "")
    if not media_base:
        return url

    data_path = os.environ.get("DATA_PATH", "/data")
    pvc_prefix = f"{data_path}/datasets/"
    file_prefix = f"file://{pvc_prefix}"

    path = url
    if url.startswith(file_prefix):
        path = url[len(file_prefix) :]
    elif url.startswith(pvc_prefix):
        path = url[len(pvc_prefix) :]
    else:
        return url  # not a PVC path

    # path is now "{dataset_name}/files/{filename}"
    return f"{media_base}/api/datasets/{path}"


def _media_url_to_displayable(url: str) -> str:
    """Convert any stored media URL to a displayable/clickable form.

    ``file://`` and raw PVC paths become HTTP URLs (via
    :func:`_pvc_to_http_url`).  ``data:``, ``http://``, ``https://`` and
    ``s3://`` URLs are returned unchanged.
    """
    if url.startswith(("data:", "http://", "https://", "s3://")):
        return url
    return _pvc_to_http_url(url)


# ---------------------------------------------------------------------------
# Shared media-conversion helpers (used by both Preprocessor & Postprocessor)
# ---------------------------------------------------------------------------


async def _transcribe_media(url: str, asr: VoiceModel) -> str:
    """Fetch audio from *url* and transcribe via *asr*.

    Handles ``data:`` URLs (inline base64), ``http(s)://`` (fetched),
    and ``file://`` / bare paths (read from disk).
    """
    if url.startswith("data:"):
        import re

        m = re.match(r"data:[^;]+;base64,(.+)", url)
        data = base64.b64decode(m.group(1)) if m else base64.b64decode(url.split(",", 1)[1])
        mime = url.split(";")[0].replace("data:", "") or "audio/mpeg"
    else:
        data = await _afetch_media_bytes(url)
        mime = _infer_mime(url)
    buf = BytesIO(data)
    buf.name = f"audio.{mime.split('/')[-1]}"
    result = await asr.asr_async_function_call(file=(buf.name, buf, mime))
    return result.text


_DESCRIBE_SYSTEM_PROMPT_SIMPLE = (
    "You are a detailed image captioning assistant. "
    "Describe the image(s) and video(s) in this document thoroughly: "
    "include visible text, objects, people, scene context, and any "
    "relationships between them. Be precise and factual."
)

_DESCRIBE_SYSTEM_PROMPT_WITH_SOURCE = (
    "You are a detailed image captioning assistant. "
    "Describe the image(s) and video(s) in this document thoroughly: "
    "include visible text, objects, people, scene context, and any "
    "relationships between them. Be precise and factual.\n\n"
    "If source or page information is provided above, include it in "
    "your response so the document can be properly referenced."
)


async def _describe_doc(
    doc_dict: dict[str, Any],
    query: str | dict[str, Any] | None,
    vlm: ChatModel,
    system_prompt: str,
    log_timing: bool = False,
    max_media_per_prompt: int = 4,
) -> str:
    """Describe images/videos in *doc_dict* via *vlm*.

    Builds OpenAI-compatible content parts from source info, text, an
    optional user query, and any image/video URLs (converted from
    ``file://`` to data URLs).  Returns the VLM's text response.

    When the document has more than *max_media_per_prompt* media items,
    they are split across multiple VLM calls and the descriptions
    concatenated (the VLM endpoint limits images per prompt).

    When *log_timing* is True, logs the elapsed time and media count at
    verbose level.
    """
    t0 = time.monotonic() if log_timing else 0.0

    source_info = ""
    for key in ("source", "page", "file"):
        val = doc_dict.get(key)
        if val is not None:
            source_info += f"[{key}]: {val}\n"

    # Collect all media URLs as data URLs
    image_urls: list[str] = []
    for url in _as_url_list(doc_dict.get("image", [])):
        data_url = _file_url_to_data_url(url)
        if data_url and not data_url.startswith("file://"):
            image_urls.append(data_url)
    video_urls: list[str] = []
    for url in _as_url_list(doc_dict.get("video", [])):
        data_url = _file_url_to_data_url(url)
        if data_url and not data_url.startswith("file://"):
            video_urls.append(data_url)

    all_media = [("image_url", u) for u in image_urls] + [("video_url", u) for u in video_urls]
    n_media = len(all_media)

    # Build the text-only content parts (shared across all calls)
    text_parts: list[dict[str, Any]] = []
    if source_info:
        text_parts.append({"type": "text", "text": source_info.strip()})
    text = doc_dict.get("text", "")
    if text:
        text_parts.append({"type": "text", "text": text})
    q_text = query if isinstance(query, str) else (query.get("text", "") if isinstance(query, dict) else "")
    if q_text:
        text_parts.append({"type": "text", "text": f"User query: {q_text}"})

    if n_media <= max_media_per_prompt:
        # Single call — all media fits
        content_parts = list(text_parts)
        for media_type, url in all_media:
            content_parts.append({"type": media_type, media_type: {"url": url}})
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content_parts or ""},
        ]
        response = await vlm.llm_async_chat_function_call(messages)
    else:
        # Split media across multiple VLM calls, concatenate descriptions
        # (this branch only runs when n_media > max_media_per_prompt).
        descriptions: list[str] = []
        for i in range(0, n_media, max_media_per_prompt):
            batch = all_media[i : i + max_media_per_prompt]
            content_parts = list(text_parts)
            content_parts.append(
                {
                    "type": "text",
                    "text": f"(Describing images {i + 1}-{min(i + max_media_per_prompt, n_media)} of {n_media})",
                }
            )
            for media_type, url in batch:
                content_parts.append({"type": media_type, media_type: {"url": url}})
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content_parts or ""},
            ]
            resp = await vlm.llm_async_chat_function_call(messages)
            descriptions.append(resp.choices[0].message.content)
        response_descriptions = descriptions

    if log_timing:
        logger.verbose(  # type: ignore[attr-defined]
            "  %.2fs vlm describe  — %d media items",
            time.monotonic() - t0,
            n_media,
        )  # type: ignore[attr-defined]
    if n_media > max_media_per_prompt:
        return "\n\n".join(response_descriptions)
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Query classification — does the query ask for specific visual details?
# ---------------------------------------------------------------------------

# Keywords/patterns that indicate the user is asking about something specific
# about an image/video that a general caption might not cover.  When a
# pre-caption exists and the query does NOT match any of these, the
# Postprocessor can skip the VLM call and reuse the ingest-time caption.
_VLM_SPECIFIC_PATTERNS: list[tuple[str, str]] = [
    # Spatial / location
    (
        "spatial",
        r"\b(top|bottom|left|right|corner|behind|in front|above|below|center|centre|middle|background|foreground|next to|between)\b",
    ),
    # Counting / quantity
    ("count", r"\b(how many|count|number of|how much)\b"),
    # Color
    ("color", r"\b(what color|colour|color of|colour of)\b"),
    # Text in image
    ("text", r"\b(what (does|do).*say|read the|text on|sign says|sign say|label|writing|what.*written)\b"),
    # Presence / existence
    ("presence", r"\b(is there|are there|do you see|can you see|does it (contain|have|show))\b"),
    # Specific detail
    ("detail", r"\b(zoom|look (closely|carefully)|specifically|exactly|precisely)\b"),
    # Comparison
    ("comparison", r"\b(difference|compare|versus|vs\.?|unlike)\b"),
]


def _query_needs_vlm(query: str | dict[str, Any] | None) -> bool:
    """Return True if *query* asks for specific visual details about media.

    Used by the Postprocessor to decide whether to re-run the VLM at
    retrieval time or reuse an existing ingest-time caption.

    A query "needs VLM" when it references spatial locations, counts,
    colors, text in the image, presence of specific objects, or other
    specific details that a general caption may not cover.  Generic
    queries like "describe this image" or "show me photos of mountains"
    do NOT need VLM — the pre-caption is sufficient.
    """
    if not query:
        return False
    q_text = query if isinstance(query, str) else (query.get("text", "") if isinstance(query, dict) else "")
    if not q_text:
        return False
    import re

    q_lower = q_text.lower()
    for _label, pattern in _VLM_SPECIFIC_PATTERNS:
        if re.search(pattern, q_lower):
            return True
    return False


def _media_payloads_needed(
    docs: list[Any],
    *,
    use_vlm: bool,
    vlm: Any,
    llm_modalities: set[str],
    query: str | dict[str, Any] | None,
) -> bool:
    """Conservatively predict whether tier-3 base64 media payloads are needed.

    Mirrors the Postprocessor's VLM-skip logic (caption reuse for generic
    queries) so retrieval can run a lightweight phase-1 search (tier-3 base64
    excluded) and only re-fetch payloads when a consumer will actually use
    them:

    * a vision-capable base LLM consumes ``image``/``video`` keys directly;
    * a VLM conversion will run — i.e. some retrieved doc has media and is
      NOT covered by a reusable ingest-time caption (caption present AND the
      query is generic).

    Media presence is detected from tier-2 ``preprocessed_*`` refs, which —
    unlike the tier-3 base64 keys — survive the lightweight payload selector.
    Audio never triggers a payload fetch: tier-3 audio is stored as a file
    ref, so the Postprocessor's ASR path reads it from disk regardless.
    """
    if bool(llm_modalities & {"image", "video"}):
        return any(
            isinstance(d, dict) and any(k in d for k in ("image", "video", "preprocessed_image", "preprocessed_video"))
            for d in docs
        )
    if not use_vlm or vlm is None:
        return False
    query_is_specific = _query_needs_vlm(query)
    for d in docs:
        if not isinstance(d, dict):
            continue
        text = d.get("text") or ""
        if ("image" in d or "preprocessed_image" in d) and not (
            "[Image description]:" in text and not query_is_specific
        ):
            return True
        if ("video" in d or "preprocessed_video" in d) and not (
            "[Video description]:" in text and not query_is_specific
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Preprocessor  —  runs BEFORE embedding / storage
# ---------------------------------------------------------------------------


@dataclass
class Preprocessor:
    """Convert unsupported modalities to text before embedding.

    Behaviour is driven by *target_modalities* (e.g. ``{"text", "image"}``).
    Any modality present in a document but absent from *target_modalities* is
    converted to text via the supplied auxiliary models:

    * ``audio``  → ASR transcription appended to ``text``
    * ``image``  → VLM description appended to ``text``
    * ``video``  → VLM description + optional ASR audio transcription

    Parameters
    ----------
    chunk_size:
        Maximum number of documents to process concurrently.  Larger ingests
        are split into chunks to avoid overwhelming the ASR / VLM endpoints.
    """

    vlm: ChatModel | None = None
    asr: VoiceModel | None = None
    caption_with_asr: bool = False
    caption_with_vlm: bool = False
    chunk_size: int = 128

    def __call__(
        self,
        documents: list[str | dict[str, Any]],
        target_modalities: set[str],
        query: str | dict[str, Any] | None = None,
    ) -> list[str | dict[str, Any]]:
        return sync_wrapper_safe(
            self.acall,
            {"documents": documents, "target_modalities": target_modalities, "query": query},
        )

    @cached_property
    def caption(self) -> bool:
        return self.caption_with_asr and self.asr is not None

    @cached_property
    def caption_img(self) -> bool:
        return self.caption_with_vlm and self.vlm is not None

    async def acall(
        self,
        documents: list[str | dict[str, Any]],
        target_modalities: set[str],
        query: str | dict[str, Any] | None = None,
    ) -> list[str | dict[str, Any]]:
        async def _process_one(doc) -> str | dict[str, Any] | None:
            if isinstance(doc, str):
                return doc

            d = dict(doc)
            media = _split_media(d)
            text_parts: list[str] = [d.get("text", "")] if d.get("text") else []

            asr_tasks: list = []
            vlm_tasks: list = []

            # -- audio not supported → ASR ----------------------------------
            if "audio" not in target_modalities and media["audio"]:
                if self.asr is None:
                    # No ASR to transcribe; drop the audio payload.  Omit the
                    # doc entirely if nothing meaningful remains.
                    d.pop("audio", None)
                    has_other_media = bool(media["image"]) or bool(media["video"])
                    text = (d.get("text") or "").strip()
                    if not has_other_media and (not text or text.startswith("[Audio:")):
                        logger.warning(
                            "Omitting audio document: no ASR model and embedder does not support audio. Source: %s",
                            d.get("source", "(unknown)"),
                        )
                        return None
                    logger.warning(
                        "Dropping audio from document: no ASR model and embedder "
                        "does not support audio. Keeping remaining content. Source: %s",
                        d.get("source", "(unknown)"),
                    )
                else:
                    asr_tasks.extend([self._transcribe(url) for url in media["audio"]])

            # -- video caption (VLM) / audio-track caption (ASR) ------------
            # video_needs_vlm is only true when the embedder cannot ingest
            # video; in that case the caption becomes the doc's text and the
            # video payload is dropped.  When the embedder DOES support video
            # natively, captioning is still done for the stored payload, but
            # the caption lines are stripped from the embedding input below
            # (see _strip_embed_caption) so the vector keys off the raw video.
            video_needs_vlm = "video" not in target_modalities and media["video"]
            video_captioned = False
            if video_needs_vlm:
                if self.vlm is not None:
                    vlm_tasks.append(self._describe(d, query=query))
                if self.caption:
                    asr_tasks.extend([self._transcribe(url) for url in media["video"]])
                d.pop("video", None)
            # -- video supported, but its audio track ignored by embedder ----
            elif "audio" not in target_modalities and self.caption:
                asr_tasks.extend([self._transcribe(url) for url in media["video"]])
            # -- video supported, but VLM caption requested ------------------
            # Caption is generated and stored, but is STRIPPED from the
            # embedding input below (see _strip_embed_caption) so the
            # vector keys off the raw video, not the caption wording.
            if not video_needs_vlm and media["video"] and self.caption_img and self.vlm is not None:
                vlm_tasks.append(self._describe(d, query=query))
                video_captioned = True

            # -- image caption (VLM) ------------------------------------------
            # Same rule as video: when the embedder cannot ingest images the
            # caption becomes the doc's text; when it can, captioning is kept
            # for the stored payload but stripped from the embedding input
            # below (see _strip_embed_caption).
            image_needs_vlm = "image" not in target_modalities and media["image"]
            image_captioned = False
            if image_needs_vlm:
                if self.vlm is not None:
                    vlm_tasks.append(self._describe(d, query=query))
                else:
                    # No VLM to turn the image into caption text, and the
                    # embedder can't ingest it either — the image can never
                    # be used.  Drop it (matches audio/video's "skip
                    # entirely" rule); the doc itself is omitted later if
                    # nothing embeddable remains.
                    d.pop("image", None)
            elif self.caption_img and media["image"]:
                # Caption generated for the stored payload; stripped from the
                # embedding input below (see _strip_embed_caption).
                vlm_tasks.append(self._describe(d, query=query))
                image_captioned = True

            asr_results = list(await asyncio.gather(*asr_tasks)) if asr_tasks else []
            vlm_results = list(await asyncio.gather(*vlm_tasks)) if vlm_tasks else []

            # -- audio ASR results ------------------------------------------
            # ASR captioning runs only when the embedder cannot ingest audio
            # ("audio" not in target_modalities) — same rule as image/video.
            # If a future embedder supports audio natively, the ASR
            # transcription is skipped automatically.
            if "audio" not in target_modalities and media["audio"] and self.asr is not None:
                n = len(media["audio"])
                transcripts = [t for t in asr_results[:n] if t]
                if len(transcripts) < n:
                    # ASR unavailable for this track — behave like the
                    # no-ASR-model case: drop the audio payload, omit the
                    # doc entirely if nothing meaningful remains.
                    d.pop("audio", None)
                    has_other_media = bool(media["image"]) or bool(media["video"])
                    text = (d.get("text") or "").strip()
                    if not has_other_media and (not text or text.startswith("[Audio:")):
                        wmsg = f"Omitting audio document: ASR unavailable ({d.get('source', '(unknown)')})"
                        logger.warning(wmsg)
                        _record_ingest_warning(wmsg)
                        return None
                    wmsg = f"Dropping audio from document: ASR unavailable ({d.get('source', '(unknown)')})"
                    logger.warning(wmsg)
                    _record_ingest_warning(wmsg)
                for transcript in transcripts:
                    text_parts.append(f"[Audio transcription]: {transcript}")
                asr_results = asr_results[n:]

            # -- VLM results (in order: video describe, image describe) -----
            vlm_idx = 0
            if (video_needs_vlm or video_captioned) and self.vlm is not None:
                if vlm_results[vlm_idx]:
                    text_parts.append(f"[Video description]: {vlm_results[vlm_idx]}")
                vlm_idx += 1
            if (image_needs_vlm or image_captioned) and self.vlm is not None:
                if vlm_results[vlm_idx]:
                    text_parts.append(f"[Image description]: {vlm_results[vlm_idx]}")
                vlm_idx += 1

            # -- remaining ASR results (video caption) ----------------------
            # Marker must match the Postprocessor's check ("[Video audio
            # transcription]:") so ingest-time captions are reused and the
            # audio is not re-transcribed at retrieval.
            for transcript in asr_results:
                if transcript:
                    text_parts.append(f"[Video audio transcription]: {transcript}")

            d["text"] = "\n".join(p for p in text_parts if p)

            # ── Unified "skip entirely" rule ───────────────────────────────
            # After conversion, drop the whole sample if nothing embeddable
            # survived: no media the embedder can ingest, and no caption text
            # (a converter-produced transcription/description counts; a bare
            # "[Image: x.jpg]" placeholder does not).  Audio docs are already
            # omitted by their branch above; this closes the same gap for
            # image/video and any mixed cases.
            if not _has_embeddable_content(d, target_modalities):
                wmsg = (
                    "Omitting document: media not supported by the embedder and "
                    f"no ASR/VLM to convert it to text ({d.get('source', '(unknown)')})"
                )
                logger.warning(wmsg)
                _record_ingest_warning(wmsg)
                return None
            return d

        out: list = []
        for i in range(0, len(documents), self.chunk_size):
            chunk = documents[i : i + self.chunk_size]
            results = await asyncio.gather(*[_process_one(d) for d in chunk])
            out.extend(r for r in results if r is not None)
        return out

    # -- internal helpers ---------------------------------------------------

    async def _transcribe(self, url: str) -> str | None:
        assert self.asr is not None
        try:
            return await _transcribe_media(url, self.asr)
        except Exception as exc:
            msg = f"ASR unavailable — caption skipped ({url}): {exc}"
            logger.warning(msg)
            _record_ingest_warning(msg)
            return None

    _DESCRIBE_SYSTEM_PROMPT = _DESCRIBE_SYSTEM_PROMPT_SIMPLE

    async def _describe(self, doc_dict: dict[str, Any], query: str | dict[str, Any] | None = None) -> str | None:
        assert self.vlm is not None
        try:
            return await _describe_doc(doc_dict, query, self.vlm, self._DESCRIBE_SYSTEM_PROMPT)
        except Exception as exc:
            msg = f"VLM unavailable — caption skipped ({doc_dict.get('source', '(unknown)')}): {exc}"
            logger.warning(msg)
            _record_ingest_warning(msg)
            return None


# ---------------------------------------------------------------------------
# Postprocessor  —  runs AFTER retrieval, converts docs for LLM consumption
# ---------------------------------------------------------------------------


@dataclass
class Postprocessor:
    """Convert retrieved documents for the LLM, preserving supported modalities.

    Behaves as a *modality gate*: any modality the target LLM supports
    natively (e.g. ``image`` / ``video``) is passed through in the output
    dict.  Unsupported modalities are converted to text via the supplied
    auxiliary models:

    * ``audio``  → ASR transcription
    * ``image``  → VLM description
    * ``video``  → VLM description + optional ASR audio transcription
    """

    vlm: ChatModel | None = None
    asr: VoiceModel | None = None
    caption_with_asr: bool = False

    def __call__(
        self,
        documents: list,
        llm_modalities: set[str],
        query: str | dict[str, Any] | None = None,
    ) -> list[str | dict[str, Any]]:
        return sync_wrapper_safe(
            self.acall,
            {"documents": documents, "llm_modalities": llm_modalities, "query": query},
        )

    async def acall(
        self,
        documents: list,
        llm_modalities: set[str],
        query: str | dict[str, Any] | None = None,
    ) -> list[str | dict[str, Any]]:
        async def _process_one(doc) -> str | dict[str, Any]:
            d = doc if isinstance(doc, dict) else {"text": str(doc)}
            text = d.get("text", "")
            media = _split_media(d)
            # Tier-2 preprocessed_* refs — used when surfacing URLs to the
            # base LLM / users so they link a user-viewable version.
            # VLM captioning and ASR keep using ``media`` (the tier-3 data
            # URL already in Qdrant) so descriptions stay precise and cheap
            # — no file read or re-resize needed.
            preferred = _preferred_media(d)

            result: dict[str, Any] = {}
            if text:
                result["text"] = text

            # -- pass through modalities the LLM supports natively ----------
            # Surface the tier-2 preprocessed_* refs (PVC files) rather than
            # the tier-3 embedding-grade data URLs stored in Qdrant.
            for modality in ("image", "video", "audio"):
                if modality in llm_modalities and preferred[modality]:
                    result[modality] = preferred[modality]

            asr_tasks: list = []
            vlm_tasks: list = []

            # -- audio: NEVER re-run ASR at retrieval ----------------------
            # Audio transcription is the ingest Preprocessor's job — it
            # runs once when the embedder doesn't support audio natively
            # (text is enriched with "[Audio transcription]: …").  Re-
            # running ASR at retrieval is wasteful (model call per result)
            # and unsafe for large files (e.g. a 38 MB MP3 hitting the ASR
            # endpoint's payload cap).  Whatever transcription was produced
            # at ingest is already in *text*; the link below lets the
            # LLM/user listen to the actual file.
            has_audio_transcript = "[Audio transcription]:" in text or "[Caption]:" in text

            # -- image / video not supported → VLM description --------------
            # When the LLM doesn't support image/video natively, we need to
            # convert them to text.  But if an ingest-time caption already
            # exists in the text (from caption_with_vlm / caption_with_asr), and
            # the user's query doesn't ask for specific visual details, we
            # can reuse that caption and skip the VLM call entirely.
            has_image_caption = "[Image description]:" in text
            has_video_caption = "[Video description]:" in text
            query_is_specific = _query_needs_vlm(query)

            image_needs_vlm = "image" not in llm_modalities and media["image"]
            video_needs_vlm = "video" not in llm_modalities and media["video"]

            # Skip VLM for images if a pre-caption exists and query is generic
            if image_needs_vlm and has_image_caption and not query_is_specific:
                image_needs_vlm = False
            # Skip VLM for videos if a pre-caption exists and query is generic
            if video_needs_vlm and has_video_caption and not query_is_specific:
                video_needs_vlm = False

            needs_vlm = image_needs_vlm or video_needs_vlm
            if needs_vlm and self.vlm is not None:
                vlm_tasks.append(self._describe(d, query=query))

            # -- optional video audio captioning ----------------------------
            # Only for video (audio is handled above via ingest-time text).
            # Skipped when the video's audio was already captioned at ingest
            # (marker "[Video audio transcription]:" present in text).
            has_video_audio_caption = "[Video audio transcription]:" in text
            if (
                self.caption_with_asr
                and "video" not in llm_modalities
                and media["video"]
                and self.asr is not None
                and not has_video_audio_caption
            ):
                asr_tasks.extend([self._transcribe(url) for url in media["video"]])

            asr_results = list(await asyncio.gather(*asr_tasks)) if asr_tasks else []
            vlm_results = list(await asyncio.gather(*vlm_tasks)) if vlm_tasks else []

            # -- ASR results (video caption only — audio handled above) ----
            if (
                self.caption_with_asr
                and "video" not in llm_modalities
                and media["video"]
                and self.asr is not None
                and not has_video_audio_caption
            ):
                for transcript in asr_results:
                    if not transcript:
                        continue
                    caption = f"[Video audio transcription]: {transcript}"
                    result["text"] = result["text"] + "\n" + caption if result.get("text") else caption

            # -- VLM description --------------------------------------------
            if needs_vlm and self.vlm is not None:
                desc = vlm_results[0]
                if desc:
                    desc_text = f"[Media description]: {desc}"
                    result["text"] = result["text"] + "\n" + desc_text if result.get("text") else desc_text

            # -- Audio link (always — no ASR at retrieval) -----------------
            # Always emit a clickable link for audio results so the LLM
            # and user can access the file.  If ingest produced a
            # transcription, it's already in *text* — no need to reproduce.
            if "audio" not in llm_modalities and preferred["audio"]:
                links = [_media_url_to_displayable(u) for u in preferred["audio"]]
                marker = "[Audio file (transcribed)]" if has_audio_transcript else "[Audio file]"
                link_text = f"{marker}: {' '.join(links)}"
                result["text"] = result["text"] + "\n" + link_text if result.get("text") else link_text

            # -- Fallback: include clickable links for unconverted media ----
            # When the LLM doesn't support a modality AND the conversion
            # model (ASR/VLM) is unavailable, include a link so the LLM
            # can share the reference with the user.  Link the tier-2
            # preprocessed_* ref when available.
            if "image" not in llm_modalities and preferred["image"] and self.vlm is None:
                links = [_media_url_to_displayable(u) for u in preferred["image"]]
                link_text = f"[Image file]: {' '.join(links)}"
                result["text"] = result["text"] + "\n" + link_text if result.get("text") else link_text
                logger.warning("No VLM model — including image link(s) instead of description.")
            if "video" not in llm_modalities and preferred["video"] and self.vlm is None:
                links = [_media_url_to_displayable(u) for u in preferred["video"]]
                link_text = f"[Video file]: {' '.join(links)}"
                result["text"] = result["text"] + "\n" + link_text if result.get("text") else link_text
                logger.warning("No VLM model — including video link(s) instead of description.")

            return result if result else ""

        return list(await asyncio.gather(*[_process_one(d) for d in documents]))

    # -- internal helpers ---------------------------------------------------

    async def _transcribe(self, url: str) -> str | None:
        assert self.asr is not None
        try:
            return await _transcribe_media(url, self.asr)
        except Exception as exc:
            msg = f"ASR unavailable — caption skipped ({url}): {exc}"
            logger.warning(msg)
            _record_ingest_warning(msg)
            return None

    _DESCRIBE_SYSTEM_PROMPT = _DESCRIBE_SYSTEM_PROMPT_WITH_SOURCE

    async def _describe(self, doc_dict: dict[str, Any], query: str | dict[str, Any] | None = None) -> str | None:
        assert self.vlm is not None
        try:
            return await _describe_doc(doc_dict, query, self.vlm, self._DESCRIBE_SYSTEM_PROMPT, log_timing=True)
        except Exception as exc:
            msg = f"VLM unavailable — caption skipped ({doc_dict.get('source', '(unknown)')}): {exc}"
            logger.warning(msg)
            _record_ingest_warning(msg)
            return None


# ---------------------------------------------------------------------------
# Pure RAG Core  (embed, store, retrieve, pre/post-process, no LLM)
# ---------------------------------------------------------------------------


@dataclass
class MultimodalRAG:
    """RAG core without an LLM — embedding, storage, retrieval, modality conversion.

    Use this directly when you only need retrieval (e.g. building a custom
    generation pipeline).  For a complete RAG + LLM experience see
    :class:`MultiModalRAGSystem`.
    """

    embedder: EmbeddingModel
    reranker: RerankerModel | None = None
    vlm: ChatModel | None = None
    asr: VoiceModel | None = None
    caption_with_asr: bool = False
    caption_with_vlm: bool = False
    preprocess: bool = True
    preprocess_chunk_size: int = 128
    dedup_threshold: float = 0.995

    # VectorStore option — pass a ``VectorStore`` instance, a config dict for
    # auto-creation, or ``None`` (in-memory retrieval via ``documents`` param).
    vector_store: VectorStore | dict[str, Any] | None = None

    remote: bool = False

    _preprocessor: Preprocessor = field(init=False, repr=False)
    _postprocessor: Postprocessor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.remote:
            for m in (self.embedder, self.reranker, self.vlm, self.asr):
                if m is not None:
                    m.remote()

        if self.caption_with_asr and self.asr is None:
            logger.warning(
                "caption_with_asr is enabled but no ASR model is configured; "
                "video audio-track captioning will be skipped (auto-disabled). "
                "Set MODEL_ASR_URL or pass an `asr` model to enable it."
            )

        self._preprocessor = Preprocessor(
            vlm=self.vlm,
            asr=self.asr,
            caption_with_asr=self.caption_with_asr,
            caption_with_vlm=self.caption_with_vlm,
            chunk_size=self.preprocess_chunk_size,
        )
        # caption_with_asr is passed through so retrieval can still transcribe
        # video audio tracks for docs ingested WITHOUT captioning (e.g. created
        # before the flag was enabled).  The Postprocessor skips this when an
        # ingest-time "[Video audio transcription]:" caption already exists.
        self._postprocessor = Postprocessor(
            vlm=self.vlm,
            asr=self.asr,
            caption_with_asr=self.caption_with_asr,
        )

        vs = self.vector_store
        if isinstance(vs, dict):
            qdrant_path: str = vs.pop("qdrant_path", "./qdrant_storage")
            collection_name: str = vs.pop("collection_name", "documents")
            qdrant_host: str = vs.pop("qdrant_host", "")
            qdrant_port: int = int(vs.pop("qdrant_port", 6333))
            self.vector_store = self._build_qdrant_vector_store(
                embedding=self.embed,
                qdrant_path=qdrant_path,
                collection_name=collection_name,
                qdrant_host=qdrant_host,
                qdrant_port=qdrant_port,
                **vs,
            )
        elif isinstance(vs, VectorStore):
            logger.warning(
                "Embedding model must match the one used to build the vector store:\n"
                "  Embedder model : %s\n"
                "  Embedder URL   : %s\n"
                "Nothing is checked explicitly — ensure they match.",
                self.embedder.model_name,
                self.embedder.base_url,
            )
        else:
            self.vector_store = InMemoryVectorStore(embedding=self.embed)
            logger.info("No vector_store provided — using in-memory store")

    @staticmethod
    def _build_qdrant_vector_store(
        embedding: Any,
        qdrant_path: str = "./qdrant_storage",
        collection_name: str = "documents",
        qdrant_host: str = "",
        qdrant_port: int = 6333,
        client: Any = None,
        bm25_stats_path: str | None = None,
        **kwargs,
    ) -> VectorStore:
        """Create (or adopt) the collection and return its store wrapper.

        New collections get the hybrid schema (roadmap feature 2): a named
        ``dense`` vector plus a named ``bm25`` **sparse** vector, so text
        queries can RRF-fuse the lexical lane with the dense one.  The
        schema is created regardless of ``RAG_HYBRID_SEARCH`` — that knob
        forces dense-only *behaviour* (no sparse vectors computed at ingest,
        no fusion request at query time), not a different collection shape,
        so a dataset keeps one stable schema and can adopt the BM25 lane
        later without a recreate.

        Existing collections are adopted as they are: legacy collections
        with an unnamed default vector keep ``vector_name=None`` (hybrid
        simply doesn't apply to them), while collections already carrying
        the named-vector schema are re-detected so a restart after creation
        keeps working.  ``client`` (pre-built QdrantClient) and
        ``bm25_stats_path`` are injectable for callers that own the client
        or the dataset directory (DatasetManager passes the latter).
        """
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, SparseVectorParams, VectorParams

        if client is None:
            if qdrant_host:
                client = QdrantClient(
                    host=qdrant_host,
                    port=qdrant_port,
                    prefer_grpc=_qdrant_prefer_grpc(),
                    timeout=_qdrant_client_timeout(),
                )
            else:
                client = QdrantClient(path=qdrant_path)

        test_vec = embedding.embed_query("")
        vector_size = len(test_vec)

        # An explicitly requested vector name wins over schema detection.
        explicit_vector_name = kwargs.pop("vector_name", None)

        collections = retry_call(
            lambda: client.get_collections().collections,
            max_attempts=3,
            base_delay=2.0,
            connection_delay=5.0,
        )
        vector_name: str | None = None
        if not any(c.name == collection_name for c in collections):
            client.create_collection(
                collection_name=collection_name,
                vectors_config={bm25_lane.DENSE_VECTOR_NAME: VectorParams(size=vector_size, distance=Distance.COSINE)},
                sparse_vectors_config={bm25_lane.BM25_VECTOR_NAME: SparseVectorParams()},
                quantization_config=_qdrant_quantization_config(),
            )
            vector_name = bm25_lane.DENSE_VECTOR_NAME
            # Payload indexes for metadata-filtered search (best-effort —
            # filtering works without them, just slower).  Existing
            # collections get theirs via the backfill-search-metadata
            # admin endpoint.
            ensure_search_payload_indexes(client, collection_name)
        else:
            vector_name = _detect_dense_vector_name(client, collection_name)

        return QdrantVectorStore(
            client=client,
            collection_name=collection_name,
            embedding=embedding,
            vector_name=explicit_vector_name or vector_name,
            bm25_stats_path=bm25_stats_path,
            **kwargs,
        )

    def __repr__(self) -> str:
        def _mn(m: Any) -> str:
            return m.model_name if hasattr(m, "model_name") else str(m)

        def _vs(vs: Any) -> str:
            if vs is None:
                return "(none)"
            name = type(vs).__qualname__
            if name == "InMemoryVectorStore":
                return f"{name}({self.embedder.model_name})"
            try:
                return f"{name}(collection={vs.collection_name})"  # type: ignore[attr-defined]
            except Exception:
                return name

        fields = [
            ("embedder", _mn(self.embedder)),
            ("reranker", _mn(self.reranker) if self.reranker else "(none)"),
            ("vlm", _mn(self.vlm) if self.vlm else "(none)"),
            ("asr", _mn(self.asr) if self.asr else "(none)"),
            ("caption_with_asr", str(self.caption_with_asr)),
            ("preprocess", str(self.preprocess)),
            ("remote", str(self.remote)),
            ("vector_store", _vs(self.vector_store)),
        ]
        body = "\n".join(f"  {k}={v}" for k, v in fields)
        return f"MultimodalRAG(\n{body}\n)"

    # -- shortcuts -------------------------------------------------------------

    @property
    def _embed_modalities(self) -> set[str]:
        return set(self.embedder.allowable_modalities)

    @cached_property
    def embed(self):
        model = self.embedder.model
        type_name = type(model).__qualname__
        if type_name in ("MultiModalEmbeddings", "MultiModalReranker"):
            return model
        raise TypeError(
            f"Embedder model has unexpected type {type(model).__module__}.{type(model).__qualname__}.\n"
            f"  Embedder model name:            {self.embedder.model_name}\n"
            f"  model_instantiation_class:      {self.embedder.model_instantiation_class}\n"
            f"  model_usage:                    {self.embedder.model_usage}\n\n"
            f"Expected a {MultiModalEmbeddings.__module__}.{MultiModalEmbeddings.__qualname__} "
            f"instance."
        )

    @cached_property
    def rank(self) -> MultiModalReranker:
        if self.reranker is None:
            raise AttributeError("No reranker configured.")
        return self.reranker.model  # type: ignore[return-value]

    # -- 1:1 passthrough delegates -----------------------------------------------

    @property
    def embed_documents(self):
        return self.embed.embed_documents

    @property
    def aembed_documents(self):
        return self.embed.aembed_documents

    @property
    def embed_query(self):
        return self.embed.embed_query

    @property
    def aembed_query(self):
        return self.embed.aembed_query

    @property
    def arerank(self):
        return self.rank.arerank

    @property
    def ascore(self):
        return self.rank.ascore

    @property
    def rerank(self):
        return self.rank.rerank

    @property
    def score(self):
        return self.rank.score

    # -- vector store management ------------------------------------------------

    @staticmethod
    def _normalize_doc(doc: Any) -> Any:
        """Normalize common document schemas into the flat ``{text: ...}`` form.

        API clients sometimes post the legacy nested shape::

            {"id": "t1", "content": {"text": "...", "image": "..."}, "metadata": {...}}

        The ``content`` mapping (or string) is lifted onto the top level and
        any ``metadata`` dict is merged in, so downstream preprocessing and
        embedding see the real ``text`` / ``image`` / ``video`` / ``audio``
        keys instead of an empty document.  Docs already in the flat form are
        returned unchanged.
        """
        if not isinstance(doc, dict):
            return doc
        content = doc.get("content")
        if not isinstance(content, (dict, str)):
            return doc
        d = dict(doc)
        d.pop("content", None)
        if isinstance(content, dict):
            for k, v in content.items():
                d.setdefault(k, v)
        else:
            d.setdefault("text", content)
        meta = d.get("metadata")
        if isinstance(meta, dict):
            d.pop("metadata", None)
            for k, v in meta.items():
                d.setdefault(k, v)
        return d

    @staticmethod
    def _to_documents(inputs: Sequence[str | dict[str, Any]]) -> list[Document]:
        docs = []
        for inp in inputs:
            if isinstance(inp, str):
                docs.append(Document(page_content=inp, metadata={}))
            elif isinstance(inp, dict):
                text = inp.get("text", "")
                meta = {k: v for k, v in inp.items() if k != "text"}
                # Metadata-filtered search keys off metadata.file_type —
                # classify once at ingest from the document source.  Docs
                # without any source (raw text drops) stay unlabelled.
                if "file_type" not in meta:
                    src = meta.get("source") or meta.get("original_source")
                    if src:
                        # Lazy import: dataset_manager imports this module.
                        from multimodal_rag.dataset_manager import _classify_file

                        meta["file_type"] = _classify_file(str(src))
                docs.append(Document(page_content=text, metadata=meta))
            else:
                docs.append(Document(page_content=str(inp), metadata={}))
        return docs

    @staticmethod
    def _from_documents(docs: list[Document]) -> list[str | dict[str, Any]]:
        result: list[str | dict[str, Any]] = []
        for doc in docs:
            if doc.metadata:
                entry = dict(doc.metadata)
                entry["text"] = doc.page_content
                result.append(entry)
            else:
                result.append(doc.page_content)
        return result

    @staticmethod
    def _extract_doc(doc: Any) -> str | dict[str, Any]:
        if isinstance(doc, Document):
            if doc.metadata:
                entry = dict(doc.metadata)
                entry["text"] = doc.page_content
                return entry
            return doc.page_content
        return doc

    async def _preprocess_docs(
        self,
        documents: Sequence[str | dict[str, Any]],
    ) -> list[str | dict[str, Any]]:
        """Run the preprocessor unless the embedder already supports all modalities."""
        documents = [self._normalize_doc(d) for d in documents]
        if not self.preprocess:
            docs = list(documents)
            # Even without preprocessing, apply the unified "skip entirely"
            # rule: drop media the embedder can't ingest (with no VLM/ASR
            # conversion possible — preprocessing is disabled), and omit the
            # doc entirely when nothing embeddable remains.
            kept: list[str | dict[str, Any]] = []
            omitted = 0
            for d in docs:
                if isinstance(d, dict):
                    unsupported = [
                        m for m in ("audio", "image", "video") if m not in self._embed_modalities and d.get(m)
                    ]
                    if unsupported:
                        d = dict(d)
                        for m in unsupported:
                            d.pop(m, None)
                        if not _has_embeddable_content(d, self._embed_modalities):
                            omitted += 1
                            continue
                kept.append(d)
            if omitted:
                logger.warning(
                    "Omitting %d document(s): media unsupported by the embedder and no ASR/VLM to convert it.",
                    omitted,
                )
            return kept
        return await self._preprocessor.acall(
            list(documents),
            target_modalities=self._embed_modalities,
        )

    def list_documents(self, limit: int = 50) -> list[tuple[str, dict[str, Any]]]:
        """Return stored documents for debugging.

        Returns ``[(id, {"text": ..., "image": ..., ...}), ...]`` where the
        dict is the reconstructed multimodal entry.
        """
        return sync_wrapper_safe(self.alist_documents, {"limit": limit})

    async def alist_documents(self, limit: int = 50) -> list[tuple[str, dict[str, Any]]]:
        vs = self.vector_store
        assert vs is not None and not isinstance(vs, dict)
        if hasattr(vs, "store"):
            # InMemoryVectorStore
            out = []
            for doc_id, entry in list(vs.store.items())[:limit]:
                doc: Document = entry["document"]
                meta = dict(doc.metadata)
                meta["text"] = doc.page_content
                out.append((doc_id, meta))
            return out
        # QdrantVectorStore – use the underlying client
        try:
            client = vs._client  # type: ignore[attr-defined]
            coll = vs.collection_name  # type: ignore[attr-defined]
            # Offloaded: the sync scroll is a network round-trip that would
            # otherwise block the caller's event loop.
            records, _ = await asyncio.get_running_loop().run_in_executor(
                _QDRANT_IO_POOL,
                lambda: client.scroll(coll, limit=limit, with_payload=True, with_vectors=False),
            )
            out = []
            for rec in records:
                payload = rec.payload or {}
                meta = dict(payload.get("metadata", {}))
                meta["text"] = payload.get("page_content", "")
                out.append((str(rec.id), meta))
            return out
        except Exception as e:
            return [(f"error: {e}", {})]

    def add_to_vector_store(
        self,
        documents: Sequence[str | dict[str, Any]],
        deduplicate: bool = True,
        dedup_threshold: float | None = None,
        **kwargs,
    ) -> list[str]:
        """Sync wrapper around :meth:`aadd_to_vector_store`."""
        return sync_wrapper_safe(
            self.aadd_to_vector_store,
            dict(
                documents=documents,
                deduplicate=deduplicate,
                dedup_threshold=dedup_threshold,
                **kwargs,
            ),
        )

    def _resize_media_in_docs(self, docs: list[str | dict[str, Any]]) -> list[str | dict[str, Any]]:
        """Downscale embedded images and transcode videos in document dicts before storage.

        * Images are resized so width × height ≤ *max_pixels*.
        * Videos are transcoded to *fps* (default 1) and scaled so each
          frame fits within *max_pixels*.
        * HTTP(S) URLs are left untouched (the server fetches them).
        """
        mpk = self.embedder.mm_processor_kwargs
        max_px = mpk.get("max_pixels", 0)
        fps = mpk.get("fps", 1.0)
        if max_px <= 0:
            return docs

        out: list[str | dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict):
                out.append(doc)
                continue
            d = dict(doc)
            for key in ("image", "video"):
                val = d.get(key)
                if not val:
                    continue
                urls = val if isinstance(val, list) else [val]
                resized: list[str] = []
                for url in urls:
                    if url.startswith(("http://", "https://")):
                        resized.append(url)
                        continue
                    if url.startswith("data:") and key != "video":
                        # Data URL image — resize in-memory
                        import re

                        m = re.match(r"data:([^;]+);base64,(.+)", url)
                        if m:
                            mime = m.group(1)
                            raw = base64.b64decode(m.group(2))
                        else:
                            raw = base64.b64decode(url.split(",", 1)[1])
                            mime = "image/png"
                        resized_raw = self.embed.input_conversion._resize_image(raw, mime, max_px)
                        b64 = base64.b64encode(resized_raw).decode("utf-8")
                        resized.append(f"data:{mime};base64,{b64}")
                        continue
                    # Local file path — keep as-is (already resized by _save_doc_media)
                    if url.startswith("file://"):
                        resized.append(url)
                        continue
                    # video data URL — pass through
                    if url.startswith("data:"):
                        # video data URL — pass through
                        resized.append(url)
                        continue
                    path = url  # bare path, no scheme
                    if key == "image":
                        with open(path, "rb") as f:
                            raw = f.read()
                        mime = _infer_mime(url)
                        resized_raw = self.embed.input_conversion._resize_image(raw, mime, max_px)
                        b64 = base64.b64encode(resized_raw).decode("utf-8")
                        resized.append(f"data:{mime};base64,{b64}")
                    else:
                        # Video — transcode with ffmpeg: fps + dynamic aspect-ratio-aware resize
                        import json
                        import subprocess as sp

                        vw = vh = 0
                        try:
                            probe = sp.run(
                                [
                                    "ffprobe",
                                    "-v",
                                    "error",
                                    "-select_streams",
                                    "v:0",
                                    "-show_entries",
                                    "stream=width,height",
                                    "-of",
                                    "json",
                                    path,
                                ],
                                capture_output=True,
                                text=True,
                                timeout=15,
                            )
                            pinfo = json.loads(probe.stdout)
                            for s in pinfo.get("streams", []):
                                vw = int(s.get("width", 0))
                                vh = int(s.get("height", 0))
                                break
                        except Exception:
                            logger.debug("Suppressed exception", exc_info=True)
                        if vw > 0 and vh > 0 and vw * vh > max_px:
                            scale = (max_px / (vw * vh)) ** 0.5
                            new_w = max(2, (int(vw * scale) // 2) * 2)
                            new_h = max(2, (int(vh * scale) // 2) * 2)
                            scale = f"scale={new_w}:{new_h}"
                        else:
                            scale = "scale='min(720,iw)':'min(720,ih)':force_original_aspect_ratio=decrease"
                        proc = sp.run(
                            [
                                "ffmpeg",
                                "-v",
                                "error",
                                "-i",
                                path,
                                # Video-only: muxing audio yields a negative first
                                # packet (AAC priming) that some ffmpeg builds reject
                                # in the mp4 muxer.  Only frames are needed here.
                                "-an",
                                "-avoid_negative_ts",
                                "make_zero",
                                "-vf",
                                f"fps={fps},{scale}",
                                "-f",
                                "mp4",
                                "-movflags",
                                "frag_keyframe+empty_moov",
                                "-vcodec",
                                "libx264",
                                "-preset",
                                "fast",
                                "-crf",
                                "28",
                                "-",
                            ],
                            capture_output=True,
                            timeout=300,
                        )
                        mime = "video/mp4"
                        b64 = base64.b64encode(proc.stdout).decode("utf-8")
                        resized.append(f"data:{mime};base64,{b64}")
                d[key] = resized if isinstance(val, list) else resized[0]
            out.append(d)
        return out

    def _split_audio_chunks(self, docs: list[str | dict[str, Any]]) -> list[str | dict[str, Any]]:
        """Split long audio-transcription documents into multiple chunks.

        When a document has an ``audio`` key and its text exceeds the
        embedder's chunk budget, the text is split into smaller pieces so
        that each chunk fits within the embedding window.  Every chunk
        retains the audio reference.
        """
        chunk_size = self.embedder.chunk_size
        chunk_overlap = self.embedder.chunk_overlap
        text_splitter = self.embedder.text_splitter

        def _split(text: str) -> list[str]:
            if text_splitter is not None:
                return text_splitter.split_text(text)
            # Character-based fallback (mirrors TokenTextSplitter.split_text:
            # 10% net-new tail merge + chunk_size//4 minimum-tail backfill).
            min_new = max(chunk_size // 10, 1)
            min_tail = max(chunk_size // 4, 1)
            result: list[str] = []
            start = 0
            prev_end = 0
            while start < len(text):
                end = min(start + chunk_size, len(text))
                if end < len(text):
                    next_space = text.find(" ", end)
                    if next_space != -1 and next_space - end < chunk_size // 2:
                        end = next_space
                if result and end >= len(text):
                    new_content = len(text) - prev_end
                    if new_content < min_new:
                        tail = text[prev_end:end].strip()
                        if tail:
                            result[-1] = result[-1] + " " + tail
                        break
                    if end - start < min_tail:
                        start = max(0, end - min_tail)
                result.append(text[start:end].strip())
                if end >= len(text):
                    break
                prev_end = end
                start = end - chunk_overlap
                start = max(start, 0)
            return [c for c in result if c]

        out: list[str | dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict) or not doc.get("audio"):
                out.append(doc)
                continue
            text = doc.get("text", "")
            if not text:
                out.append(doc)
                continue

            # Split once (token-counting + splitting in a single pass).  A
            # single output chunk identical to the input means the doc was
            # within budget — keep it untouched.
            chunks = _split(text)
            if not chunks or (len(chunks) == 1 and chunks[0].strip() == text.strip()):
                out.append(doc)
                continue

            for i, chunk in enumerate(chunks):
                entry = dict(doc)
                entry["text"] = chunk
                entry["chunk_index"] = i
                out.append(entry)

        return out

    def _split_image_chunks(self, docs: list[str | dict[str, Any]], max_images: int = 4) -> list[str | dict[str, Any]]:
        """Split documents with too many images into multiple sub-docs.

        The Qwen3-VL-Embedding endpoint limits each prompt to 4 images.
        When a document (e.g. a PDF page with many images) exceeds this,
        it is split into N sub-docs — each with the same text but a
        different subset of <=4 images.  Each sub-doc gets its own Qdrant
        point so all images are searchable.

        Splits are balanced via ``list_chunker(optimize=True)`` so that
        images are distributed as evenly as possible (e.g. 5 -> [3, 2],
        9 -> [3, 3, 3]) rather than greedily filling the first chunks and
        leaving a small remainder.
        """
        if max_images <= 0:
            return list(docs)
        out: list[str | dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict):
                out.append(doc)
                continue
            images = doc.get("image")
            if not images:
                out.append(doc)
                continue
            img_list = images if isinstance(images, list) else [images]
            if len(img_list) <= max_images:
                out.append(doc)
                continue
            # Balance images across chunks (e.g. 5 -> [3, 2], 9 -> [3, 3, 3])
            for i, group in enumerate(list_chunker(img_list, max_images, optimize=True)):
                entry = dict(doc)
                entry["image"] = list(group)
                entry["chunk_index"] = i
                out.append(entry)
        return out

    def _batch_find_duplicates(
        self,
        vs: VectorStore,
        embs: list[list[float]],
        threshold: float,
    ) -> list[bool]:
        """Return a bool for each embedding: True if a duplicate exists above *threshold*.

        For Qdrant this uses a single batched ``query_batch_points`` call
        (1 HTTP request) instead of N individual queries.  For the
        InMemoryVectorStore path it uses vectorised numpy cosine similarity.
        """
        if not embs:
            return []

        if hasattr(vs, "store"):
            # ── InMemoryVectorStore — vectorised numpy ──────────────────
            stored_vecs = [
                s.get("vector")
                for s in vs.store.values()
                if s.get("vector") is not None  # type: ignore[attr-defined]
            ]
            if not stored_vecs:
                return [False] * len(embs)
            query_matrix = np.array(embs, dtype=np.float32)  # N x D
            stored_matrix = np.array(stored_vecs, dtype=np.float32)  # M x D
            sims = cosine_sim(query_matrix, stored_matrix)  # N x M
            return [bool(np.any(row >= threshold)) for row in sims]
        else:
            # ── QdrantVectorStore — single batched request ───────────────
            from qdrant_client.models import QueryRequest

            client = vs._client  # type: ignore[attr-defined]
            coll = vs.collection_name  # type: ignore[attr-defined]
            vector_name = vs.vector_name  # type: ignore[attr-defined]

            responses = client.query_batch_points(
                collection_name=coll,
                requests=[
                    QueryRequest(
                        query=emb,
                        using=vector_name,
                        limit=1,
                        with_payload=False,
                        with_vector=False,
                        score_threshold=threshold,
                    )
                    for emb in embs
                ],
            )
            return [bool(resp.points) for resp in responses]

    async def aadd_to_vector_store(
        self,
        documents: Sequence[str | dict[str, Any]],
        deduplicate: bool = True,
        dedup_threshold: float | None = None,
        **kwargs,
    ) -> list[str]:
        """Embed and add documents to the vector store.

        Multimodal content (``image``, ``video``, ``audio`` keys) is
        preserved in the dict and **embedded together with the text** so
        that Qwen3-VL-Embedding can use the full visual information for
        similarity search — not just the text caption.

        Images are resized client-side **before** storage so that the
        vector store never holds the original full-resolution payload.

        Parameters
        ----------
        deduplicate:
            When enabled, each document's embedding is checked against
            existing vectors in the store.  Documents whose cosine
            similarity to the nearest neighbour exceeds
            *dedup_threshold* are skipped and logged.
        dedup_threshold:
            Cosine similarity threshold above which a document is
            considered a duplicate (0.0 – 1.0).  When ``None`` (the
            default), falls back to ``self.dedup_threshold``.
        """
        if dedup_threshold is None:
            dedup_threshold = self.dedup_threshold
        t0 = time.monotonic()

        # ── 0. Preprocess ───────────────────────────────────────────────────
        processed = await self._preprocess_docs(documents)
        if not processed:
            logger.verbose("  %.2fs add_vs  — no docs to add", time.monotonic() - t0)  # type: ignore[attr-defined]
            return []
        logger.verbose(  # type: ignore[attr-defined]
            "  %.2fs add_vs  — preprocess (%d docs)",
            time.monotonic() - t0,
            len(processed),
        )  # type: ignore[attr-defined]

        # ── 0d helper (used inside sub-batch loop) ────────────────────────
        def _replace_audio(d: dict[str, Any]) -> dict[str, Any]:
            if "audio" not in d:
                return d
            src = d.get("source", "")
            ref = f"file://{src}" if src and not src.startswith(("file://", "http://", "https://", "s3://")) else src
            d = dict(d)
            d["audio"] = ref if ref else d["audio"]
            return d

        # ── 1-3. Sub-batched resize → embed → dedup → upsert ─────────────
        # Media resize, audio splitting, and audio payload replacement are
        # done per sub-batch so large doc dicts (with image/video data URLs)
        # are released after each upsert rather than accumulating for the
        # full set.  This bounds peak memory for large ingests.
        vs = self.vector_store
        assert vs is not None and not isinstance(vs, dict)

        # ── 0f. Hybrid BM25 state (roadmap feature 2) ─────────────────────
        # None unless the collection carries the named ``bm25`` sparse
        # vector, RAG_HYBRID_SEARCH is on, and a stats sidecar is wired.
        # The capability probe (get_collection round trip) runs on the
        # Qdrant I/O pool — never on the event loop.
        bm25_ctx = await asyncio.get_running_loop().run_in_executor(_QDRANT_IO_POOL, _bm25_ingest_context, vs)

        embed_batch_size = getattr(self.embed, "chunk_size", 64) or 64
        all_ids: list[str] = []
        t_embed_total = 0.0
        t_store_total = 0.0
        total_skipped = 0

        for sub in list_chunker(processed, embed_batch_size):
            # ── 0b. Resize media before storage ─────────────────────────────
            # Run in the dedicated media pool — ffmpeg/probe subprocess calls
            # would otherwise block the event loop for up to 300s per video
            # (and would previously compete with Qdrant search flushes on the
            # default executor).
            loop = asyncio.get_running_loop()
            sub = await loop.run_in_executor(_MEDIA_POOL, self._resize_media_in_docs, sub)

            # ── 0c. Split long audio transcriptions into chunks ─────────────
            sub = self._split_audio_chunks(sub)

            # ── 0c2. Split docs with too many images (vLLM limit: 4/prompt) ─
            max_images = int(os.environ.get("EMBEDDING_MAX_IMAGES_PER_PROMPT", "4"))
            sub = self._split_image_chunks(sub, max_images=max_images)

            # ── 0d. Replace audio payloads with file references ─────────────
            sub = [_replace_audio(d) if isinstance(d, dict) else d for d in sub]

            # ── 0e. Create twins for multimodal docs ─────────────────────────
            # A multimodal embedding (text + images) can be dominated by the
            # image content, burying the text signal so text queries don't
            # match.  Two twin kinds exist, both tagged ``_twin=True`` so the
            # retrieval dedup (:meth:`_dedup_twins`) can collapse them with
            # their multimodal parent:
            #
            # 1. **Text-only twins** — for docs that carry *real* extracted
            #    text (e.g. a PDF page).  The twin embeds the same text
            #    WITHOUT media, so text queries match even when the
            #    multimodal embedding is dominated by the visual content.
            #    Its stored payload keeps the image/video so the media is
            #    still retrievable when only the twin matches a query.
            #
            # 2. **Caption twins** — for PURE media docs (image/video/audio
            #    whose text is only a "[Image: x.JPG]" placeholder and an
            #    optional ingest-time VLM/ASR caption).  The base embedding
            #    keys off the raw media (captions stripped, see
            #    _strip_embed_caption); the caption twin embeds the SAME
            #    media WITH the caption text so text queries that match the
            #    caption wording can still retrieve the point.  Created only
            #    when the embedder supports the doc's media AND a caption is
            #    present (_media_caption_twin_needed).
            #
            # Docs whose text is only a media placeholder without a caption
            # get no twin (there is nothing meaningful for the twin to add),
            # and docs whose media the embedder can't ingest were already
            # collapsed to caption text by the Preprocessor — that caption
            # text embedding is the caption-only path.
            twin_embed_docs: list[dict[str, Any]] = []
            twin_store_docs: list[dict[str, Any]] = []
            twin_caption_count = 0
            for idx, doc in enumerate(sub):
                if not isinstance(doc, dict):
                    continue
                text = (doc.get("text") or "").strip()
                if not text:
                    continue
                has_media = any(doc.get(k) for k in ("image", "video", "audio"))
                if not has_media:
                    continue
                # Embedding input: text-only (media stripped).
                embed_twin = {k: v for k, v in doc.items() if k not in ("image", "video", "audio")}
                embed_twin["_twin"] = True
                # Stored payload: media retained so the twin stays retrievable
                # with its image/video reference.
                store_twin = dict(doc)
                store_twin["_twin"] = True
                if _has_real_text(text):
                    # Real extracted content (e.g. a PDF page) → text-only twin.
                    twin_embed_docs.append(embed_twin)
                elif _media_caption_twin_needed(doc, self._embed_modalities):
                    # Pure media + caption → caption twin embeds the media WITH
                    # the caption text (embed_twin keeps the media keys), so
                    # the twin's vector is (media + caption) rather than text-
                    # only.  The base parent embedding is the media-only one.
                    embed_twin = dict(doc)
                    embed_twin["_twin"] = True
                    twin_embed_docs.append(embed_twin)
                    twin_caption_count += 1
                else:
                    continue
                twin_store_docs.append(store_twin)

            # ── 1. Embed this sub-batch (multimodal + twins) ────────────────
            # Ingest-time VLM/ASR captions are stored in the payload so the
            # retrieval Postprocessor can reuse them for the LLM.  Only PURE
            # media docs (no real extracted text) get their caption lines
            # stripped from the embedding input — for those the raw image/
            # video is what should drive the vector, not the caption wording.
            # Docs that carry real text (e.g. a PDF page with an image) keep
            # the extracted text AND the caption in the embedding.  Docs
            # whose media modality is not supported by the embedder (e.g.
            # audio today) also keep their caption text as the embeddable
            # content — there is nothing else to embed.
            t1 = time.monotonic()
            embed_inputs = [_strip_embed_caption(d, self._embed_modalities) if isinstance(d, dict) else d for d in sub]
            sub_embs = await self.embed.aembed_documents(embed_inputs)
            if twin_embed_docs:
                # Text-only twins embed text only (media keys removed); caption
                # twins embed media + caption together (media keys kept).
                # Text twins are only created for docs that carry REAL text
                # (see _has_real_text gating above), and real-text docs keep
                # their VLM caption in the embedding — so no caption stripping
                # here either way.
                twin_embs = await self.embed.aembed_documents(twin_embed_docs)
                sub_embs.extend(twin_embs)
            t_embed_total += time.monotonic() - t1
            if twin_caption_count:
                logger.verbose(  # type: ignore[attr-defined]
                    "  %d caption twin(s) added (media + caption embeddings)",
                    twin_caption_count,
                )  # type: ignore[attr-defined]

            # Guard against silent data loss: if the embedding API returns
            # fewer/more vectors than documents, zip() would silently
            # truncate.  Log at error level and align to the shorter length.
            all_docs = list(sub) + list(twin_store_docs)
            if len(sub_embs) != len(all_docs):
                logger.error(
                    "Embedding count mismatch: %d docs → %d embeddings. "
                    "Truncating to %d — %d document(s) will be DROPPED.",
                    len(all_docs),
                    len(sub_embs),
                    min(len(all_docs), len(sub_embs)),
                    abs(len(all_docs) - len(sub_embs)),
                )
                min_len = min(len(all_docs), len(sub_embs))
                all_docs = all_docs[:min_len]
                sub_embs = sub_embs[:min_len]

            # ── 2. Build Document payloads ──────────────────────────────────
            sub_docs = self._to_documents(all_docs)

            # ── 2b. Deduplicate ─────────────────────────────────────────────
            if deduplicate and sub_embs:
                results = await loop.run_in_executor(
                    _QDRANT_IO_POOL,
                    self._batch_find_duplicates,
                    vs,
                    sub_embs,
                    dedup_threshold,
                )
                skipped = sum(results)
                total_skipped += skipped
                sub_docs = [d for d, dup in zip(sub_docs, results) if not dup]
                sub_embs = [e for e, dup in zip(sub_embs, results) if not dup]
                if skipped:
                    logger.info("Dedup: skipped %d of %d document(s)", skipped, len(all_docs))

            if not sub_docs:
                continue

            # ── 3. Store vectors + payloads ─────────────────────────────────
            t2 = time.monotonic()

            if hasattr(vs, "store"):
                # ── InMemoryVectorStore ──────────────────────────────────────
                batch_ids = await vs.aadd_documents(sub_docs, **kwargs)
                for doc_id, emb in zip(batch_ids, sub_embs):
                    vs.store[doc_id]["vector"] = emb
                all_ids.extend(batch_ids)
            else:
                # ── QdrantVectorStore ────────────────────────────────────────
                import uuid

                from qdrant_client.models import PointStruct

                client = vs._client  # type: ignore[attr-defined]
                coll = vs.collection_name  # type: ignore[attr-defined]
                vector_name = vs.vector_name  # type: ignore[attr-defined]

                # ── 3b. BM25 sparse vectors for the docs actually stored ────
                # Runs after dedup so dropped duplicates never pollute the
                # df stats, and only for docs with indexable text (see
                # _bm25_indexable_text — bare media placeholders out,
                # caption text in).  Deltas accumulate in bm25_ctx and are
                # persisted once after the last sub-batch.
                sparse_vectors = _bm25_sparse_vectors(sub_docs, bm25_ctx)

                points = []
                for i, (doc, emb) in enumerate(zip(sub_docs, sub_embs)):
                    doc_id = uuid.uuid4().hex
                    all_ids.append(doc_id)
                    vector: Any = {vector_name: emb} if vector_name else emb
                    if sparse_vectors[i] is not None:
                        # bm25-capable collections are always named-vector
                        # collections, so this never collides with the
                        # unnamed-vector shape above.
                        vector[bm25_lane.BM25_VECTOR_NAME] = sparse_vectors[i]
                    points.append(
                        PointStruct(
                            id=doc_id,
                            vector=vector,
                            payload={
                                "page_content": doc.page_content,
                                "metadata": doc.metadata,
                            },
                        )
                    )
                # Offloaded: a media-heavy sub-batch can carry tens to
                # hundreds of MB of tier-3 base64 payloads — the sync upsert
                # (network + JSON serialization) must not block the event
                # loop while it POSTs.
                await loop.run_in_executor(
                    _QDRANT_IO_POOL,
                    lambda: client.upsert(collection_name=coll, points=points, **kwargs),
                )

            t_store_total += time.monotonic() - t2
            # sub_embs / sub_docs fall out of scope here — released before the
            # next sub-batch starts.

        # ── 4. Persist the batch's BM25 df deltas (once, locked) ─────────
        _bm25_persist_stats(bm25_ctx)

        if total_skipped:
            logger.info("Dedup total: skipped %d document(s)", total_skipped)

        logger.verbose(  # type: ignore[attr-defined]
            "  %.2fs add_vs  — embed (%d vectors), store (%d docs)  [total %.2fs]",
            t_embed_total,
            len(all_ids) + total_skipped,
            len(all_ids),
            time.monotonic() - t0,
        )  # type: ignore[attr-defined]
        return all_ids

    # -- retrieval --------------------------------------------------------------

    def _similarity_retrieve(
        self,
        query_emb: list[float],
        doc_embs: list[list[float]],
        documents: list,
        k: int,
    ) -> list[tuple[Any, float]]:
        scores = cosine_sim(
            np.array(query_emb, dtype=np.float32).reshape(1, -1),
            np.array(doc_embs, dtype=np.float32),
        )[0]
        top_indices = np.argsort(scores)[-k:][::-1]
        return [(documents[i], float(scores[i])) for i in top_indices]

    @staticmethod
    def _dedup_twins(results: list[tuple[Any, float]]) -> list[tuple[Any, float]]:
        """Remove duplicate results so downstream compute and LLM context isn't wasted.

        Thin wrapper over :meth:`_dedup_by_identity` with the single-dataset
        twin-identity key (see :meth:`_twin_identity_key`).  Two dedup passes:

        1. **Twin vs parent**: twins (tagged ``_twin=True`` — both the
           text-only twins of real-text docs and the media+caption caption
           twins of pure media docs) that share the same
           ``(source, page, chunk_index, time-window)`` as a multimodal parent are
           dropped — the parent carries the media (and, for pure media docs,
           the identical stored payload).

        2. **Text-content dedup**: if two results have identical text
           (e.g. overlapping chunks from cross-page carry), keep only the
           higher-scoring one.  When one is a twin and the other isn't,
           prefer the non-twin (multimodal) version regardless of score.
        """
        return [
            (item.doc, item.score)
            for item in MultimodalRAG._dedup_by_identity(
                [_DedupItem(doc=doc, score=score) for doc, score in results],
                lambda item: MultimodalRAG._twin_identity_key(item.doc),
            )
        ]

    @staticmethod
    def _twin_identity_key(doc: Any) -> tuple | None:
        """Identity key for twin collapse: ``(source, page, chunk_index, time-window)``.

        ``None`` for docs that cannot collide (not a dict, or no source).
        """
        if isinstance(doc, dict):
            src = doc.get("source", "")
            page = doc.get("page")
            ci = doc.get("chunk_index")
            # Video segments share (source, page=None, chunk_index=None);
            # disambiguate by the segment's time window so a twin of one
            # segment is not collapsed against a parent of another.
            ts = (doc.get("timestamp_start"), doc.get("timestamp_end"))
            if src:
                return (src, page, ci, ts)
        return None

    @staticmethod
    def _dedup_by_identity(
        items: list["_DedupItem"],
        key_fn: "Callable[[_DedupItem], tuple | None]",
    ) -> list["_DedupItem"]:
        """Generic two-pass dedup over scored items, keyed by *key_fn*.

        Pass 1 drops ``_twin``-tagged docs whose identity key collides with a
        non-twin doc in the pool (the parent carries the media).  Pass 2
        collapses identical text content, keeping the best-scoring entry and
        preferring the non-twin (multimodal) version.  Original order is
        preserved (first occurrence wins ties).

        Both passes are scoped by ``item.dataset`` — ``None`` for the
        single-dataset twin collapse (so ``_dedup_twins`` behaves exactly as
        before) and the dataset name for the federated merge, where the same
        chunk stored in two datasets stays two labelled hits instead of
        collapsing into one.

        Shared verbatim by the single-dataset twin collapse
        (:meth:`_dedup_twins`) and the federated multi-dataset merge
        (:func:`dedup_federated_results`) so the two cannot drift.
        """
        if not items:
            return items

        # ── Pass 1: drop twins whose multimodal parent is also present ──
        parent_keys: set[tuple] = set()
        for item in items:
            if isinstance(item.doc, dict) and item.doc.get("_twin"):
                continue
            key = key_fn(item)
            if key is not None:
                parent_keys.add(key)

        pass1: list[_DedupItem] = []
        for item in items:
            if isinstance(item.doc, dict) and item.doc.get("_twin"):
                key = key_fn(item)
                if key is not None and key in parent_keys:
                    continue
            pass1.append(item)

        # ── Pass 2: dedup by text content, keep best score ─────────────
        # When a twin and non-twin share the same text, prefer the non-twin
        # (it has images).  Otherwise keep the higher-scoring entry.
        import hashlib as _hashlib

        best_by_text: dict[str, _DedupItem] = {}
        for item in pass1:
            doc = item.doc
            score = item.score
            if isinstance(doc, dict):
                text = (doc.get("text") or "").strip()
            elif isinstance(doc, str):
                text = doc.strip()
            else:
                text = str(doc).strip()
            if not text:
                continue
            text_hash = (item.dataset, _hashlib.md5(text.encode()).hexdigest())
            is_twin = isinstance(doc, dict) and doc.get("_twin", False)
            if text_hash not in best_by_text:
                best_by_text[text_hash] = item
            else:
                existing = best_by_text[text_hash]
                existing_is_twin = isinstance(existing.doc, dict) and existing.doc.get("_twin", False)
                # Replace if: current is non-twin and existing is twin,
                # or current scores higher and twin-status is equal
                if not is_twin and existing_is_twin or is_twin == existing_is_twin and score > existing.score:
                    best_by_text[text_hash] = item

        # Preserve original order (first occurrence wins ties)
        seen_hashes: set[str] = set()
        deduped: list[_DedupItem] = []
        for item in pass1:
            doc = item.doc
            if isinstance(doc, dict):
                text = (doc.get("text") or "").strip()
            elif isinstance(doc, str):
                text = doc.strip()
            else:
                text = str(doc).strip()
            if not text:
                deduped.append(item)
                continue
            text_hash = (item.dataset, _hashlib.md5(text.encode()).hexdigest())
            if text_hash in seen_hashes:
                continue
            seen_hashes.add(text_hash)
            # Only keep if this doc is the best for its text hash
            deduped.append(best_by_text[text_hash])
        return deduped

    async def _arerank_results(
        self,
        query: str | dict[str, Any],
        results: list[tuple[Any, float]],
        reranker_top_k: int = 3,
    ) -> list[tuple[Any, float]]:
        """Rerank this dataset's results with the configured cross-encoder.

        Thin delegate to :func:`_arerank_with` (the shared implementation also
        used for the federated merged-pool rerank).
        """
        return await _arerank_with(self.rank, self._extract_doc, query, results, reranker_top_k)

    def retrieve(
        self,
        query: str | dict[str, Any],
        documents: list[str | dict[str, Any]] | None = None,
        top_k: int = 10,
        use_reranker: bool = True,
        reranker_top_k: int = 3,
        query_vector: list[float] | None = None,
        need_media: bool | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[Any, float]]:
        """Sync wrapper around :meth:`aretrieve`."""
        return sync_wrapper_safe(
            self.aretrieve,
            {
                "query": query,
                "documents": documents,
                "top_k": top_k,
                "use_reranker": use_reranker,
                "reranker_top_k": reranker_top_k,
                "query_vector": query_vector,
                "need_media": need_media,
                "filters": filters,
            },
        )

    async def aretrieve(
        self,
        query: str | dict[str, Any],
        documents: list[str | dict[str, Any]] | None = None,
        top_k: int = 10,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        query_vector: list[float] | None = None,
        need_media: bool | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[tuple[Any, float]]:

        # Auto-compute need_media: base64 media payloads are needed when the
        # reranker will consume them.  (VLM / base-LLM-vision cases are
        # handled by the caller — they pass need_media=True explicitly.)
        if need_media is None:
            need_media = use_reranker and self.reranker is not None

        if documents is not None:
            if filters:
                # Metadata-filtered search over caller-provided documents:
                # apply the same predicate the stores apply, over each doc's
                # metadata view (every non-'text' key).
                pred = filters_to_predicate(filters)
                if pred is not None:
                    documents = [
                        d
                        for d in documents
                        if isinstance(d, dict) and pred({k: v for k, v in d.items() if k != "text"})
                    ]
                    if not documents:
                        return []
            if query_vector is not None:
                query_emb = query_vector
            else:
                query_emb = await self.aembed_query(query)
            doc_embs = await self.aembed_documents(documents)
            results = self._similarity_retrieve(query_emb, doc_embs, documents, top_k)
        else:
            vs = self.vector_store
            assert vs is not None and not isinstance(vs, dict)

            if isinstance(query, dict):
                # Multimodal query (text + image/video/audio) — embed and
                # search by vector directly.
                if query_vector is not None:
                    query_emb = query_vector
                else:
                    query_emb = await self.aembed_query(query)
                docs_and_scores = await vs.asimilarity_search_with_score_by_vector(  # type: ignore[attr-defined]
                    query_emb,
                    top_k,
                    need_media=need_media,
                    filters=filters,
                )
            else:
                docs_and_scores = await vs.asimilarity_search_with_relevance_scores(
                    query,
                    k=top_k,
                    need_media=need_media,
                    filters=filters,
                )
            results = [(self._extract_doc(doc), score) for doc, score in docs_and_scores]

        # ── Deduplicate twins ────────────────────────────────────────
        # When both a twin and its multimodal parent appear in the results,
        # drop the twin — the parent has the media the user needs.  Twins are
        # identified by the ``_twin`` metadata flag (both the text-only twins
        # of real-text docs and the media+caption caption twins of pure media
        # docs).  Dedup key: (source, page, chunk_index, time-window) so
        # twins of different sub-chunks/segments from the same source are not
        # collapsed.
        results = self._dedup_twins(results)

        if use_reranker and self.reranker is None:
            logger.warning(
                "Reranker requested (use_reranker=True) but no reranker model is "
                "configured — returning top embedding results."
            )
        elif self.reranker is not None and use_reranker and results:
            results = await self._arerank_results(query, results, reranker_top_k)
        elif use_reranker and results and len(results) > reranker_top_k:
            # Reranker unavailable — trim to reranker_top_k for consistency
            # with what the caller expected.
            results = results[:reranker_top_k]

        return results

    # -- context formatting ----------------------------------------------------

    def _format_context(self, documents: list) -> str:
        parts = []
        for doc in documents:
            if isinstance(doc, str):
                text = doc
                ref = ""
            elif isinstance(doc, dict):
                text = doc.get("text", "")
                src = doc.get("source")
                pg = doc.get("page")
                ref_parts = []
                if src:
                    ref_parts.append(f"Source: {_pvc_to_http_url(src)}")
                if pg:
                    ref_parts.append(f"Page: {pg}")
                ref = "[" + ", ".join(ref_parts) + "]\n" if ref_parts else ""
            elif isinstance(doc, Document):
                text = doc.page_content
                meta = doc.metadata or {}
                ref_parts = []
                if meta.get("source"):
                    ref_parts.append(f"Source: {_pvc_to_http_url(meta['source'])}")
                if meta.get("page"):
                    ref_parts.append(f"Page: {meta['page']}")
                ref = "[" + ", ".join(ref_parts) + "]\n" if ref_parts else ""
            else:
                parts.append(str(doc))
                continue
            parts.append(ref + text if ref else text)
        return "\n\n".join(parts)

    @staticmethod
    def _build_multimodal_content(
        postprocessed: list[str | dict[str, Any]],
        query: str | dict[str, Any],
    ) -> str | dict[str, Any] | list[dict[str, Any]]:
        """Build user message content from postprocessed docs + query.

        Returns a plain string when all content is text-only (backward
        compatible), otherwise returns an OpenAI-compatible list of content
        parts that can include ``image_url`` / ``video_url`` / ``audio_url``
        alongside text.
        """
        has_media = any(isinstance(d, dict) and any(k in d for k in ("image", "video", "audio")) for d in postprocessed)
        query_has_media = isinstance(query, dict) and any(k in query for k in ("image", "video", "audio"))

        if not has_media and not query_has_media:
            # All text — flatten to a single string (backward compatible)
            context = "\n\n".join(d if isinstance(d, str) else d.get("text", "") for d in postprocessed if d)
            if not context:
                return query
            if isinstance(query, str):
                return f"Context:\n{context}\n\nQuery: {query}"
            q = dict(query)
            q["text"] = f"Context:\n{context}\n\nQuery: {q.get('text', '')}"
            return q

        # -- multimodal: build content parts array ---------------------------
        content_parts: list[dict[str, Any]] = []

        content_parts.append({"type": "text", "text": "Context documents:"})

        for doc in postprocessed:
            if isinstance(doc, str):
                if doc:
                    content_parts.append({"type": "text", "text": doc})
                continue

            text = doc.get("text", "")
            if text:
                src = doc.get("source", "")
                pg = doc.get("page", "")
                ref = ""
                if src:
                    ref = f"[Source: {_pvc_to_http_url(src)}"
                    if pg:
                        ref += f", Page: {pg}"
                    ref += "]\n"
                content_parts.append({"type": "text", "text": ref + text})

            for url in _as_url_list(doc.get("image", [])):
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": _file_url_to_data_url(url)},
                    }
                )
            for url in _as_url_list(doc.get("video", [])):
                content_parts.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": _file_url_to_data_url(url)},
                    }
                )
            for url in _as_url_list(doc.get("audio", [])):
                content_parts.append(
                    {
                        "type": "audio_url",
                        "audio_url": {"url": _file_url_to_data_url(url)},
                    }
                )

        # Append the query
        if isinstance(query, str):
            content_parts.append({"type": "text", "text": f"Query: {query}"})
        else:
            q_text = query.get("text", "")
            if q_text:
                content_parts.append({"type": "text", "text": f"Query: {q_text}"})
            for url in _as_url_list(query.get("image", [])):
                content_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": _file_url_to_data_url(url)},
                    }
                )
            for url in _as_url_list(query.get("video", [])):
                content_parts.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": _file_url_to_data_url(url)},
                    }
                )
            for url in _as_url_list(query.get("audio", [])):
                content_parts.append(
                    {
                        "type": "audio_url",
                        "audio_url": {"url": _file_url_to_data_url(url)},
                    }
                )

        return content_parts


# ---------------------------------------------------------------------------
# RAG + LLM  (generation on top of MultimodalRAG)
# ---------------------------------------------------------------------------


class MultiModalRAGSystem:
    """Complete RAG system with an LLM for generation.

    Holds a :class:`MultimodalRAG` instance internally and delegates all
    retrieval / storage / modality-conversion to it.  Generation methods
    (``generate`` / ``agenerate``) combine retrieval with the LLM.

    Parameters
    ----------
    llm:
        Chat model used for generation (and optional routing).
    embedder, reranker, vlm, asr, caption_with_asr, preprocess,
    preprocess_chunk_size, vector_store, remote:
        Forwarded directly to :class:`MultimodalRAG`.
    """

    def __init__(
        self,
        llm: ChatModel,
        embedder: EmbeddingModel,
        reranker: RerankerModel | None = None,
        vlm: ChatModel | None = None,
        asr: VoiceModel | None = None,
        caption_with_asr: bool = False,
        caption_with_vlm: bool = False,
        preprocess: bool = True,
        preprocess_chunk_size: int = 128,
        vector_store: VectorStore | dict[str, Any] | None = None,
        remote: bool = False,
    ):
        self.llm = llm
        if remote:
            self.llm.remote()
        if caption_with_asr and asr is None:
            logger.warning(
                "caption_with_asr is enabled but no ASR model is provided; "
                "video audio-track captioning will be disabled (auto-disabled). "
                "Pass an `asr` model to enable it."
            )
        self._rag = MultimodalRAG(
            embedder=embedder,
            reranker=reranker,
            vlm=vlm,
            asr=asr,
            caption_with_asr=caption_with_asr,
            caption_with_vlm=caption_with_vlm,
            preprocess=preprocess,
            preprocess_chunk_size=preprocess_chunk_size,
            vector_store=vector_store,
            remote=remote,
        )

    def __repr__(self) -> str:
        def _mn(m: Any) -> str:
            return m.model_name if hasattr(m, "model_name") else str(m)

        fields = [
            ("llm", _mn(self.llm)),
            ("rag", repr(self._rag).replace("\n", "\n  ")),
        ]
        body = "\n".join(f"  {k}={v}" for k, v in fields)
        return f"MultiModalRAGSystem(\n{body}\n)"

    # -- model property delegates -----------------------------------------------

    @property
    def embedder(self):
        return self._rag.embedder

    @embedder.setter
    def embedder(self, val):
        self._rag.embedder = val

    @property
    def reranker(self):
        return self._rag.reranker

    @reranker.setter
    def reranker(self, val):
        self._rag.reranker = val

    @property
    def vlm(self):
        return self._rag.vlm

    @vlm.setter
    def vlm(self, val):
        self._rag.vlm = val

    @property
    def asr(self):
        return self._rag.asr

    @asr.setter
    def asr(self, val):
        self._rag.asr = val

    @property
    def caption_with_asr(self):
        return self._rag.caption_with_asr

    @caption_with_asr.setter
    def caption_with_asr(self, val):
        self._rag.caption_with_asr = val

    @property
    def preprocess(self):
        return self._rag.preprocess

    @preprocess.setter
    def preprocess(self, val):
        self._rag.preprocess = val

    @property
    def preprocess_chunk_size(self):
        return self._rag.preprocess_chunk_size

    @preprocess_chunk_size.setter
    def preprocess_chunk_size(self, val):
        self._rag.preprocess_chunk_size = val

    @property
    def vector_store(self):
        return self._rag.vector_store

    @vector_store.setter
    def vector_store(self, val):
        self._rag.vector_store = val

    @property
    def remote(self):
        return self._rag.remote

    @remote.setter
    def remote(self, val):
        self._rag.remote = val

    @property
    def _preprocessor(self):
        return self._rag._preprocessor

    @property
    def _postprocessor(self):
        return self._rag._postprocessor

    @property
    def _embed_modalities(self):
        return self._rag._embed_modalities

    @property
    def embed(self):
        return self._rag.embed

    @property
    def rank(self):
        return self._rag.rank

    @property
    def embed_documents(self):
        return self._rag.embed_documents

    @property
    def aembed_documents(self):
        return self._rag.aembed_documents

    @property
    def embed_query(self):
        return self._rag.embed_query

    @property
    def aembed_query(self):
        return self._rag.aembed_query

    @property
    def arerank(self):
        return self._rag.arerank

    @property
    def ascore(self):
        return self._rag.ascore

    @property
    def rerank(self):
        return self._rag.rerank

    @property
    def score(self):
        return self._rag.score

    # -- LLM-specific -----------------------------------------------------------

    @property
    def _llm_modalities(self) -> set[str]:
        return set(self.llm.allowable_modalities) if hasattr(self.llm, "allowable_modalities") else {"text"}

    @property
    def chat(self):
        return self.llm.llm_chat_function_call

    @property
    def achat(self):
        return self.llm.llm_async_chat_function_call

    # -- method delegates -------------------------------------------------------

    def add_to_vector_store(self, documents, **kwargs):
        return self._rag.add_to_vector_store(documents, **kwargs)

    async def aadd_to_vector_store(self, documents, **kwargs):
        return await self._rag.aadd_to_vector_store(documents, **kwargs)

    def retrieve(self, query, documents=None, top_k=10, use_reranker=True, reranker_top_k=3, query_vector=None):
        return self._rag.retrieve(query, documents, top_k, use_reranker, reranker_top_k, query_vector=query_vector)

    async def aretrieve(self, query, documents=None, top_k=10, use_reranker=False, reranker_top_k=3, query_vector=None):
        return await self._rag.aretrieve(
            query, documents, top_k, use_reranker, reranker_top_k, query_vector=query_vector
        )

    def list_documents(self, limit=50):
        return self._rag.list_documents(limit)

    async def alist_documents(self, limit=50):
        return await self._rag.alist_documents(limit)

    # -- query routing (LLM decides if RAG is needed) -------------------------

    _ROUTE_PROMPT = (
        "You are a router. Determine if you need to retrieve external knowledge "
        "to answer the user's query accurately. Reply with exactly one word: "
        "YES or NO."
    )

    def _needs_rag(self, query: str | dict[str, Any]) -> bool:
        return sync_wrapper_safe(self._aneeds_rag, {"query": query})

    async def _aneeds_rag(self, query: str | dict[str, Any]) -> bool:
        query_text = query if isinstance(query, str) else query.get("text", str(query))
        messages = [
            {"role": "system", "content": self._ROUTE_PROMPT},
            {"role": "user", "content": query_text},
        ]
        reply = await self.llm.llm_async_chat_function_call(messages)
        answer = reply.choices[0].message.content.strip().upper()
        return answer.startswith("YES")

    # -- RAG generation ---------------------------------------------------------

    def generate(
        self,
        query: str | dict[str, Any],
        documents: list[str | dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        top_k: int = 10,
        use_vlm: bool = True,
        route: bool = False,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
        **llm_kwargs,
    ) -> str:
        return sync_wrapper_safe(
            self.agenerate,
            dict(
                query=query,
                documents=documents,
                system_prompt=system_prompt,
                top_k=top_k,
                use_vlm=use_vlm,
                route=route,
                use_reranker=use_reranker,
                reranker_top_k=reranker_top_k,
                **llm_kwargs,
            ),
        )

    async def agenerate(
        self,
        query: str | dict[str, Any],
        documents: list[str | dict[str, Any]] | None = None,
        system_prompt: str | None = None,
        top_k: int = 10,
        use_vlm: bool = True,
        route: bool = False,
        use_reranker: bool = True,
        reranker_top_k: int = 3,
        **llm_kwargs,
    ) -> str:
        t0 = time.monotonic()

        if route:
            t_r = time.monotonic()
            needs_rag = await self._aneeds_rag(query)
            if not needs_rag:
                direct_messages: list[dict[str, Any]] = []
                if system_prompt:
                    direct_messages.append({"role": "system", "content": system_prompt})
                direct_messages.append({"role": "user", "content": query})
                response = await self.achat(direct_messages, **llm_kwargs)
                logger.verbose(  # type: ignore[attr-defined]
                    "%.2fs route(llm)  — direct answer (RAG skipped)",
                    time.monotonic() - t0,
                )  # type: ignore[attr-defined]
                return response.choices[0].message.content
            logger.verbose("%.2fs route(llm)  → RAG needed", time.monotonic() - t_r)  # type: ignore[attr-defined]

        t_r = time.monotonic()
        # Two-phase media fetch: phase 1 searches WITHOUT the heavy tier-3
        # base64 payloads whenever the reranker doesn't need them upfront;
        # _media_payloads_needed() then predicts — from the light docs
        # (captions + tier-2 refs) and the query — whether a consumer will
        # actually use the payloads, and only then does phase 2 re-search
        # with them included.  With a VLM configured but a generic query over
        # pre-captioned docs this skips a multi-MB Qdrant transfer entirely.
        # (The second search re-embeds the query — cheap relative to the
        # payload transfer it avoids; the embed batcher absorbs it.)
        reranker_needs_media = use_reranker and self._rag.reranker is not None
        retrieved = await self._rag.aretrieve(
            query,
            documents,
            top_k,
            use_reranker=use_reranker,
            reranker_top_k=reranker_top_k,
            need_media=reranker_needs_media,
        )
        retrieved_docs = [d for d, _ in retrieved]
        if not reranker_needs_media and _media_payloads_needed(
            retrieved_docs,
            use_vlm=use_vlm,
            vlm=self._rag.vlm,
            llm_modalities=self._llm_modalities,
            query=query,
        ):
            retrieved = await self._rag.aretrieve(
                query,
                documents,
                top_k,
                use_reranker=False,
                reranker_top_k=reranker_top_k,
                need_media=True,
            )
            retrieved_docs = [d for d, _ in retrieved]
        logger.verbose(  # type: ignore[attr-defined]
            "%.2fs retrieve  — %d docs (top_k=%s, reranker=%s%s)",
            time.monotonic() - t_r,
            len(retrieved_docs),
            top_k,
            "yes" if self._rag.reranker and use_reranker else "no",
            f", reranker_top_k={reranker_top_k}" if self._rag.reranker and use_reranker else "",
        )  # type: ignore[attr-defined]

        needs_llm_conversion = (
            use_vlm
            and (self._rag.vlm is not None or self._rag.asr is not None)
            and any(isinstance(d, dict) and any(k in d for k in ("image", "video", "audio")) for d in retrieved_docs)
        )

        t_p = time.monotonic()
        if needs_llm_conversion:
            postprocessed = await self._rag._postprocessor.acall(
                retrieved_docs,
                llm_modalities=self._llm_modalities,
                query=query,
            )
            user_content = self._rag._build_multimodal_content(postprocessed, query)
            logger.verbose(  # type: ignore[attr-defined]
                "%.2fs postproc  — VLM/ASR conversion (%d docs with media)",
                time.monotonic() - t_p,
                sum(
                    1
                    for d in retrieved_docs
                    if isinstance(d, dict) and any(k in d for k in ("image", "video", "audio"))
                ),
            )  # type: ignore[attr-defined]
        else:
            context = self._rag._format_context(retrieved_docs)
            if context:
                if isinstance(query, str):
                    user_content = f"Context:\n{context}\n\nQuery: {query}"
                else:
                    q = dict(query)
                    q["text"] = f"Context:\n{context}\n\nQuery: {q.get('text', '')}"
                    user_content = q
            else:
                user_content = query
            logger.verbose(  # type: ignore[attr-defined]
                "%.2fs postproc  — text-only (skipped VLM/ASR)", time.monotonic() - t_p
            )  # type: ignore[attr-defined]

        t_g = time.monotonic()
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        elif user_content:
            messages.append(
                {
                    "role": "system",
                    "content": "You are a helpful assistant. Use the retrieved context to answer the user's query. "
                    "When you reference specific documents, cite their source and page number if available.",
                }
            )
        messages.append({"role": "user", "content": user_content})
        response = await self.achat(messages, **llm_kwargs)
        logger.verbose(  # type: ignore[attr-defined]
            "%.2fs llm       — generation (%d tokens?)  [total %.2fs]",
            time.monotonic() - t_g,
            len(response.choices[0].message.content.split()),
            time.monotonic() - t0,
        )  # type: ignore[attr-defined]
        return response.choices[0].message.content
